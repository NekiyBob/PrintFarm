# printer_history.py
import json
import os
import time
import threading
from typing import Dict, Any, Optional


class PrinterHistory:
    """
    Хранит для каждого принтера:
      - last_started: последний файл, который мы запускали
      - last_printed: последний файл, который считается напечатанным (после FINISH)
      - current_file: файл, который мы считаем "текущим" (между стартом и финишем)
      - last_gcode_state: последний gcode_state (для переходов)
    И всё это сохраняет в JSON на диск.
    """

    def __init__(self, path: str = "printer_history.json"):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._data = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f) or {}
        except Exception:
            self._data = {}

    def _atomic_save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def get_last_printed(self, pid: str) -> Optional[str]:
        with self._lock:
            return (self._data.get(pid) or {}).get("last_printed")

    def get_last_printed_ts(self, pid: str) -> Optional[float]:
        with self._lock:
            value = (self._data.get(pid) or {}).get("last_printed_ts")
            return float(value) if value is not None else None

    def set_started(self, pid: str, filename: str) -> None:
        now = time.time()
        with self._lock:
            rec = self._data.setdefault(pid, {})
            rec["last_started"] = filename
            rec["last_started_ts"] = now
            rec["current_file"] = filename
            rec["current_ts"] = now
            self._atomic_save()

    def note_report(self, pid: str, ok: Optional[bool], gcode_state: Optional[str], file_hint: Optional[str] = None) -> None:
        """
        Вызывать на каждый status/report (из MQTT manager).
        На диск пишем ТОЛЬКО если произошло важное событие (например FINISH).
        """
        if ok is False:
            return  # offline-метки не пишем на диск

        if not gcode_state:
            return

        st = str(gcode_state).upper()
        now = time.time()

        with self._lock:
            rec = self._data.setdefault(pid, {})
            prev = (rec.get("last_gcode_state") or "").upper()
            rec["last_gcode_state"] = st

            # если принтер печатает, а мы не знаем current_file,
            # пробуем хотя бы сохранить file_hint (н это /data/Metadata/plate_1.gcode)
            if st in ("RUNNING", "PRINTING") and not rec.get("current_file") and file_hint:
                rec["current_file"] = file_hint
                rec["current_ts"] = now
                # не сохраняем на диск ради этого

            # если был RUNNING/PAUSE и стал FINISH/IDLE -> считаем, что печать закончилась
            if st in ("FINISH", "IDLE") and prev in ("RUNNING", "PRINTING", "PAUSE", "PAUSED"):
                # берём имя файла: current_file -> last_started -> file_hint
                finished_file = rec.get("current_file") or rec.get("last_started") or file_hint
                if finished_file:
                    rec["last_printed"] = finished_file
                    rec["last_printed_ts"] = now

                # текущую работу сбрасываем
                rec.pop("current_file", None)
                rec.pop("current_ts", None)

                #сохраняем на диск только на FINISH/IDLE-переходе
                self._atomic_save()
