import json
import ssl
import time
import uuid
import threading
from typing import Any, Dict, Tuple

import paho.mqtt.client as mqtt


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _merge_non_empty(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in incoming.items():
        if isinstance(value, dict):
            nested = base.get(key)
            if not isinstance(nested, dict):
                nested = {}
            base[key] = _merge_non_empty(nested, value)
        elif _has_value(value):
            base[key] = value
    return base


def _normalize_report_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    report_block = data.get("report")
    if isinstance(report_block, dict):
        _merge_non_empty(normalized, report_block)
    _merge_non_empty(normalized, data)
    return normalized


def _report_has_rich_print_data(data: Dict[str, Any]) -> bool:
    pr = data.get("print", {}) or {}
    rich_keys = (
        "gcode_state",
        "mc_percent",
        "layer_num",
        "total_layer_num",
        "subtask_name",
        "project_name",
        "gcode_file",
        "nozzle_target_temper",
        "bed_target_temper",
    )
    return any(_has_value(pr.get(key)) for key in rich_keys)


def _normalize_print_state(model: str, print_block: Dict[str, Any]) -> str | None:
    state = str(print_block.get("gcode_state") or "").strip().upper()
    if state:
        return state

    if str(model or "").strip().upper() == "P1S":
        remaining_time = _safe_int(print_block.get("mc_remaining_time"))
        progress_percent = _safe_float(print_block.get("mc_percent"))
        if remaining_time is not None and remaining_time > 0:
            return "RUNNING"
        if progress_percent is not None and 0 < progress_percent < 100:
            return "RUNNING"

    return None


def _print_has_started(model: str, print_block: Dict[str, Any]) -> bool:
    state = _normalize_print_state(model, print_block)
    return state in {"RUNNING", "PRINTING", "PREPARE", "PREPARING"}


def _cleanup_mqtt_client(client: mqtt.Client) -> None:
    try:
        client.disconnect()
    except Exception:
        pass
    try:
        client.loop_stop()
    except Exception:
        pass


def _publish_and_wait(
    client: mqtt.Client,
    topic: str,
    payload: dict[str, Any],
    *,
    qos: int = 0,
    timeout: float = 5.0,
) -> None:
    info = client.publish(topic, json.dumps(payload), qos=qos)
    info.wait_for_publish(timeout=timeout)
    if info.rc > 0:
        raise RuntimeError(f"MQTT publish failed: rc={info.rc}")


class Printer:
    """
    Управление Bambu принтером через LAN MQTT (порт 8883) напрямую.

    Поля:
      ip          - IP принтера
      serial      - serial number
      access_code - access code (пароль для bblp)
    """

    def __init__(self, ip: str, serial: str, access_code: str, model: str = ""):
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        self.model = str(model or "").strip().upper()

    def _build_project_url(self, name: str) -> str:
        if self.model == "P2S":
            return f"ftp://{name}"
        return f"file:///sdcard/{name}"

    

    def _mqtt_make_client(self, client_id_prefix: str) -> mqtt.Client:
        client_id = f"{client_id_prefix}-{uuid.uuid4()}"
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

        # Bambu LAN: username=bblp, password=access_code
        client.username_pw_set("bblp", self.access_code)

        # self-signed TLS
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)

        return client

    def _mqtt_connect_and_listen(
        self,
        on_report,
        connect_timeout: float = 5.0,
    ) -> Tuple[mqtt.Client, threading.Event]:
        """
        Подключаем к MQTT, подписываемся на report и loop_start().
        Возвращает (client, connected_evt).
        """
        topic_report = f"device/{self.serial}/report"
        connected_evt = threading.Event()

        client = self._mqtt_make_client("bambu")
        connect_error = {"value": None}

        def on_connect(cl, userdata, flags, rc, properties=None):
            if rc != 0:
                connect_error["value"] = f"MQTT connect failed: rc={rc}"
                connected_evt.set()
                return
            cl.subscribe(topic_report)
            connected_evt.set()

        def on_message(cl, userdata, msg):
            try:
                payload = msg.payload.decode("utf-8", errors="replace")
                data = json.loads(payload)
                on_report(data)
            except Exception:
                pass

        client.on_connect = on_connect
        client.on_message = on_message

        client.reconnect_delay_set(min_delay=1, max_delay=2)
        client.connect_async(self.ip, 8883, keepalive=60)
        client.loop_start()

        if not connected_evt.wait(connect_timeout):
            _cleanup_mqtt_client(client)
            raise RuntimeError("MQTT connect timeout")

        if connect_error["value"]:
            _cleanup_mqtt_client(client)
            raise RuntimeError(connect_error["value"])

        return client, connected_evt

   

    def getStatus(self, timeout: float = 10.0) -> Dict[str, Any]:
        """
        Возвращает последний report от принтера.
        Ждёт хотя бы один report (timeout секунд).
        """
        got_evt = threading.Event()
        detailed_evt = threading.Event()
        last_report: Dict[str, Any] = {}
        merged_report: Dict[str, Any] = {}

        def on_report(data: Dict[str, Any]):
            nonlocal last_report
            normalized = _normalize_report_payload(data)
            last_report = normalized
            _merge_non_empty(merged_report, normalized)
            got_evt.set()
            if _report_has_rich_print_data(merged_report):
                detailed_evt.set()

        client, _ = self._mqtt_connect_and_listen(on_report)

        try:
            deadline = time.time() + timeout
            if not got_evt.wait(timeout):
                return {"ok": False, "error": "no report received", "report": None}

            remaining = max(0.0, deadline - time.time())
            if not detailed_evt.is_set() and remaining > 0:
                detailed_evt.wait(min(2.5, remaining))

            report = merged_report or last_report
            pr = dict(report.get("print", {}) or {})
            gcode_state = _normalize_print_state(self.model, pr)
            if gcode_state and not pr.get("gcode_state"):
                pr["gcode_state"] = gcode_state
                report = dict(report)
                report["print"] = pr
            return {
                "ok": True,
                "gcode_state": gcode_state,
                "print": pr,
                "report": report,
            }
        finally:
            _cleanup_mqtt_client(client)

    def pause(self, timeout: float = 15.0) -> bool:
        """
        Ставит печать на паузу.
        Возвращает True, если увидели подтверждение в report (gcode_state PAUSE/PAUSED).
        """
        topic_request = f"device/{self.serial}/request"

        paused_evt = threading.Event()
        last_gcode_state = {"value": None}
        last_report = {"value": None}

        def on_report(data: Dict[str, Any]):
            last_report["value"] = data
            pr = data.get("print", {})
            gcode_state = pr.get("gcode_state")
            if gcode_state:
                last_gcode_state["value"] = gcode_state
                if str(gcode_state).upper() in ("PAUSE", "PAUSED"):
                    paused_evt.set()

        client, _ = self._mqtt_connect_and_listen(on_report)

        try:
            # pause с sequence_id (важно)
            cmd = {"print": {"sequence_id": "0", "command": "pause"}}
            _publish_and_wait(client, topic_request, cmd, qos=0)

            if paused_evt.wait(timeout):
                return True

            return False

        finally:
            _cleanup_mqtt_client(client)

    def resume(self, timeout: float = 15.0) -> bool:
        """
        Возобновляет печать после паузы.
        Возвращает True, если увидели подтверждение в report (gcode_state RUNNING/PRINTING).
        """
        topic_request = f"device/{self.serial}/request"

        resumed_evt = threading.Event()
        last_gcode_state = {"value": None}
        last_report = {"value": None}

        def on_report(data: Dict[str, Any]):
            last_report["value"] = data
            pr = data.get("print", {})
            gcode_state = pr.get("gcode_state")
            if gcode_state:
                last_gcode_state["value"] = gcode_state
                if str(gcode_state).upper() in ("RUNNING", "PRINTING"):
                    resumed_evt.set()

        client, _ = self._mqtt_connect_and_listen(on_report)

        try:
            cmd = {"print": {"sequence_id": "0", "command": "resume"}}
            _publish_and_wait(client, topic_request, cmd, qos=0)

            if resumed_evt.wait(timeout):
                return True

            return False

        finally:
            _cleanup_mqtt_client(client)

    def stop(self, timeout: float = 15.0) -> bool:
        """
        Останавливает текущую печать.
        Возвращает True, если увидели подтверждение по report (gcode_state стал IDLE/FINISH/STOP...).
        """
        topic_request = f"device/{self.serial}/request"

        stopped_evt = threading.Event()

        def on_report(data: Dict[str, Any]):
            pr = data.get("print", {})
            st = pr.get("gcode_state")
            if st and str(st).upper() in ("IDLE", "FINISH", "STOP", "STOPPED", "DONE"):
                stopped_evt.set()

        client, _ = self._mqtt_connect_and_listen(on_report)

        try:
            cmd = {"print": {"sequence_id": "0", "command": "stop"}}
            _publish_and_wait(client, topic_request, cmd, qos=0)

            return stopped_evt.wait(timeout)

        finally:
            _cleanup_mqtt_client(client)

    def start_print(
        self,
        filename_on_sd: str,
        plate_num: int = 1,
        plate_path: str | None = None,
        timeout: float = 25.0,
        bed_leveling: bool = False,
        flow_cali: bool = False,
        ams_mapping: list[int] | None = None,
        vibration_cali: bool = False,
        use_ams: bool = False,
    ) -> bool:
        """
        Запускает печать файла, который уже лежит на принтере.
        - для .gcode: gcode_param
        - для .gcode.3mf/.3mf: project_url
        """
        topic_request = f"device/{self.serial}/request"
        started_evt = threading.Event()

        def on_report(data: Dict[str, Any]):
            normalized = _normalize_report_payload(data)
            pr = normalized.get("print", {}) or {}
            if _print_has_started(self.model, pr):
                started_evt.set()

            # текст ошибки в консоли
            err = pr.get("print_error") or pr.get("error") or pr.get("errmsg") or pr.get("err_msg")
            if err:
                print("[REPORT ERROR]", err)

        client, _ = self._mqtt_connect_and_listen(on_report)

        try:
            name = filename_on_sd
            lower = name.lower()

            # =========================
            # 1) Обычный .gcode
            # =========================
            if lower.endswith(".gcode") or lower.endswith(".gcode.gz"):
                gcode_param = f"/{name}"
            
                cmd = {
                    "print": {
                        "sequence_id": "1",
                        "command": "gcode_file",
                        "param": gcode_param,
                    }
                }

                print("[MQTT SEND]", cmd)
                _publish_and_wait(client, topic_request, cmd, qos=1)
                return started_evt.wait(timeout)

            # =========================
            # 2) .gcode.3mf 
            
            project_url = self._build_project_url(name)
            selected_plate_path = plate_path or f"Metadata/plate_{plate_num}.gcode"

            base_print = {
                "sequence_id": "1",
                "command": "project_file",
                "param": selected_plate_path,
                "url": project_url,
                "file": name,
                "subtask_name": name,
                "project_id": "0",
                "profile_id": "0",
                "task_id": "0",
                "subtask_id": "0",
                "bed_leveling": bed_leveling,
                "flow_cali": flow_cali,
                "vibration_cali": vibration_cali,
                "use_ams": False,
                "ams_mapping": [254],
            }

            # if use_ams:
            #     base_print["ams_mapping"] = list(ams_mapping or [0])

            cmd = {"print": base_print}

            print("[MQTT SEND]", cmd)
            _publish_and_wait(client, topic_request, cmd, qos=1)
            return started_evt.wait(timeout)

        finally:
            _cleanup_mqtt_client(client)



