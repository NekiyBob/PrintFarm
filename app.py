import os
import yaml
import json
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


@app.post("/api/upload_and_print")
def api_upload_and_print():
    """
    Принимает multipart/form-data:
      - file: бинарник (.3mf)
      - printers: JSON-строка со списком id принтеров
    """
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

    # Сохраняем файл во временный путь в папке jobs/
    os.makedirs("jobs", exist_ok=True)
    safe_name = os.path.basename(file.filename)
    temp_path = os.path.join("jobs", safe_name)
    file.save(temp_path)

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
            # 1) загрузка файла на принтер по FTP
            upload_file_to_printer(ip, "bblp", access_code, temp_path)
            # 2) запуск печати через MQTT/bambulabs_api
            start_print_on_printer(ip, access_code, serial, safe_name, plate_num=1)

            results.append({"printer": pid, "status": "ok"})
        except Exception as e:
            results.append({"printer": pid, "status": "error", "message": str(e)})

    # при желании можно удалить временный файл:
    # os.remove(temp_path)

    return jsonify(results)


if __name__ == "__main__":
    # debug=True только для разработки
    app.run(host="0.0.0.0", port=8080, debug=True)
