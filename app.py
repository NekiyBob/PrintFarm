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
from mqtt_manager import MqttStatusManager, PrinterCfg
from printer_history import PrinterHistory
import random


UPLOAD_CONCURRENCY = 3  # попробуй 2..4
UPLOAD_SEM = threading.Semaphore(UPLOAD_CONCURRENCY)
RESTART_CONCURRENCY = 3   # попробуй 2..4
RESTART_SEM = threading.Semaphore(RESTART_CONCURRENCY)


HISTORY = PrinterHistory("printer_history.json")
JOBS = {}  # job_id -> dict(progress...)
JOBS_LOCK = threading.Lock()
MQTT_MANAGER = None


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
            delay += random.uniform(0, 0.35 * delay)  # джиттер 0..35%

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

        def do_upload():
            with UPLOAD_SEM:
                return upload_file_to_printer(ip, access_code, temp_path)

        retry(
            do_upload,
            tries=3, # Количество одновременных загрузок
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

        HISTORY.set_started(pid, safe_name)

        _job_set(job_id, pid, stage="started", message=None)
        _job_done(job_id, pid, ok=True)

    except Exception as e:
        _job_done(job_id, pid, ok=False, message=str(e))

def _restart_one_printer(job_id: str, pid: str, p: dict):
    ip = p["ip"]
    serial = p["serial"]
    access_code = p["access_code"]

    last_file = HISTORY.get_last_printed(pid)
    if not last_file:
        _job_set(job_id, pid, stage="error", message="no_last_printed")
        _job_done(job_id, pid, ok=False, message="no_last_printed")
        return

    def log_retry(stage):
        def _cb(attempt, exc, delay):
            _job_set(job_id, pid, stage=stage, message=f"retry {attempt}: {exc} (sleep {delay:.1f}s)")
        return _cb

    try:
        _job_set(job_id, pid, stage="starting", file=last_file, message=None)

        def do_start():
            with RESTART_SEM:
                return start_print_on_printer(ip, access_code, serial, last_file)

        retry(
            do_start,
            tries=3,
            base_delay=1.0,
            factor=2.0,
            on_retry=log_retry("starting"),
        )


        # считаем, что этот файл стал "последним запущенным" у нас
        HISTORY.set_started(pid, last_file)

        _job_set(job_id, pid, stage="started", file=last_file, message=None)
        _job_done(job_id, pid, ok=True)

    except Exception as e:
        _job_done(job_id, pid, ok=False, message=str(e))


def _run_restart_job(job_id: str, printer_ids: list, max_workers: int = 20):
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

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut_map = {ex.submit(_restart_one_printer, job_id, pid, p): pid for pid, p in valid}
        for fut in as_completed(fut_map):
            try:
                fut.result()
            except Exception as e:
                pid = fut_map[fut]
                _job_done(job_id, pid, ok=False, message=f"worker crash: {e}")


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


def _on_status(pid: str, st: dict):
    with STATUS_LOCK:
        STATUS_CACHE[pid] = st

    
    HISTORY.note_report(
        pid=pid,
        ok=st.get("ok"),
        gcode_state=st.get("gcode_state"),
        file_hint=st.get("file")  # если поля нет — будет None
    )





# ---------- СОЗДАЁМ FLASK ПРИЛОЖЕНИЕ ----------
app = Flask(__name__)

# после создания app = Flask(...)
def start_mqtt_manager():
    global MQTT_MANAGER
    printer_cfgs = []
    for p in PRINTERS:
        printer_cfgs.append(
            PrinterCfg(
                id=p["id"],
                ip=p.get("ip") or "",
                serial=p.get("serial") or "",
                access_code=p.get("access_code") or "",
            )
        )

    MQTT_MANAGER = MqttStatusManager(
        printers=printer_cfgs,
        on_status=_on_status,
        offline_after_sec=50.0,     # Через сколько сек без новых репортов принтер считается выкл.
        monitor_interval_sec=2.0,   # Как часто менеджер проверяет выкл ли кто-то.
        keepalive=60,               # Держит mqtt tcp открытым (пингует раз в 60 сек)
    )
    MQTT_MANAGER.start()


# ВАЖНО: чтобы debug-reloader не запускал два раза
if not app.debug or (app.debug and os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
    start_mqtt_manager()


    
@app.get("/api/status")
def api_status():
    with STATUS_LOCK:
        cache = dict(STATUS_CACHE)
    # добавим last_printed на каждый принтер
    out = []
    for p in PRINTERS:
        pid = p["id"]
        st = cache.get(pid, {"id": pid, "ok": None, "gcode_state": "NONE"})
        st = dict(st)
        st["last_printed"] = HISTORY.get_last_printed(pid)
        out.append(st)
    return jsonify({"statuses": out})



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

@app.post("/api/restart_last_printed")
def api_restart_last_printed():
    data = request.get_json(silent=True) or {}
    printer_ids = data.get("printers", [])

    if not printer_ids:
        return jsonify({"error": "no printers specified"}), 400

    job_id = uuid.uuid4().hex

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "filename": "(restart last printed)",
            "created_ts": time.time(),
            "total": len(printer_ids),
            "done": 0,
            "ok_count": 0,
            "err_count": 0,
            "finished": False,
            "printers": {pid: {"stage": "queued"} for pid in printer_ids},
        }

    t = threading.Thread(target=_run_restart_job, args=(job_id, printer_ids), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

@app.post("/api/mqtt/restart")
def api_mqtt_restart():
    global MQTT_MANAGER

    if MQTT_MANAGER is None:
        return jsonify({"error": "mqtt_manager_not_initialized"}), 500

    # (опционально) очистим статусы, чтобы UI не показывал старое
    with STATUS_LOCK:
        STATUS_CACHE.clear()

    try:
        MQTT_MANAGER.restart()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    # debug=True только для разработки
    app.run(host="0.0.0.0", port=8080, debug=True)
