import os
import yaml
import json
from flask import Flask, jsonify, request, render_template
from printer_client import ImplicitFTP_TLS, upload_file_to_printer, start_print_on_printer
from printer_lan import *
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import uuid

JOBS = {}  # job_id -> dict(progress...)
JOBS_LOCK = threading.Lock()


# ---------- ЗАГРУЗКА КОНФИГА ПРИНТЕРОВ ----------
with open("printers.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

PRINTERS = cfg["printers"]


def get_printer(printer_id: str):
    for p in PRINTERS:
        if p["id"] == printer_id:
            return p
    return None
    

STATUS_CACHE = {}   # printer_id -> {ok, gcode_state, ts, error}
STATUS_LOCK = threading.Lock()


def _poll_one(printer_cfg: dict) -> dict:
    pid = printer_cfg["id"]
    ip = printer_cfg.get("ip") or ""
    serial = printer_cfg.get("serial") or ""
    access_code = printer_cfg.get("access_code") or ""

    if not ip or not serial or not access_code:
        return {"id": pid, "ok": False, "gcode_state": None, "error": "missing_config", "ts": time.time()}

    try:
        p = Printer(ip, serial, access_code)
        st = p.getStatus()  # st — dict

        # если status() вернул ok=False
        if not st.get("ok", False):
            return {
                "id": pid,
                "ok": False,
                "gcode_state": None,
                "error": st.get("error", "no_report"),
                "ts": time.time(),
            }

        gcode_state = st["gcode_state"]  

        return {"id": pid, "ok": True, "gcode_state": gcode_state, "error": None, "ts": time.time()}

    except KeyError:
        # если в st нет ключа 'gcode_state'
        return {"id": pid, "ok": False, "gcode_state": None, "error": "no_gcode_state", "ts": time.time()}

    except Exception as e:
        return {"id": pid, "ok": False, "gcode_state": None, "error": str(e), "ts": time.time()}


def refresh_all_statuses():
    # Параллельно, иначе 90 принтеров будут очень долго
    results = []
    with ThreadPoolExecutor(max_workers=15) as ex:
        futs = [ex.submit(_poll_one, p) for p in PRINTERS]
        for f in as_completed(futs):
            results.append(f.result())

    with STATUS_LOCK:
        for r in results:
            STATUS_CACHE[r["id"]] = r


def status_poller_loop(interval_sec: int = 60):
    while True:
        try:
            refresh_all_statuses()
        except Exception:
            pass
        time.sleep(interval_sec)


def _job_set(job_id, pid, **kwargs):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        pr = job["printers"].setdefault(pid, {})
        pr.update(kwargs)

def _job_done(job_id, pid, ok: bool, message: str = None):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["done"] += 1
        if ok:
            job["ok_count"] += 1
        else:
            job["err_count"] += 1
        pr = job["printers"].setdefault(pid, {})
        pr["stage"] = "ok" if ok else "error"
        pr["message"] = message
        job["finished"] = (job["done"] >= job["total"])



def retry(fn, tries=3, base_delay=1.0, factor=2.0, on_retry=None):
    """
    tries=3: всего попыток
    base_delay=1: первая пауза 1с, потом 2с, потом 4с...
    on_retry(attempt, exc, delay)
    """
    last_exc = None
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt == tries:
                raise
            delay = base_delay * (factor ** (attempt - 1))
            if on_retry:
                on_retry(attempt, e, delay)
            time.sleep(delay)
    raise last_exc


def _process_one_printer(job_id: str, pid: str, p: dict, temp_path: str, safe_name: str):
    ip = p["ip"]
    serial = p["serial"]
    access_code = p["access_code"]

    def log_retry(stage):
        def _cb(attempt, exc, delay):
            _job_set(job_id, pid, stage=stage, message=f"retry {attempt}: {exc} (sleep {delay:.1f}s)")
        return _cb

    try:
        # 1) UPLOAD
        _job_set(job_id, pid, stage="uploading", message=None)
        retry(
            lambda: upload_file_to_printer(ip, access_code, temp_path),
            tries=3,
            base_delay=1.0,
            factor=2.0,
            on_retry=log_retry("uploading"),
        )

        _job_set(job_id, pid, stage="uploaded", message=None)
        time.sleep(1.5)

        # 2) START
        _job_set(job_id, pid, stage="starting", message=None)
        retry(
            lambda: start_print_on_printer(ip, access_code, serial, safe_name, plate_num=1),
            tries=3,
            base_delay=1.0,
            factor=2.0,
            on_retry=log_retry("starting"),
        )

        _job_set(job_id, pid, stage="started", message=None)
        _job_done(job_id, pid, ok=True)

    except Exception as e:
        _job_done(job_id, pid, ok=False, message=str(e))


def _run_job(job_id: str, printer_ids: list, temp_path: str, safe_name: str, max_workers: int = 12):
    # Подготовим список валидных конфигов
    valid = []

    for pid in printer_ids:
        p = get_printer(pid)
        if not p:
            _job_set(job_id, pid, stage="not_found", message=None)
            _job_done(job_id, pid, ok=False, message="not_found")
            continue

        ip = p.get("ip")
        serial = p.get("serial")
        access_code = p.get("access_code")
        if not ip or not serial or not access_code:
            _job_set(job_id, pid, stage="missing_config", message=None)
            _job_done(job_id, pid, ok=False, message="missing_config")
            continue

        _job_set(job_id, pid, stage="queued", message=None)
        valid.append((pid, p))

    # Параллельная обработка
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut_map = {
            ex.submit(_process_one_printer, job_id, pid, p, temp_path, safe_name): pid
            for pid, p in valid
        }
        for fut in as_completed(fut_map):
            # ошибки уже учтены внутри _process_one_printer,
            # но на всякий — чтобы пул не упал молча:
            try:
                fut.result()
            except Exception as e:
                pid = fut_map[fut]
                _job_done(job_id, pid, ok=False, message=f"worker crash: {e}")

# ---------- СОЗДАЁМ FLASK ПРИЛОЖЕНИЕ ----------
app = Flask(__name__)


def start_status_poller():
    t = threading.Thread(target=status_poller_loop, kwargs={"interval_sec": 10}, daemon=True)
    t.start()

# после создания app = Flask(...)
if not app.debug or (app.debug and os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
    start_status_poller()

    
@app.get("/api/status")
def api_status():
    # Отдаём то, что есть в кэше; если принтера нет в кэше — будет NONE на фронте
    with STATUS_LOCK:
        statuses = list(STATUS_CACHE.values())
    return jsonify({"statuses": statuses})


@app.get("/api/jobs/<job_id>")
def api_job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "job_not_found"}), 404
        return jsonify(job)




