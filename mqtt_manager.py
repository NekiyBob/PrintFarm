import json
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from filament_detector import LoadFilamentDetector


@dataclass(frozen=True)
class PrinterCfg:
    id: str
    ip: str
    serial: str
    access_code: str
    model: str = ""


ACTIVE_STATES = {"RUNNING", "PRINTING", "PAUSE", "PAUSED", "PREPARE", "PREPARING"}


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _safe_int(value: Any) -> Optional[int]:
    try:
        if not _has_value(value):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> Optional[float]:
    try:
        if not _has_value(value):
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


class MqttStatusManager:
    def __init__(
        self,
        printers: List[PrinterCfg],
        on_status: Callable[[str, Dict[str, Any]], None],
        offline_after_sec: float = 180.0,
        monitor_interval_sec: float = 3.0,
        keepalive: int = 60,
    ):
        self._printers = printers
        self._on_status = on_status
        self._offline_after = offline_after_sec
        self._monitor_interval = monitor_interval_sec
        self._keepalive = keepalive

        self._clients: Dict[str, mqtt.Client] = {}
        self._last_seen: Dict[str, float] = {}
        self._last_ok: Dict[str, Optional[bool]] = {}
        self._last_status: Dict[str, Dict[str, Any]] = {}
        self._started_at: Dict[str, float] = {}
        self._load_detectors: Dict[str, LoadFilamentDetector] = {}
        self._stop_evt = threading.Event()
        self._mon_thread: Optional[threading.Thread] = None
        self._ctl_lock = threading.RLock()
        self._started = False

    def start(self) -> None:
        with self._ctl_lock:
            if self._started:
                return

            self._stop_evt.clear()
            self._clients.clear()
            self._last_seen.clear()
            self._last_ok.clear()
            self._last_status.clear()
            self._started_at.clear()
            self._load_detectors.clear()

            start_ts = time.time()
            for printer in self._printers:
                if not printer.ip or not printer.serial or not printer.access_code:
                    self._last_ok[printer.id] = False
                    self._emit(printer.id, ok=False, gcode_state=None, error="missing_config")
                    continue

                client = self._make_client(printer)
                self._clients[printer.id] = client
                self._last_ok[printer.id] = None
                self._started_at[printer.id] = start_ts

                client.connect_async(printer.ip, 8883, keepalive=self._keepalive)
                client.loop_start()

            self._mon_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="mqtt-status-monitor",
            )
            self._mon_thread.start()
            self._started = True

    def stop(self) -> None:
        with self._ctl_lock:
            if not self._started and not self._clients:
                return

            self._stop_evt.set()
            monitor_thread = self._mon_thread
            clients = list(self._clients.items())

            self._mon_thread = None
            self._clients = {}
            self._started = False

        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.join(timeout=2.0)

        for _, client in clients:
            try:
                client.loop_stop()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass

        with self._ctl_lock:
            self._last_seen.clear()
            self._last_ok.clear()
            self._last_status.clear()
            self._started_at.clear()
            self._load_detectors.clear()

    def restart(self) -> None:
        with self._ctl_lock:
            self.stop()
            time.sleep(0.3)
            self.start()

    def _make_client(self, printer: PrinterCfg) -> mqtt.Client:
        client_id = f"farm-{printer.id}-{uuid.uuid4()}"
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

        client.username_pw_set("bblp", printer.access_code)
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        client.reconnect_delay_set(min_delay=1, max_delay=20)

        topic_report = f"device/{printer.serial}/report"

        def on_connect(cl, userdata, flags, rc, properties=None):
            if rc == 0:
                cl.subscribe(topic_report)
            else:
                self._emit(printer.id, ok=False, gcode_state=None, error=f"mqtt_connect_rc_{rc}")

        def on_disconnect(cl, userdata, rc, properties=None):
            if self._stop_evt.is_set():
                return

        def on_message(cl, userdata, msg):
            try:
                payload = msg.payload.decode("utf-8", errors="replace")
                data = json.loads(payload)
                normalized = _normalize_report_payload(data)
                pr = normalized.get("print", {}) or {}
                previous = self._last_status.get(printer.id, {})
                detector = self._load_detectors.setdefault(printer.id, LoadFilamentDetector())
                filament_load_event = detector.update(normalized)

                raw_state = pr.get("gcode_state")
                remaining_time = pr.get("mc_remaining_time")
                progress_percent = pr.get("mc_percent")
                current_layer = pr.get("layer_num")
                total_layers = pr.get("total_layer_num")
                nozzle_diameter = pr.get("nozzle_diameter")
                file_hint = (
                    pr.get("subtask_name")
                    or pr.get("file")
                    or pr.get("gcode_file")
                    or pr.get("project_name")
                    or previous.get("file")
                )

                gcode_state = raw_state
                state_upper = str(raw_state or "").upper()
                previous_state = str(previous.get("gcode_state") or "").upper()
                remaining_time_int = _safe_int(remaining_time)
                progress_float = _safe_float(progress_percent)

                if not gcode_state:
                    if remaining_time_int is not None and remaining_time_int > 0:
                        gcode_state = previous_state if previous_state in ACTIVE_STATES else "RUNNING"
                    elif progress_float is not None and 0 < progress_float < 100:
                        gcode_state = previous_state if previous_state in ACTIVE_STATES else "RUNNING"
                    elif remaining_time_int == 0 and previous_state in ACTIVE_STATES:
                        gcode_state = "FINISH"
                    elif progress_float is not None and progress_float >= 100 and previous_state in ACTIVE_STATES:
                        gcode_state = "FINISH"
                    elif str(printer.model or "").upper() == "P1S" and previous_state in ACTIVE_STATES:
                        gcode_state = previous_state

                if not _has_value(progress_percent):
                    progress_percent = previous.get("progress_percent")
                if not _has_value(current_layer):
                    current_layer = previous.get("current_layer")
                if not _has_value(total_layers):
                    total_layers = previous.get("total_layers")
                if not _has_value(remaining_time):
                    remaining_time = previous.get("remaining_time_min")
                if not _has_value(nozzle_diameter):
                    nozzle_diameter = previous.get("nozzle_diameter")

                hms_list = normalized.get("hms") or pr.get("hms") or []
                print_error = pr.get("print_error") or previous.get("print_error")

                hms_codes = []
                for item in hms_list:
                    if isinstance(item, dict):
                        code = item.get("code") or item.get("hms_code") or item.get("hms") or item.get("id")
                        if code is not None:
                            hms_codes.append(str(code))
                    else:
                        hms_codes.append(str(item))

                def _norm(code: str) -> str:
                    return "".join(ch for ch in code.upper() if ch in "0123456789ABCDEF")

                filament_runout = False
                for code in hms_codes:
                    normalized = _norm(code)
                    if normalized == _norm("07FE-7000-0002-0003") or normalized == _norm("0700-2000-0002-0001"):
                        filament_runout = True
                        break

                self._last_seen[printer.id] = time.time()

                self._emit(
                    printer.id,
                    ok=True,
                    gcode_state=gcode_state,
                    error="filament_runout" if filament_runout else None,
                    file=file_hint,
                    progress_percent=progress_percent,
                    current_layer=current_layer,
                    total_layers=total_layers,
                    remaining_time_min=remaining_time,
                    nozzle_diameter=nozzle_diameter,
                    hms=hms_codes,
                    print_error=print_error,
                    filament_load_event=filament_load_event,
                )
            except Exception as exc:
                self._emit(printer.id, ok=False, gcode_state=None, error=f"bad_report: {exc}")

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        return client

    def _monitor_loop(self) -> None:
        while not self._stop_evt.is_set():
            now = time.time()

            for printer in self._printers:
                pid = printer.id
                last_seen = self._last_seen.get(pid)

                if last_seen is None:
                    started_at = self._started_at.get(pid)
                    if started_at is None:
                        continue

                    if (now - started_at) > self._offline_after and self._last_ok.get(pid) is not False:
                        self._emit(pid, ok=False, gcode_state=None, error="stale_no_report")
                    continue

                if (now - last_seen) > self._offline_after and self._last_ok.get(pid) is not False:
                    self._emit(pid, ok=False, gcode_state=None, error="stale_no_report")

            self._stop_evt.wait(self._monitor_interval)

    def _emit(
        self,
        pid: str,
        ok: Optional[bool],
        gcode_state: Optional[str],
        error: Optional[str],
        file=None,
        progress_percent=None,
        current_layer=None,
        total_layers=None,
        remaining_time_min=None,
        nozzle_diameter=None,
        hms=None,
        print_error=None,
        filament_load_event: Optional[str] = None,
    ) -> None:
        self._last_ok[pid] = ok
        status = {
            "id": pid,
            "ok": ok,
            "gcode_state": gcode_state,
            "error": error,
            "file": file,
            "progress_percent": progress_percent,
            "current_layer": current_layer,
            "total_layers": total_layers,
            "remaining_time_min": remaining_time_min,
            "nozzle_diameter": nozzle_diameter,
            "hms": hms or [],
            "print_error": print_error,
            "ts": time.time(),
        }
        if filament_load_event:
            status["filament_load_event"] = filament_load_event
        if ok:
            stored_status = dict(status)
            stored_status.pop("filament_load_event", None)
            self._last_status[pid] = stored_status
        self._on_status(pid, status)
