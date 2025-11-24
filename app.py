import os
import yaml
from flask import Flask, jsonify, request, render_template
from printer_client import ImplicitFTP_TLS, upload_file_to_printer, start_print_on_printer, upload_and_start_file_to_printer


# ---------- ЗАГРУЗКА КОНФИГА ПРИНТЕРОВ ----------
with open("printers.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

PRINTERS = cfg["printers"]


def get_printer(printer_id: str):
    for p in PRINTERS:
        if p["id"] == printer_id:
            return p
    return None
    

# ---------- СОЗДАЁМ FLASK ПРИЛОЖЕНИЕ ----------
app = Flask(__name__)


@app.route("/")
def index():
    """Отдаём HTML страницу."""
    return render_template("index.html")


@app.get("/api/printers")
def api_printers():
    """Список принтеров (для фронта)."""
    # На будущее сюда можно добавить статусы (idle/printing) через MQTT
    return jsonify(PRINTERS)


@app.post("/api/print")
def api_print():
    """
    Запуск печати:
    ожидает JSON: { "printers": ["R1-S1-L1-P1", ...], "filename": "job.3mf" }
    """
    print('odfa')
    data = request.get_json(silent=True) or {}
    printer_ids = data.get("printers") or []
    filename = data.get("filename")

    if not printer_ids or not filename:
        return jsonify({"error": "printers and filename required"}), 400

    # считаем, что файлы лежат в папке jobs/ рядом с app.py
    local_path = os.path.join("jobs", filename)
    if not os.path.isfile(local_path):
        return jsonify({"error": f"file not found: {local_path}"}), 400

    results = []
    for pid in printer_ids:
        p = get_printer(pid)
        if not p:
            results.append({"printer": pid, "status": "not_found"})
            continue
        ip = p["ip"]
        serial = p["serial"]
        access_code = p["access_code"]
        if not ip or not serial or not access_code:
            results.append({"printer": pid, "status": "missing_config"})
            continue

        try:
            # 1) загрузка .3mf на принтер
            upload_file_to_printer(ip, "bblp", access_code, local_path)
            # 2) запуск печати
            start_print_on_printer(ip, access_code, serial,
                                   os.path.basename(local_path), plate_num=1)
            results.append({"printer": pid, "status": "ok"})
        except Exception as e:
            results.append({"printer": pid, "status": "error", "message": str(e)})

    return jsonify(results)


if __name__ == "__main__":
    # debug=True только для разработки
    app.run(host="0.0.0.0", port=8080, debug=True)