@app.route("/")
def index():
    """Отдаём HTML страницу."""
    return render_template("index.html")


@app.get("/api/printers")
def api_printers():
    """Список принтеров (для фронта)."""
    # На будущее сюда можно добавить статусы (idle/printing) через MQTT
    return jsonify(PRINTERS)


@app.post("/api/upload_and_print")
def api_upload_and_print():
    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400

    file = request.files["file"]
    printers_json = request.form.get("printers", "[]")

    try:
        printer_ids = json.loads(printers_json)
    except json.JSONDecodeError:
        return jsonify({"error": "printers is not valid JSON"}), 400

    if not printer_ids:
        return jsonify({"error": "no printers specified"}), 400
    if file.filename == "":
        return jsonify({"error": "empty filename"}), 400

    os.makedirs("jobs", exist_ok=True)
    safe_name = os.path.basename(file.filename)
    temp_path = os.path.join("jobs", safe_name)
    file.save(temp_path)

    job_id = uuid.uuid4().hex

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "filename": safe_name,
            "created_ts": time.time(),
            "total": len(printer_ids),
            "done": 0,
            "ok_count": 0,
            "err_count": 0,
            "finished": False,
            "printers": {pid: {"stage": "queued"} for pid in printer_ids},
        }

    t = threading.Thread(target=_run_job, args=(job_id, printer_ids, temp_path, safe_name), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})



if __name__ == "__main__":
    # debug=True только для разработки
    app.run(host="0.0.0.0", port=8080, debug=True)
