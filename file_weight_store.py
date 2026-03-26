import json
import os
import threading
from pathlib import Path
from typing import Optional


class FileWeightStore:
    def __init__(self, path: str = "file_weights.json"):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self._data = {}
            return

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle) or {}
        except Exception:
            self._data = {}
            return

        normalized: dict[str, float] = {}
        for key, value in raw.items():
            filename = self._normalize_filename(key)
            if not filename:
                continue
            try:
                normalized[filename] = round(max(0.0, float(value)), 2)
            except (TypeError, ValueError):
                continue

        self._data = normalized

    def _atomic_save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def _normalize_filename(self, filename: str) -> str:
        normalized = str(filename or "").strip().replace("\\", "/")
        if not normalized:
            return ""
        return Path(normalized).name

    def get_weight(self, filename: str) -> Optional[float]:
        key = self._normalize_filename(filename)
        if not key:
            return None

        with self._lock:
            value = self._data.get(key)
            return float(value) if value is not None else None

    def set_weight(self, filename: str, grams: float) -> Optional[float]:
        key = self._normalize_filename(filename)
        if not key:
            return None

        try:
            value = round(max(0.0, float(grams)), 2)
        except (TypeError, ValueError):
            return None

        with self._lock:
            self._data[key] = value
            self._atomic_save()

        return value
