import atexit
import json
import os
import random
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from flask import Flask, jsonify, render_template, request, send_file

from file_weight_store import FileWeightStore
from maintenance_db import init_maintenance_db
from maintenance_models import MAINTENANCE_TIMEZONE, MaintenanceEvent, MaintenanceEventType, maintenance_now
from maintenance_service import (
    create_maintenance_event,
    get_farm_maintenance_history,
    get_printer_maintenance_history,
)
from mqtt_manager import MqttStatusManager, PrinterCfg
from printer_client import start_print_on_printer, upload_file_to_printer
from printer_history import PrinterHistory
from printer_lan import Printer
from project_weight import get_total_weight_from_gcode_3mf
from remote_agent_store import RemoteAgentStore
from status_utils import extract_loaded_material_from_payload


UPLOAD_CONCURRENCY = 3
RESTART_CONCURRENCY = 3
DEFAULT_UPLOAD_WORKERS = 12
DEFAULT_RESTART_WORKERS = 12
DEFAULT_METADATA_WORKERS = 8
JOB_RETENTION_SEC = 6 * 60 * 60
COMMAND_RETENTION_SEC = 24 * 60 * 60
FINISH_HIGHLIGHT_SEC = 15 * 60
MQTT_OFFLINE_AFTER_SEC = 180.0
DEFAULT_FILAMENT_REMAINING_G = 1000
DEFAULT_P1S_NOZZLE_DIAMETER = "0.4"
P1S_NOZZLE_DIAMETER_OPTIONS = ("0.2", "0.4", "0.6", "0.8")

UPLOAD_SEM = threading.Semaphore(UPLOAD_CONCURRENCY)
RESTART_SEM = threading.Semaphore(RESTART_CONCURRENCY)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "printers.yaml"
HISTORY_PATH = BASE_DIR / "printer_history.json"
FILE_WEIGHTS_PATH = BASE_DIR / "file_weights.json"
MAINTENANCE_DB_PATH = BASE_DIR / "maintance"
JOBS_DIR = BASE_DIR / "jobs"
REMOTE_STATE_DIR = BASE_DIR / "remote_state"

APP_ROLE = str(os.environ.get("PRINTFARM_ROLE") or "standalone").strip().lower()
LOCAL_PRINTER_IO_ENABLED = APP_ROLE != "server"
AGENT_SHARED_TOKEN = str(os.environ.get("PRINTFARM_AGENT_TOKEN") or "").strip()
AGENT_AUTH_REQUIRED = APP_ROLE == "server" or bool(AGENT_SHARED_TOKEN)

STATUS_CACHE: dict[str, dict[str, Any]] = {}
STATUS_LOCK = threading.Lock()

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
JOB_COMPLETIONS: dict[str, set[str]] = {}

MQTT_MANAGER: Optional[MqttStatusManager] = None
MQTT_MANAGER_LOCK = threading.Lock()
PRINTER_METADATA_REFRESH_RUNNING = False
PRINTER_METADATA_REFRESH_LOCK = threading.Lock()


