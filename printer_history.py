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

    def _coerce_grams(self, value: Any, default: float = 0.0) -> float:
        try:
            return round(max(0.0, float(value)), 2)
        except (TypeError, ValueError):
            return round(max(0.0, float(default)), 2)

    def ensure_filament_remaining(self, pid: str, grams: float = 1000.0) -> float:
        remaining = self._coerce_grams(grams, 1000.0)
        with self._lock:
            rec = self._data.setdefault(pid, {})
            current = rec.get("filament_remaining_g")
            if current is not None:
                return self._coerce_grams(current, remaining)

            rec["filament_remaining_g"] = remaining
            self._atomic_save()
            return remaining

    def get_filament_remaining(self, pid: str, default: float = 1000.0) -> float:
        with self._lock:
            current = (self._data.get(pid) or {}).get("filament_remaining_g")
            if current is None:
                return self._coerce_grams(default, 1000.0)
            return self._coerce_grams(current, default)

    def get_material_override(self, pid: str) -> Optional[str]:
        with self._lock:
            value = (self._data.get(pid) or {}).get("loaded_material_override")
            if value is None:
                return None
            normalized = str(value).strip()
            return normalized or None

    def set_material_override(self, pid: str, material: Optional[str]) -> Optional[str]:
        normalized = str(material or "").strip()
        with self._lock:
            rec = self._data.setdefault(pid, {})
            if normalized:
                rec["loaded_material_override"] = normalized
            else:
                rec.pop("loaded_material_override", None)
            self._atomic_save()
            return normalized or None

    def set_filament_remaining(self, pid: str, grams: float) -> float:
        remaining = self._coerce_grams(grams, 0.0)
        with self._lock:
            rec = self._data.setdefault(pid, {})
            rec["filament_remaining_g"] = remaining
            self._atomic_save()
            return remaining

    def consume_filament(self, pid: str, grams: float) -> float:
        used = self._coerce_grams(grams, 0.0)
        with self._lock:
            rec = self._data.setdefault(pid, {})
            current = self._coerce_grams(rec.get("filament_remaining_g"), 1000.0)
            remaining = round(max(0.0, current - used), 2)
            rec["filament_remaining_g"] = remaining
            self._atomic_save()
            return remaining

    def set_started(self, pid: str, filename: str) -> None:
        now = time.time()
        with self._lock:
            rec = self._data.setdefault(pid, {})
            rec["last_started"] = filename
            rec["last_started_ts"] = now
            rec["current_file"] = filename
            rec["current_ts"] = now
            self._atomic_save()

    def note_report(
        self,
        pid: str,
        ok: Optional[bool],
        gcode_state: Optional[str],
        file_hint: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """
        Вызывать на каждый status/report (из MQTT manager).
        На диск пишем ТОЛЬКО если произошло важное событие (например FINISH).
        """
        if ok is False:
            return None  # offline-метки не пишем на диск

        if not gcode_state:
            return None

        st = str(gcode_state).upper()
        now = time.time()
        event: Optional[dict[str, Any]] = None

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
                    event = {"event": "PRINT_FINISHED", "file": finished_file}

                # текущую работу сбрасываем
                rec.pop("current_file", None)
                rec.pop("current_ts", None)

                #сохраняем на диск только на FINISH/IDLE-переходе
                self._atomic_save()

        return event
