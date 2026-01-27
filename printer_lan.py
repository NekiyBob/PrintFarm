import json
import ssl
import time
import uuid
import threading
from typing import Any, Dict, Tuple

import paho.mqtt.client as mqtt


class Printer:
    """
    Управление Bambu принтером через LAN MQTT (порт 8883) напрямую.

    Поля:
      ip          - IP принтера
      serial      - serial number
      access_code - access code (пароль для bblp)
    """

    def __init__(self, ip: str, serial: str, access_code: str):
        self.ip = ip
        self.serial = serial
        self.access_code = access_code

    # -------------------- внутренние helpers --------------------

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
        Подключается к MQTT, подписывается на report и запускает loop_start().
        Возвращает (client, connected_evt).
        """
        topic_report = f"device/{self.serial}/report"
        connected_evt = threading.Event()

        client = self._mqtt_make_client("bambu")

        def on_connect(cl, userdata, flags, rc):
            if rc != 0:
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

        client.connect(self.ip, 8883, keepalive=60)
        client.loop_start()

        if not connected_evt.wait(connect_timeout):
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
            raise RuntimeError("MQTT connect timeout")

        return client, connected_evt

    # -------------------- публичные методы --------------------

    def getStatus(self, timeout: float = 5.0) -> Dict[str, Any]:
        """
        Возвращает последний report от принтера.
        Ждёт хотя бы один report (timeout секунд).
        """
        got_evt = threading.Event()
        last_report: Dict[str, Any] = {}

        def on_report(data: Dict[str, Any]):
            nonlocal last_report
            last_report = data
            got_evt.set()

        client, _ = self._mqtt_connect_and_listen(on_report)

        try:
            if not got_evt.wait(timeout):
                return {"ok": False, "error": "no report received", "report": None}

            pr = last_report.get("print", {})
            gcode_state = pr.get("gcode_state")
            return {
                "ok": True,
                "gcode_state": gcode_state,
                "print": pr,
                "report": last_report,
            }
        finally:
            client.loop_stop()
            client.disconnect()

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
            # pause с sequence_id (важно для многих прошивок)
            cmd = {"print": {"sequence_id": "0", "command": "pause"}}
            info = client.publish(topic_request, json.dumps(cmd), qos=0)
            info.wait_for_publish()

            if paused_evt.wait(timeout):
                return True

            # диагностика при желании:
            # print("[!] No PAUSE confirmation. Last gcode_state =", last_gcode_state["value"])
            # if last_report["value"] is not None:
            #     print("[i] Last report print-block:", last_report["value"].get("print", {}))
            return False

        finally:
            client.loop_stop()
            client.disconnect()

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
            info = client.publish(topic_request, json.dumps(cmd), qos=0)
            info.wait_for_publish()

            return stopped_evt.wait(timeout)

        finally:
            client.loop_stop()
            client.disconnect()

    def start_print(
        self,
        filename_on_sd: str,
        plate_num: int = 1,
        timeout: float = 10.0,
        bed_leveling: bool = False,
        flow_cali: bool = False,
        ams_mapping: list[int] = [0],
        vibration_cali: bool = False,
    ) -> bool:
        """
        Запускает печать файла, который УЖЕ лежит на SD (ты заливаешь через FTP).

        Возвращает True, если увидели, что печать реально пошла (gcode_state RUNNING/PRINTING).
        """
        topic_request = f"device/{self.serial}/request"

        started_evt = threading.Event()

        def on_report(data: Dict[str, Any]):
            pr = data.get("print", {})
            st = pr.get("gcode_state")
            if st and str(st).upper() in ("RUNNING", "PRINTING"):
                started_evt.set()

        client, _ = self._mqtt_connect_and_listen(on_report)

        try:
            cmd = {
                "print": {
                    "sequence_id": "0",
                    "command": "project_file",
                    "param": f"Metadata/plate_{plate_num}.gcode",
                    "url": f"ftp:///{filename_on_sd}",
                    "file": filename_on_sd,
                    "bed_leveling": bed_leveling,
                    "flow_cali": flow_cali,
                    "vibration_cali": vibration_cali,
                    "ams_mapping": list(ams_mapping),
                    "use_ams": bool(False),
                }
            }

            info = client.publish(topic_request, json.dumps(cmd), qos=0)
            info.wait_for_publish()

            return started_evt.wait(timeout)

        finally:
            client.loop_stop()
            client.disconnect()


# -------------------- пример использования --------------------
if __name__ == "__main__":
    p = Printer("192.168.1.130", "00M09D461602386", "241cf96e")

    print("STATUS:", p.status())
    print("START:", p.start_print("AI.gcode.3mf", plate_num=1))
    time.sleep(5)
    print("PAUSE:", p.pause())
    time.sleep(3)
    print("STOP:", p.stop())
    print("STATUS:", p.status())