def _load_printers_config(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    printers = cfg.get("printers")
    if not isinstance(printers, list):
        raise RuntimeError("printers.yaml must contain a 'printers' list")

    normalized: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    for index, printer in enumerate(printers, start=1):
        if not isinstance(printer, dict):
            raise RuntimeError(f"Printer entry #{index} must be a mapping")

        pid = str(printer.get("id", "")).strip()
        if not pid:
            raise RuntimeError(f"Printer entry #{index} is missing a non-empty 'id'")
        if pid in by_id:
            raise RuntimeError(f"Duplicate printer id in config: {pid}")

        normalized_printer = dict(printer)
        normalized_printer["id"] = pid
        normalized.append(normalized_printer)
        by_id[pid] = normalized_printer

    return normalized, by_id


PRINTERS, PRINTERS_BY_ID = _load_printers_config(CONFIG_PATH)
HISTORY = PrinterHistory(str(HISTORY_PATH))
FILE_WEIGHTS = FileWeightStore(str(FILE_WEIGHTS_PATH))
MAINTENANCE_ENGINE = init_maintenance_db(MAINTENANCE_DB_PATH)
REMOTE_STORE = RemoteAgentStore(REMOTE_STATE_DIR)
STATUS_CACHE.update(REMOTE_STORE.list_statuses())


def _ensure_filament_history_defaults() -> None:
    for printer in PRINTERS:
        HISTORY.ensure_filament_remaining(printer["id"], DEFAULT_FILAMENT_REMAINING_G)


_ensure_filament_history_defaults()


def _reload_printers_config() -> None:
    global PRINTERS, PRINTERS_BY_ID
    PRINTERS, PRINTERS_BY_ID = _load_printers_config(CONFIG_PATH)
    _ensure_filament_history_defaults()


def get_printer(printer_id: str) -> Optional[dict[str, Any]]:
    return PRINTERS_BY_ID.get(printer_id)


def _is_printer_configured(printer: dict[str, Any]) -> bool:
    if "configured" in printer:
        return bool(printer.get("configured"))
    return bool(printer.get("ip") and printer.get("serial") and printer.get("access_code"))


def _sanitize_printer(printer: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": printer["id"],
        "row": printer.get("row"),
        "rack": printer.get("rack"),
        "level": printer.get("level"),
        "slot": printer.get("slot"),
        "model": printer.get("model"),
        "name": printer.get("name"),
        "configured": _is_printer_configured(printer),
    }


def _extract_bearer_token() -> str:
    header = str(request.headers.get("Authorization") or "").strip()
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


def _require_agent_auth() -> Optional[tuple[Any, int]]:
    if not AGENT_AUTH_REQUIRED:
        return None

    if not AGENT_SHARED_TOKEN:
        return jsonify({"error": "agent_token_not_configured"}), 500

    if _extract_bearer_token() == AGENT_SHARED_TOKEN:
        return None

    return jsonify({"error": "unauthorized"}), 401


def _queue_printer_command(
    command_type: str,
    *,
    printer_id: str,
    payload: Optional[dict[str, Any]] = None,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    return REMOTE_STORE.create_command(
        command_type,
        printer_id=printer_id,
        payload=payload,
        job_id=job_id,
        scope="printer",
    )


def _queue_farm_command(
    command_type: str,
    *,
    payload: Optional[dict[str, Any]] = None,
    job_id: Optional[str] = None,
) -> dict[str, Any]:
    return REMOTE_STORE.create_command(
        command_type,
        payload=payload,
        job_id=job_id,
        scope="farm",
    )


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _normalize_status_payload_for_model(
    printer: dict[str, Any],
    status_payload: Optional[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(status_payload or {"ok": False, "error": "no_status"})
    print_block = dict(payload.get("print") or {})
    payload["print"] = print_block

    model = str(printer.get("model") or "").strip().upper()
    gcode_state = payload.get("gcode_state") or print_block.get("gcode_state")

    if model == "P1S" and not gcode_state:
        remaining_time = _safe_int(print_block.get("mc_remaining_time"))
        progress_percent = _safe_float(print_block.get("mc_percent"))
        if remaining_time is not None and remaining_time > 0:
            payload["gcode_state"] = "RUNNING"
            print_block["gcode_state"] = "RUNNING"
        elif progress_percent is not None and 0 < progress_percent < 100:
            payload["gcode_state"] = "RUNNING"
            print_block["gcode_state"] = "RUNNING"

    return payload


def _normalize_runtime_status_for_printer(
    printer: dict[str, Any],
    status: Optional[dict[str, Any]],
) -> dict[str, Any]:
    normalized = dict(status or {})
    gcode_state = normalized.get("gcode_state")
    remaining_time = _safe_int(normalized.get("remaining_time_min"))
    progress_percent = _safe_float(normalized.get("progress_percent"))

    if not gcode_state:
        if remaining_time is not None and remaining_time > 0:
            gcode_state = "RUNNING"
        elif progress_percent is not None and 0 < progress_percent < 100:
            gcode_state = "RUNNING"

    normalized["gcode_state"] = gcode_state
    return normalized


def _extract_loaded_material(
    payload: Optional[dict[str, Any]],
    *,
    cached_status: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    return extract_loaded_material_from_payload(
        payload,
        cached_status=cached_status,
    )


def _resolve_loaded_material(
    printer_id: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    cached_status: Optional[dict[str, Any]] = None,
    fallback_value: Optional[str] = None,
) -> Optional[str]:
    return (
        HISTORY.get_material_override(printer_id)
        or _extract_loaded_material(payload, cached_status=cached_status)
        or fallback_value
    )


def _normalize_nozzle_diameter_value(value: Any) -> Optional[str]:
    if value is None:
        return None

    normalized = str(value).strip().replace(",", ".")
    if not normalized or normalized == "—":
        return None

    try:
        return f"{float(normalized):.1f}"
    except (TypeError, ValueError):
        return normalized


def _resolve_nozzle_diameter_value(
    model: str,
    *,
    reported_value: Any = None,
    override_value: Any = None,
    fallback_value: Any = None,
) -> Optional[str]:
    normalized_model = str(model or "").strip().upper()
    normalized_reported = _normalize_nozzle_diameter_value(reported_value)
    normalized_fallback = _normalize_nozzle_diameter_value(fallback_value)

    if normalized_model == "P1S":
        normalized_override = _normalize_nozzle_diameter_value(override_value)
        return normalized_override or normalized_reported or normalized_fallback or DEFAULT_P1S_NOZZLE_DIAMETER

    return normalized_reported or normalized_fallback


def _resolve_nozzle_diameter(
    printer: dict[str, Any],
    payload: Optional[dict[str, Any]] = None,
    *,
    cached_status: Optional[dict[str, Any]] = None,
    fallback_value: Any = None,
) -> Optional[str]:
    payload = payload or {}
    print_block = payload.get("print") or {}
    cached_status = cached_status or {}

    reported_value = _coalesce(
        print_block.get("nozzle_diameter"),
        payload.get("nozzle_diameter"),
    )
    cached_value = _coalesce(
        cached_status.get("nozzle_diameter"),
        fallback_value,
    )

    return _resolve_nozzle_diameter_value(
        printer.get("model") or "",
        reported_value=reported_value,
        override_value=HISTORY.get_nozzle_diameter_override(printer["id"]),
        fallback_value=cached_value,
    )


def _is_recent_finish(status: dict[str, Any], last_printed_ts: Optional[float], now: Optional[float] = None) -> bool:
    if str(status.get("gcode_state") or "").upper() != "FINISH":
        return False
    if last_printed_ts is None:
        return False
    return ((now or time.time()) - last_printed_ts) < FINISH_HIGHLIGHT_SEC


def _build_printer_details(
    printer: dict[str, Any],
    status_payload: Optional[dict[str, Any]] = None,
    cached_status: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    configured = _is_printer_configured(printer)
    payload = _normalize_status_payload_for_model(printer, status_payload)
    print_block = payload.get("print") or {}
    cached_status = cached_status or {}
    gcode_state = _coalesce(payload.get("gcode_state"), print_block.get("gcode_state"), cached_status.get("gcode_state"))
    state_upper = str(gcode_state or "").upper()
    active_states = {"RUNNING", "PRINTING", "PAUSE", "PAUSED", "PREPARE", "PREPARING"}
    is_active = state_upper in active_states
    current_file = _coalesce(
        print_block.get("subtask_name"),
        print_block.get("project_name"),
        print_block.get("gcode_file"),
        cached_status.get("file"),
    )

    return {
        "id": printer["id"],
        "name": printer.get("name"),
        "ip": printer.get("ip"),
        "model": printer.get("model"),
        "configured": configured,
        "status": {
            "ok": payload.get("ok"),
            "error": payload.get("error"),
            "gcode_state": gcode_state,
            "is_active_print": is_active,
            "current_file": current_file if is_active else None,
            "current_layer": _coalesce(_safe_int(print_block.get("layer_num")), _safe_int(cached_status.get("current_layer"))),
            "total_layers": _coalesce(_safe_int(print_block.get("total_layer_num")), _safe_int(cached_status.get("total_layers"))),
            "remaining_time_min": _coalesce(_safe_int(print_block.get("mc_remaining_time")), _safe_int(cached_status.get("remaining_time_min"))),
            "progress_percent": _coalesce(_safe_float(print_block.get("mc_percent")), _safe_float(cached_status.get("progress_percent"))),
            "nozzle_temp": _safe_float(print_block.get("nozzle_temper")),
            "nozzle_target_temp": _safe_float(print_block.get("nozzle_target_temper")),
            "bed_temp": _safe_float(print_block.get("bed_temper")),
            "bed_target_temp": _safe_float(print_block.get("bed_target_temper")),
            "nozzle_diameter": _resolve_nozzle_diameter(
                printer,
                payload,
                cached_status=cached_status,
            ),
            "loaded_material": _resolve_loaded_material(
                printer["id"],
                payload,
                cached_status=cached_status,
            ),
            "filament_remaining_g": HISTORY.get_filament_remaining(printer["id"], DEFAULT_FILAMENT_REMAINING_G),
            "can_pause": state_upper in {"RUNNING", "PRINTING", "PREPARE", "PREPARING"},
            "can_resume": state_upper in {"PAUSE", "PAUSED"},
            "can_stop": state_upper in active_states,
        },
    }


def _build_printer_client(printer: dict[str, Any]) -> Printer:
    return Printer(
        ip=printer["ip"],
        serial=printer["serial"],
        access_code=printer["access_code"],
        model=printer.get("model") or "",
    )


def _get_printer_or_error(printer_id: str) -> tuple[Optional[dict[str, Any]], Optional[tuple[Any, int]]]:
    printer = get_printer(printer_id)
    if not printer:
        return None, (jsonify({"error": "printer_not_found"}), 404)

    return printer, None


def _require_configured_printer(printer_id: str) -> tuple[Optional[dict[str, Any]], Optional[tuple[Any, int]]]:
    printer, error_response = _get_printer_or_error(printer_id)
    if error_response:
        return None, error_response

    if not (printer.get("ip") and printer.get("serial") and printer.get("access_code")):
        return None, (jsonify({"error": "missing_config"}), 400)

    return printer, None


def _require_remote_printer(printer_id: str) -> tuple[Optional[dict[str, Any]], Optional[tuple[Any, int]]]:
    printer, error_response = _get_printer_or_error(printer_id)
    if error_response:
        return None, error_response

    if not _is_printer_configured(printer):
        return None, (jsonify({"error": "missing_config"}), 400)

    return printer, None


def _normalize_printer_ids(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, list):
        raise ValueError("printers must be a JSON array")

    normalized: list[str] = []
    seen: set[str] = set()

    for value in raw_value:
        if not isinstance(value, str):
            raise ValueError("printer ids must be strings")

        pid = value.strip()
        if not pid:
            raise ValueError("printer ids must be non-empty strings")
        if pid in seen:
            continue

        normalized.append(pid)
        seen.add(pid)

    if not normalized:
        raise ValueError("no printers specified")

    return normalized


def _serialize_maintenance_event(event: MaintenanceEvent) -> dict[str, Any]:
    def _numeric_or_none(value: Any) -> Optional[float]:
        if value is None:
            return None
        return float(value)

    return {
        "id": event.id,
        "printer_id": event.printer_id,
        "event_type": event.event_type.value,
        "event_at": event.event_at.isoformat() if event.event_at else None,
        "performed_by": event.performed_by,
        "note": event.note,
        "nozzle_diameter": _numeric_or_none(event.nozzle_diameter),
        "custom_type_name": event.custom_type_name,
        "print_hours_snapshot": _numeric_or_none(event.print_hours_snapshot),
        "print_count_snapshot": event.print_count_snapshot,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _parse_iso_datetime(value: Any, *, field_name: str) -> datetime:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field_name} is required")

    raw_value = str(value).strip()
    if raw_value.endswith("Z"):
        raw_value = f"{raw_value[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO 8601 datetime") from exc

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=MAINTENANCE_TIMEZONE)
    return parsed.astimezone(MAINTENANCE_TIMEZONE)


def _parse_optional_iso_datetime(value: Any, *, field_name: str) -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        return None
    return _parse_iso_datetime(value, field_name=field_name)


def _parse_optional_limit(value: Any) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None

    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc

    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    return limit


def _read_maintenance_filters() -> dict[str, Any]:
    date_from = _parse_optional_iso_datetime(request.args.get("date_from"), field_name="date_from")
    date_to = _parse_optional_iso_datetime(request.args.get("date_to"), field_name="date_to")

    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must be earlier than or equal to date_to")

    return {
        "event_type": request.args.get("event_type"),
        "performed_by": request.args.get("performed_by"),
        "date_from": date_from,
        "date_to": date_to,
        "limit": _parse_optional_limit(request.args.get("limit")),
    }


def _parse_optional_json_text(value: Any, *, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")

    normalized = value.strip()
    return normalized or None


def _build_maintenance_event_payload(data: dict[str, Any]) -> dict[str, Any]:
    raw_event_type = data.get("event_type")
    if not isinstance(raw_event_type, str) or not raw_event_type.strip():
        raise ValueError("event_type is required and must be a non-empty string")

    normalized_event_type = raw_event_type.strip().upper()
    nozzle_diameter = data.get("nozzle_diameter")
    custom_type_name = _parse_optional_json_text(data.get("custom_type_name"), field_name="custom_type_name")

    if normalized_event_type == MaintenanceEventType.NOZZLE_REPLACEMENT.value and nozzle_diameter in (None, ""):
        raise ValueError("nozzle_diameter is required when event_type is NOZZLE_REPLACEMENT")

    if normalized_event_type == MaintenanceEventType.OTHER.value and not custom_type_name:
        raise ValueError("custom_type_name is required when event_type is OTHER")

    return {
        "event_type": normalized_event_type,
        "event_at": _parse_optional_iso_datetime(data.get("event_at"), field_name="event_at")
        or maintenance_now(),
        "performed_by": _parse_optional_json_text(data.get("performed_by"), field_name="performed_by"),
        "note": _parse_optional_json_text(data.get("note"), field_name="note"),
        "nozzle_diameter": nozzle_diameter,
        "custom_type_name": custom_type_name,
        "print_hours_snapshot": data.get("print_hours_snapshot"),
        "print_count_snapshot": data.get("print_count_snapshot"),
    }


def _detect_project_plate_path(temp_path: str) -> Optional[str]:
    lower_name = Path(temp_path).name.lower()
    if not (lower_name.endswith(".3mf") or lower_name.endswith(".gcode.3mf")):
        return None

    try:
        with zipfile.ZipFile(temp_path) as archive:
            candidates: list[tuple[int, str]] = []

            for member in archive.namelist():
                normalized = member.replace("\\", "/")
                lower = normalized.lower()
                if not lower.startswith("metadata/plate_") or not lower.endswith(".gcode"):
                    continue

                plate_number_text = lower[len("metadata/plate_") : -len(".gcode")]
                try:
                    plate_number = int(plate_number_text)
                except ValueError:
                    plate_number = 10**9

                candidates.append((plate_number, normalized))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    for plate_number, normalized in candidates:
        if plate_number == 1:
            return normalized

    return candidates[0][1]


def _cleanup_finished_jobs(now: Optional[float] = None) -> None:
    cutoff = (now or time.time()) - JOB_RETENTION_SEC

    with JOBS_LOCK:
        stale_job_ids = [
            job_id
            for job_id, job in JOBS.items()
            if job.get("finished_ts") and job["finished_ts"] < cutoff
        ]
        for job_id in stale_job_ids:
            JOBS.pop(job_id, None)
            JOB_COMPLETIONS.pop(job_id, None)

    REMOTE_STORE.cleanup_commands(keep_sec=COMMAND_RETENTION_SEC)


def _job_snapshot(job_id: str) -> Optional[dict[str, Any]]:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return deepcopy(job) if job else None


def _job_set(job_id: str, pid: str, **kwargs: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return

        printer_state = job["printers"].setdefault(pid, {})
        printer_state.update(kwargs)


def _job_done(job_id: str, pid: str, ok: bool, message: Optional[str] = None) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return

        completed_printers = JOB_COMPLETIONS.setdefault(job_id, set())
        if pid in completed_printers:
            return
        completed_printers.add(pid)

        printer_state = job["printers"].setdefault(pid, {})
        job["done"] += 1
        if ok:
            job["ok_count"] += 1
        else:
            job["err_count"] += 1

        printer_state["stage"] = "ok" if ok else "error"
        printer_state["message"] = message

        if job["done"] >= job["total"]:
            job["finished"] = True
            job["finished_ts"] = time.time()


def _job_mark_remote_queued(job_id: str, pid: str) -> None:
    _job_set(job_id, pid, stage="queued", message="awaiting_agent")


def _job_apply_remote_progress(
    job_id: str,
    pid: str,
    *,
    stage: Optional[str] = None,
    message: Optional[str] = None,
    file: Optional[str] = None,
    progress_percent: Optional[float] = None,
    ok: Optional[bool] = None,
) -> None:
    fields: dict[str, Any] = {}
    if stage is not None:
        fields["stage"] = stage
    if message is not None:
        fields["message"] = message
    if file is not None:
        fields["file"] = file
    if progress_percent is not None:
        fields["progress_percent"] = max(0.0, min(float(progress_percent), 100.0))
    if fields:
        _job_set(job_id, pid, **fields)

    if stage == "started" and file:
        HISTORY.set_started(pid, file)

    if ok is not None:
        _job_done(job_id, pid, ok=ok, message=message)


def _resolve_job_artifact_path(job_id: str, filename: str) -> Optional[Path]:
    normalized_job_id = str(job_id or "").strip()
    normalized_name = os.path.basename(str(filename or "").strip())
    if not normalized_job_id or not normalized_name:
        return None

    candidate = (JOBS_DIR / normalized_job_id / normalized_name).resolve()
    jobs_root = JOBS_DIR.resolve()
    if candidate.parent != (jobs_root / normalized_job_id):
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def _validate_remote_job_printer(job_id: str, pid: str) -> Optional[dict[str, Any]]:
    printer = get_printer(pid)
    if not printer:
        _job_set(job_id, pid, stage="not_found", message=None)
        _job_done(job_id, pid, ok=False, message="not_found")
        return None

    if not _is_printer_configured(printer):
        _job_set(job_id, pid, stage="missing_config", message=None)
        _job_done(job_id, pid, ok=False, message="missing_config")
        return None

    _job_mark_remote_queued(job_id, pid)
    return printer


def _merge_status_cache(pid: str, **fields: Any) -> None:
    next_status: Optional[dict[str, Any]] = None
    with STATUS_LOCK:
        current = dict(STATUS_CACHE.get(pid) or {"id": pid})
        for key, value in fields.items():
            if value is not None and value != "":
                current[key] = value
        STATUS_CACHE[pid] = current
        next_status = dict(current)
    if next_status is not None:
        REMOTE_STORE.set_status(pid, next_status)


def _refresh_one_printer_metadata(printer: dict[str, Any]) -> None:
    pid = printer["id"]
    _merge_status_cache(pid, model=printer.get("model"))

    if not (printer.get("ip") and printer.get("serial") and printer.get("access_code")):
        return

    try:
        status_payload = _build_printer_client(printer).getStatus(timeout=6.0)
    except Exception:
        return

    print_block = (status_payload or {}).get("print") or {}
    _merge_status_cache(
        pid,
        nozzle_diameter=print_block.get("nozzle_diameter"),
        loaded_material=_extract_loaded_material(status_payload),
        gcode_state=status_payload.get("gcode_state") or print_block.get("gcode_state"),
        file=print_block.get("subtask_name") or print_block.get("project_name") or print_block.get("gcode_file"),
        progress_percent=_safe_float(print_block.get("mc_percent")),
        current_layer=_safe_int(print_block.get("layer_num")),
        total_layers=_safe_int(print_block.get("total_layer_num")),
        remaining_time_min=_safe_int(print_block.get("mc_remaining_time")),
    ) 


def _run_printer_metadata_refresh(max_workers: int = DEFAULT_METADATA_WORKERS) -> None:
    global PRINTER_METADATA_REFRESH_RUNNING

    try:
        worker_count = max(1, min(max_workers, len(PRINTERS)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(_refresh_one_printer_metadata, printer): printer["id"]
                for printer in PRINTERS
            }

            for future in as_completed(future_map):
                try:
                    future.result()
                except Exception:
                    pass
    finally:
        with PRINTER_METADATA_REFRESH_LOCK:
            PRINTER_METADATA_REFRESH_RUNNING = False


def _start_printer_metadata_refresh() -> None:
    global PRINTER_METADATA_REFRESH_RUNNING

    with PRINTER_METADATA_REFRESH_LOCK:
        if PRINTER_METADATA_REFRESH_RUNNING:
            return
        PRINTER_METADATA_REFRESH_RUNNING = True

    thread = threading.Thread(
        target=_run_printer_metadata_refresh,
        daemon=True,
        name="printer-metadata-refresh",
    )
    thread.start()


def retry(fn, tries: int = 3, base_delay: float = 1.0, factor: float = 2.0, on_retry=None):
    last_exc = None

    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt == tries:
                raise

            delay = base_delay * (factor ** (attempt - 1))
            delay += random.uniform(0, 0.35 * delay)

            if on_retry:
                on_retry(attempt, exc, delay)

            time.sleep(delay)

    raise last_exc


def _cleanup_upload_artifacts(temp_path: str) -> None:
    path = Path(temp_path)

    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass

    try:
        if path.parent != JOBS_DIR:
            path.parent.rmdir()
    except OSError:
        pass


def _process_one_printer(
    job_id: str,
    pid: str,
    printer: dict[str, Any],
    temp_path: str,
    original_name: str,
    project_plate_path: Optional[str] = None,
) -> None:
    ip = printer["ip"]
    serial = printer["serial"]
    access_code = printer["access_code"]

    def log_retry(stage: str):
        def _cb(attempt: int, exc: Exception, delay: float) -> None:
            _job_set(job_id, pid, stage=stage, message=f"retry {attempt}: {exc} (sleep {delay:.1f}s)")

        return _cb

    try:
        _job_set(job_id, pid, stage="uploading", message=None, progress_percent=0.0)

        def on_upload_progress(percent, bytes_sent, total_bytes):
            _job_set(job_id, pid, stage="uploading", progress_percent=percent)

        def do_upload():
            with UPLOAD_SEM:
                return upload_file_to_printer(
                    ip,
                    access_code,
                    temp_path,
                    model=printer.get("model") or "",
                    progress_callback=on_upload_progress,
                )

        retry(
            do_upload,
            tries=3,
            base_delay=1.0,
            factor=2.0,
            on_retry=log_retry("uploading"),
        )

        _job_set(job_id, pid, stage="uploaded", message=None, progress_percent=100.0)
        time.sleep(1.5)

        _job_set(job_id, pid, stage="starting", message=None)
        retry(
            lambda: start_print_on_printer(
                ip,
                access_code,
                serial,
                original_name,
                plate_num=1,
                plate_path=project_plate_path,
                model=printer.get("model") or "",
            ),
            tries=3,
            base_delay=1.0,
            factor=2.0,
            on_retry=log_retry("starting"),
        )

        HISTORY.set_started(pid, original_name)

        _job_set(job_id, pid, stage="started", message=None)
        _job_done(job_id, pid, ok=True)

    except Exception as exc:
        _job_done(job_id, pid, ok=False, message=str(exc))


def _upload_only_one_printer(job_id: str, pid: str, printer: dict[str, Any], temp_path: str) -> None:
    ip = printer["ip"]
    access_code = printer["access_code"]

    def log_retry(stage: str):
        def _cb(attempt: int, exc: Exception, delay: float) -> None:
            _job_set(job_id, pid, stage=stage, message=f"retry {attempt}: {exc} (sleep {delay:.1f}s)")

        return _cb

    try:
        _job_set(job_id, pid, stage="uploading", message=None, progress_percent=0.0)

        def on_upload_progress(percent, bytes_sent, total_bytes):
            _job_set(job_id, pid, stage="uploading", progress_percent=percent)

        def do_upload():
            with UPLOAD_SEM:
                return upload_file_to_printer(
                    ip,
                    access_code,
                    temp_path,
                    model=printer.get("model") or "",
                    progress_callback=on_upload_progress,
                )

        retry(
            do_upload,
            tries=3,
            base_delay=1.0,
            factor=2.0,
            on_retry=log_retry("uploading"),
        )

        _job_set(job_id, pid, stage="uploaded", message="saved_to_sd", progress_percent=100.0)
        _job_done(job_id, pid, ok=True, message="saved_to_sd")

    except Exception as exc:
        _job_done(job_id, pid, ok=False, message=str(exc))


def _restart_one_printer(job_id: str, pid: str, printer: dict[str, Any]) -> None:
    ip = printer["ip"]
    serial = printer["serial"]
    access_code = printer["access_code"]
    restart_file = HISTORY.get_last_printed(pid)

    if not restart_file:
        _job_set(job_id, pid, stage="error", message="no_last_printed")
        _job_done(job_id, pid, ok=False, message="no_last_printed")
        return

    def log_retry(stage: str):
        def _cb(attempt: int, exc: Exception, delay: float) -> None:
            _job_set(job_id, pid, stage=stage, message=f"retry {attempt}: {exc} (sleep {delay:.1f}s)")

        return _cb

    try:
        _job_set(job_id, pid, stage="starting", file=restart_file, message=None)

        def do_start():
            with RESTART_SEM:
                return start_print_on_printer(
                    ip,
                    access_code,
                    serial,
                    restart_file,
                    model=printer.get("model") or "",
                )

        retry(
            do_start,
            tries=3,
            base_delay=1.0,
            factor=2.0,
            on_retry=log_retry("starting"),
        )

        HISTORY.set_started(pid, restart_file)

        _job_set(job_id, pid, stage="started", file=restart_file, message=None)
        _job_done(job_id, pid, ok=True)

    except Exception as exc:
        _job_done(job_id, pid, ok=False, message=str(exc))


def _run_restart_job(
    job_id: str,
    printer_ids: list[str],
    max_workers: int = DEFAULT_RESTART_WORKERS,
) -> None:
    valid: list[tuple[str, dict[str, Any]]] = []

    for pid in printer_ids:
        printer = get_printer(pid)
        if not printer:
            _job_set(job_id, pid, stage="not_found", message=None)
            _job_done(job_id, pid, ok=False, message="not_found")
            continue

        ip = printer.get("ip")
        serial = printer.get("serial")
        access_code = printer.get("access_code")
        if not ip or not serial or not access_code:
            _job_set(job_id, pid, stage="missing_config", message=None)
            _job_done(job_id, pid, ok=False, message="missing_config")
            continue

        _job_set(job_id, pid, stage="queued", message=None)
        valid.append((pid, printer))

    if not valid:
        return

    worker_count = max(1, min(max_workers, len(valid)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(_restart_one_printer, job_id, pid, printer): pid
            for pid, printer in valid
        }

        for future in as_completed(future_map):
            try:
                future.result()
            except Exception as exc:
                pid = future_map[future]
                _job_done(job_id, pid, ok=False, message=f"worker crash: {exc}")


def _run_job(
    job_id: str,
    printer_ids: list[str],
    temp_path: str,
    original_name: str,
    project_plate_path: Optional[str] = None,
    max_workers: int = DEFAULT_UPLOAD_WORKERS,
) -> None:
    valid: list[tuple[str, dict[str, Any]]] = []

    try:
        for pid in printer_ids:
            printer = get_printer(pid)
            if not printer:
                _job_set(job_id, pid, stage="not_found", message=None)
                _job_done(job_id, pid, ok=False, message="not_found")
                continue

            ip = printer.get("ip")
            serial = printer.get("serial")
            access_code = printer.get("access_code")
            if not ip or not serial or not access_code:
                _job_set(job_id, pid, stage="missing_config", message=None)
                _job_done(job_id, pid, ok=False, message="missing_config")
                continue

            _job_set(job_id, pid, stage="queued", message=None)
            valid.append((pid, printer))

        if not valid:
            return

        worker_count = max(1, min(max_workers, len(valid)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    _process_one_printer,
                    job_id,
                    pid,
                    printer,
                    temp_path,
                    original_name,
                    project_plate_path,
                ): pid
                for pid, printer in valid
            }

            for future in as_completed(future_map):
                try:
                    future.result()
                except Exception as exc:
                    pid = future_map[future]
                    _job_done(job_id, pid, ok=False, message=f"worker crash: {exc}")
    finally:
        _cleanup_upload_artifacts(temp_path)


def _run_upload_only_job(
    job_id: str,
    printer_ids: list[str],
    temp_path: str,
    max_workers: int = DEFAULT_UPLOAD_WORKERS,
) -> None:
    valid: list[tuple[str, dict[str, Any]]] = []

    try:
        for pid in printer_ids:
            printer = get_printer(pid)
            if not printer:
                _job_set(job_id, pid, stage="not_found", message=None)
                _job_done(job_id, pid, ok=False, message="not_found")
                continue

            ip = printer.get("ip")
            serial = printer.get("serial")
            access_code = printer.get("access_code")
            if not ip or not serial or not access_code:
                _job_set(job_id, pid, stage="missing_config", message=None)
                _job_done(job_id, pid, ok=False, message="missing_config")
                continue

            _job_set(job_id, pid, stage="queued", message=None)
            valid.append((pid, printer))

        if not valid:
            return

        worker_count = max(1, min(max_workers, len(valid)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(_upload_only_one_printer, job_id, pid, printer, temp_path): pid
                for pid, printer in valid
            }

            for future in as_completed(future_map):
                try:
                    future.result()
                except Exception as exc:
                    pid = future_map[future]
                    _job_done(job_id, pid, ok=False, message=f"worker crash: {exc}")
    finally:
        _cleanup_upload_artifacts(temp_path)


def _on_status(pid: str, status: dict[str, Any]) -> None:
    status_for_cache = dict(status)
    filament_load_event = status_for_cache.pop("filament_load_event", None)
    filament_remaining_g = HISTORY.get_filament_remaining(pid, DEFAULT_FILAMENT_REMAINING_G)

    if filament_load_event == "LOAD_FILAMENT_STARTED":
        print(f"[LOAD FILAMENT] {pid}")

    if filament_load_event == "LOAD_FILAMENT_FINISHED":
        filament_remaining_g = HISTORY.set_filament_remaining(pid, DEFAULT_FILAMENT_REMAINING_G)

    status_for_cache["filament_remaining_g"] = filament_remaining_g

    with STATUS_LOCK:
        STATUS_CACHE[pid] = status_for_cache
    REMOTE_STORE.set_status(pid, status_for_cache)

    history_event = HISTORY.note_report(
        pid=pid,
        ok=status.get("ok"),
        gcode_state=status.get("gcode_state"),
        file_hint=status.get("file"),
    )

    if history_event and history_event.get("event") == "PRINT_FINISHED":
        finished_file = history_event.get("file")
        weight_g = FILE_WEIGHTS.get_weight(str(finished_file or ""))
        if weight_g is not None and weight_g > 0:
            remaining_after_print = HISTORY.consume_filament(pid, weight_g)
            with STATUS_LOCK:
                current = dict(STATUS_CACHE.get(pid) or {"id": pid})
                current["filament_remaining_g"] = remaining_after_print
                STATUS_CACHE[pid] = current
            REMOTE_STORE.set_status(pid, STATUS_CACHE.get(pid) or {"id": pid})


def _build_mqtt_manager() -> MqttStatusManager:
    printer_cfgs = [
        PrinterCfg(
            id=printer["id"],
            ip=printer.get("ip") or "",
            serial=printer.get("serial") or "",
            access_code=printer.get("access_code") or "",
            model=printer.get("model") or "",
        )
        for printer in PRINTERS
    ]

    return MqttStatusManager(
        printers=printer_cfgs,
        on_status=_on_status,
        offline_after_sec=MQTT_OFFLINE_AFTER_SEC,
        monitor_interval_sec=2.0,
        keepalive=60,
    )


def _ensure_mqtt_manager() -> MqttStatusManager:
    global MQTT_MANAGER
    created = False

    with MQTT_MANAGER_LOCK:
        if MQTT_MANAGER is None:
            MQTT_MANAGER = _build_mqtt_manager()
            MQTT_MANAGER.start()
            created = True

        manager = MQTT_MANAGER

    if created:
        _start_printer_metadata_refresh()

    return manager


def _stop_mqtt_manager() -> None:
    global MQTT_MANAGER

    with MQTT_MANAGER_LOCK:
        manager = MQTT_MANAGER
        MQTT_MANAGER = None

    if manager is not None:
        manager.stop()


atexit.register(_stop_mqtt_manager)


app = Flask(__name__)


@app.get("/api/status")
def api_status():
    _cleanup_finished_jobs()
    if LOCAL_PRINTER_IO_ENABLED:
        _ensure_mqtt_manager()
    now = time.time()

    with STATUS_LOCK:
        cache = deepcopy(STATUS_CACHE)

    statuses = []
    for printer in PRINTERS:
        pid = printer["id"]
        status = cache.get(pid, {"id": pid, "ok": None, "gcode_state": "NONE"})
        status = _normalize_runtime_status_for_printer(printer, status)
        status["model"] = printer.get("model")
        status["nozzle_diameter"] = _resolve_nozzle_diameter(
            printer,
            cached_status=status,
            fallback_value=status.get("nozzle_diameter"),
        )
        status["loaded_material"] = _resolve_loaded_material(
            pid,
            cached_status=status,
            fallback_value=status.get("loaded_material"),
        )
        status["filament_remaining_g"] = HISTORY.get_filament_remaining(pid, DEFAULT_FILAMENT_REMAINING_G)
        status["last_printed"] = HISTORY.get_last_printed(pid)
        status["last_printed_ts"] = HISTORY.get_last_printed_ts(pid)
        status["finish_recent"] = _is_recent_finish(status, status["last_printed_ts"], now=now)
        statuses.append(status)

    return jsonify({"statuses": statuses})


@app.get("/api/jobs/<job_id>")
def api_job_status(job_id: str):
    _cleanup_finished_jobs()
    job = _job_snapshot(job_id)
    if job is None:
        return jsonify({"error": "job_not_found"}), 404

    return jsonify(job)


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/printers")
def api_printers():
    _reload_printers_config()
    return jsonify([_sanitize_printer(printer) for printer in PRINTERS])


@app.get("/printers/<printer_id>/maintenance")
def api_printer_maintenance_history(printer_id: str):
    printer, error_response = _get_printer_or_error(printer_id)
    if error_response:
        return error_response

    try:
        filters = _read_maintenance_filters()
        items = get_printer_maintenance_history(printer["id"], **filters)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(
        {
            "printer_id": printer["id"],
            "items": [_serialize_maintenance_event(event) for event in items],
        }
    )


@app.post("/printers/<printer_id>/maintenance")
def api_create_printer_maintenance_event(printer_id: str):
    printer, error_response = _get_printer_or_error(printer_id)
    if error_response:
        return error_response

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    try:
        payload = _build_maintenance_event_payload(data)
        event = create_maintenance_event(
            printer_id=printer["id"],
            **payload,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"item": _serialize_maintenance_event(event)}), 201


@app.get("/maintenance")
def api_farm_maintenance_history():
    printer_id = request.args.get("printer_id")
    if printer_id:
        printer, error_response = _get_printer_or_error(printer_id)
        if error_response:
            return error_response
        normalized_printer_id = printer["id"]
    else:
        normalized_printer_id = None

    try:
        filters = {"printer_id": normalized_printer_id, **_read_maintenance_filters()}
        items = get_farm_maintenance_history(**filters)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(
        {
            "filters": {
                "printer_id": normalized_printer_id,
                "event_type": request.args.get("event_type"),
                "performed_by": request.args.get("performed_by"),
                "date_from": request.args.get("date_from"),
                "date_to": request.args.get("date_to"),
                "limit": filters["limit"],
            },
            "items": [_serialize_maintenance_event(event) for event in items],
        }
    )


@app.get("/api/printers/<printer_id>/details")
def api_printer_details(printer_id: str):
    printer, error_response = _get_printer_or_error(printer_id)
    if error_response:
        return error_response

    with STATUS_LOCK:
        cached_status = deepcopy(STATUS_CACHE.get(printer_id))

    if not LOCAL_PRINTER_IO_ENABLED:
        return jsonify(_build_printer_details(printer, cached_status=cached_status))

    if not (printer.get("ip") and printer.get("serial") and printer.get("access_code")):
        return jsonify(_build_printer_details(printer, {"ok": False, "error": "missing_config"}, cached_status=cached_status))

    try:
        status_payload = _build_printer_client(printer).getStatus(timeout=8.0)
    except Exception as exc:
        status_payload = {"ok": False, "error": str(exc)}

    return jsonify(_build_printer_details(printer, status_payload, cached_status=cached_status))


@app.post("/api/printers/<printer_id>/material")
def api_set_printer_material(printer_id: str):
    printer, error_response = _get_printer_or_error(printer_id)
    if error_response:
        return error_response

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    material = str(data.get("material") or "").strip()
    if len(material) > 120:
        return jsonify({"error": "material must be at most 120 characters"}), 400

    saved_override = HISTORY.set_material_override(printer["id"], material)
    with STATUS_LOCK:
        cached_status = deepcopy(STATUS_CACHE.get(printer["id"]))

    effective_material = _resolve_loaded_material(
        printer["id"],
        cached_status=cached_status,
    )

    return jsonify(
        {
            "ok": True,
            "printer_id": printer["id"],
            "loaded_material": effective_material,
            "material_override": saved_override,
        }
    )


@app.post("/api/printers/<printer_id>/nozzle_diameter")
def api_set_printer_nozzle_diameter(printer_id: str):
    printer, error_response = _get_printer_or_error(printer_id)
    if error_response:
        return error_response

    if str(printer.get("model") or "").strip().upper() != "P1S":
        return jsonify({"error": "nozzle_diameter_override_supported_only_for_p1s"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    nozzle_diameter = _normalize_nozzle_diameter_value(data.get("nozzle_diameter"))
    if nozzle_diameter not in P1S_NOZZLE_DIAMETER_OPTIONS:
        allowed = ", ".join(P1S_NOZZLE_DIAMETER_OPTIONS)
        return jsonify({"error": f"nozzle_diameter must be one of: {allowed}"}), 400

    saved_override = HISTORY.set_nozzle_diameter_override(printer["id"], nozzle_diameter)
    with STATUS_LOCK:
        cached_status = deepcopy(STATUS_CACHE.get(printer["id"]))

    effective_nozzle_diameter = _resolve_nozzle_diameter(
        printer,
        cached_status=cached_status,
    )

    return jsonify(
        {
            "ok": True,
            "printer_id": printer["id"],
            "nozzle_diameter": effective_nozzle_diameter,
            "nozzle_diameter_override": saved_override,
        }
    )


@app.post("/api/printers/<printer_id>/pause")
def api_printer_pause(printer_id: str):
    if not LOCAL_PRINTER_IO_ENABLED:
        printer, error_response = _require_remote_printer(printer_id)
        if error_response:
            return error_response

        command = _queue_printer_command("pause", printer_id=printer["id"])
        return jsonify({"ok": True, "queued": True, "command_id": command["id"]})

    printer, error_response = _require_configured_printer(printer_id)
    if error_response:
        return error_response

    try:
        ok = _build_printer_client(printer).pause(timeout=15.0)
        if not ok:
            return jsonify({"ok": False, "error": "pause_not_confirmed"}), 409
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/printers/<printer_id>/resume")
def api_printer_resume(printer_id: str):
    if not LOCAL_PRINTER_IO_ENABLED:
        printer, error_response = _require_remote_printer(printer_id)
        if error_response:
            return error_response

        command = _queue_printer_command("resume", printer_id=printer["id"])
        return jsonify({"ok": True, "queued": True, "command_id": command["id"]})

    printer, error_response = _require_configured_printer(printer_id)
    if error_response:
        return error_response

    try:
        ok = _build_printer_client(printer).resume(timeout=15.0)
        if not ok:
            return jsonify({"ok": False, "error": "resume_not_confirmed"}), 409
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/printers/<printer_id>/stop")
def api_printer_stop(printer_id: str):
    if not LOCAL_PRINTER_IO_ENABLED:
        printer, error_response = _require_remote_printer(printer_id)
        if error_response:
            return error_response

        command = _queue_printer_command("stop", printer_id=printer["id"])
        return jsonify({"ok": True, "queued": True, "command_id": command["id"]})

    printer, error_response = _require_configured_printer(printer_id)
    if error_response:
        return error_response

    try:
        ok = _build_printer_client(printer).stop(timeout=15.0)
        if not ok:
            return jsonify({"ok": False, "error": "stop_not_confirmed"}), 409
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/upload_and_print")
def api_upload_and_print():
    _cleanup_finished_jobs()

    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400

    file = request.files["file"]
    printers_json = request.form.get("printers", "[]")

    try:
        printer_ids = _normalize_printer_ids(json.loads(printers_json))
    except json.JSONDecodeError:
        return jsonify({"error": "printers is not valid JSON"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    original_name = os.path.basename(file.filename or "").strip()
    if not original_name:
        return jsonify({"error": "empty filename"}), 400

    job_id = uuid.uuid4().hex
    upload_dir = JOBS_DIR / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / original_name

    try:
        file.save(temp_path)
    except OSError as exc:
        _cleanup_upload_artifacts(str(temp_path))
        return jsonify({"error": f"failed_to_save_upload: {exc}"}), 500

    project_plate_path = _detect_project_plate_path(str(temp_path))
    project_weight_g = get_total_weight_from_gcode_3mf(str(temp_path))
    if project_weight_g is not None:
        FILE_WEIGHTS.set_weight(original_name, project_weight_g)

    with JOBS_LOCK:
        JOB_COMPLETIONS[job_id] = set()
        JOBS[job_id] = {
            "job_id": job_id,
            "filename": original_name,
            "created_ts": time.time(),
            "finished_ts": None,
            "total": len(printer_ids),
            "done": 0,
            "ok_count": 0,
            "err_count": 0,
            "finished": False,
            "printers": {pid: {"stage": "queued"} for pid in printer_ids},
        }

    if not LOCAL_PRINTER_IO_ENABLED:
        valid_count = 0
        for pid in printer_ids:
            printer = _validate_remote_job_printer(job_id, pid)
            if not printer:
                continue

            valid_count += 1
            _queue_printer_command(
                "upload_and_print",
                printer_id=printer["id"],
                job_id=job_id,
                payload={
                    "job_id": job_id,
                    "filename": original_name,
                    "project_plate_path": project_plate_path,
                },
            )

        if valid_count == 0:
            _cleanup_upload_artifacts(str(temp_path))

        return jsonify({"job_id": job_id})

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, printer_ids, str(temp_path), original_name, project_plate_path),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.post("/api/upload_to_sd")
def api_upload_to_sd():
    _cleanup_finished_jobs()

    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400

    file = request.files["file"]
    printers_json = request.form.get("printers", "[]")

    try:
        printer_ids = _normalize_printer_ids(json.loads(printers_json))
    except json.JSONDecodeError:
        return jsonify({"error": "printers is not valid JSON"}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    original_name = os.path.basename(file.filename or "").strip()
    if not original_name:
        return jsonify({"error": "empty filename"}), 400

    job_id = uuid.uuid4().hex
    upload_dir = JOBS_DIR / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir / original_name

    try:
        file.save(temp_path)
    except OSError as exc:
        _cleanup_upload_artifacts(str(temp_path))
        return jsonify({"error": f"failed_to_save_upload: {exc}"}), 500

    project_weight_g = get_total_weight_from_gcode_3mf(str(temp_path))
    if project_weight_g is not None:
        FILE_WEIGHTS.set_weight(original_name, project_weight_g)

    with JOBS_LOCK:
        JOB_COMPLETIONS[job_id] = set()
        JOBS[job_id] = {
            "job_id": job_id,
            "filename": original_name,
            "created_ts": time.time(),
            "finished_ts": None,
            "total": len(printer_ids),
            "done": 0,
            "ok_count": 0,
            "err_count": 0,
            "finished": False,
            "printers": {pid: {"stage": "queued"} for pid in printer_ids},
        }

    if not LOCAL_PRINTER_IO_ENABLED:
        valid_count = 0
        for pid in printer_ids:
            printer = _validate_remote_job_printer(job_id, pid)
            if not printer:
                continue

            valid_count += 1
            _queue_printer_command(
                "upload_to_sd",
                printer_id=printer["id"],
                job_id=job_id,
                payload={
                    "job_id": job_id,
                    "filename": original_name,
                },
            )

        if valid_count == 0:
            _cleanup_upload_artifacts(str(temp_path))

        return jsonify({"job_id": job_id})

    thread = threading.Thread(
        target=_run_upload_only_job,
        args=(job_id, printer_ids, str(temp_path)),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.post("/api/restart_last_printed")
def api_restart_last_printed():
    _cleanup_finished_jobs()

    data = request.get_json(silent=True) or {}

    try:
        printer_ids = _normalize_printer_ids(data.get("printers", []))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    job_id = uuid.uuid4().hex

    with JOBS_LOCK:
        JOB_COMPLETIONS[job_id] = set()
        JOBS[job_id] = {
            "job_id": job_id,
            "filename": "(restart last printed)",
            "created_ts": time.time(),
            "finished_ts": None,
            "total": len(printer_ids),
            "done": 0,
            "ok_count": 0,
            "err_count": 0,
            "finished": False,
            "printers": {pid: {"stage": "queued"} for pid in printer_ids},
        }

    if not LOCAL_PRINTER_IO_ENABLED:
        for pid in printer_ids:
            printer = _validate_remote_job_printer(job_id, pid)
            if not printer:
                continue

            restart_file = HISTORY.get_last_printed(pid)
            if not restart_file:
                _job_set(job_id, pid, stage="error", message="no_last_printed")
                _job_done(job_id, pid, ok=False, message="no_last_printed")
                continue

            _queue_printer_command(
                "restart_last_printed",
                printer_id=printer["id"],
                job_id=job_id,
                payload={
                    "job_id": job_id,
                    "restart_file": restart_file,
                },
            )

        return jsonify({"job_id": job_id})

    thread = threading.Thread(
        target=_run_restart_job,
        args=(job_id, printer_ids),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.post("/api/mqtt/restart")
def api_mqtt_restart():
    global MQTT_MANAGER

    if not LOCAL_PRINTER_IO_ENABLED:
        command = _queue_farm_command("restart_mqtt")
        return jsonify({"ok": True, "queued": True, "command_id": command["id"]})

    try:
        _reload_printers_config()
    except Exception as exc:
        return jsonify({"ok": False, "error": f"config_reload_failed: {exc}"}), 500

    with MQTT_MANAGER_LOCK:
        manager = MQTT_MANAGER
        MQTT_MANAGER = None

    with STATUS_LOCK:
        STATUS_CACHE.clear()

    try:
        if manager is not None:
            manager.stop()
        _ensure_mqtt_manager()
        _start_printer_metadata_refresh()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/internal/status")
def internal_push_status():
    auth_error = _require_agent_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    raw_statuses = data.get("statuses")
    if not isinstance(raw_statuses, list):
        return jsonify({"error": "statuses must be a JSON array"}), 400

    accepted = 0
    for raw_status in raw_statuses:
        if not isinstance(raw_status, dict):
            continue
        pid = str(raw_status.get("id") or "").strip()
        if not pid:
            continue
        accepted += 1
        _on_status(pid, raw_status)

    agent_id = str(data.get("agent_id") or "").strip()
    printer_ids = [str(item).strip() for item in data.get("printer_ids", []) if str(item).strip()]
    if agent_id:
        REMOTE_STORE.record_agent_heartbeat(
            agent_id,
            printer_ids=printer_ids,
            meta={"kind": "status_push"},
        )

    return jsonify({"ok": True, "accepted": accepted})


@app.post("/internal/commands/pull")
def internal_pull_commands():
    auth_error = _require_agent_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    agent_id = str(data.get("agent_id") or "").strip()
    if not agent_id:
        return jsonify({"error": "agent_id is required"}), 400

    printer_ids = [str(item).strip() for item in data.get("printer_ids", []) if str(item).strip()]
    limit = _safe_int(data.get("limit")) or 10
    limit = max(1, min(limit, 50))

    REMOTE_STORE.record_agent_heartbeat(
        agent_id,
        printer_ids=printer_ids,
        meta={"kind": "command_pull"},
    )
    commands = REMOTE_STORE.claim_commands(agent_id=agent_id, printer_ids=printer_ids, limit=limit)
    return jsonify({"commands": commands})


@app.post("/internal/commands/<command_id>/result")
def internal_complete_command(command_id: str):
    auth_error = _require_agent_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    ok = bool(data.get("ok"))
    completed = REMOTE_STORE.complete_command(
        command_id,
        ok=ok,
        message=str(data.get("message") or "").strip() or None,
        result=data.get("result") if isinstance(data.get("result"), dict) else None,
        agent_id=str(data.get("agent_id") or "").strip() or None,
    )
    if completed is None:
        return jsonify({"error": "command_not_found"}), 404

    return jsonify({"ok": True, "command": completed})


@app.post("/internal/jobs/<job_id>/progress")
def internal_job_progress(job_id: str):
    auth_error = _require_agent_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    printer_id = str(data.get("printer_id") or "").strip()
    if not printer_id:
        return jsonify({"error": "printer_id is required"}), 400

    ok_value = data.get("ok")
    normalized_ok = None if ok_value is None else bool(ok_value)
    _job_apply_remote_progress(
        job_id,
        printer_id,
        stage=str(data.get("stage") or "").strip() or None,
        message=str(data.get("message") or "").strip() or None,
        file=str(data.get("file") or "").strip() or None,
        progress_percent=_safe_float(data.get("progress_percent")),
        ok=normalized_ok,
    )
    return jsonify({"ok": True})


@app.get("/internal/jobs/<job_id>/artifact/<path:filename>")
def internal_job_artifact(job_id: str, filename: str):
    auth_error = _require_agent_auth()
    if auth_error:
        return auth_error

    artifact_path = _resolve_job_artifact_path(job_id, filename)
    if artifact_path is None:
        return jsonify({"error": "artifact_not_found"}), 404

    return send_file(artifact_path, as_attachment=True, download_name=artifact_path.name)


if __name__ == "__main__":
    import os
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    debug_mode = False

    if LOCAL_PRINTER_IO_ENABLED and (not debug_mode or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
        _ensure_mqtt_manager()

    app.run(host="0.0.0.0", port=8080, debug=debug_mode)
