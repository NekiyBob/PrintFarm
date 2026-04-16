import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

import yaml

from mqtt_manager import MqttStatusManager, PrinterCfg
from printer_client import start_print_on_printer, upload_file_to_printer
from printer_lan import Printer


UPLOAD_CONCURRENCY = 3
RESTART_CONCURRENCY = 3

UPLOAD_SEM = threading.Semaphore(UPLOAD_CONCURRENCY)
RESTART_SEM = threading.Semaphore(RESTART_CONCURRENCY)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "printers.yaml"
AGENT_JOBS_DIR = BASE_DIR / "agent_jobs"

# Configure the remote server and shared token here.
# These values are intentionally read from code, not from environment variables.
SERVER_BASE_URL = "http://192.168.2.20:5002".strip().rstrip("/")
AGENT_SHARED_TOKEN = "my-very-long-random-secret-token-2026".strip()

AGENT_ID = str(os.environ.get("PRINTFARM_AGENT_ID") or socket.gethostname()).strip()
AGENT_POLL_INTERVAL_SEC = max(1.0, float(os.environ.get("PRINTFARM_AGENT_POLL_INTERVAL_SEC") or 2.0))
AGENT_STATUS_PUSH_INTERVAL_SEC = max(1.0, float(os.environ.get("PRINTFARM_AGENT_STATUS_PUSH_INTERVAL_SEC") or 2.0))
AGENT_COMMAND_WORKERS = max(1, int(os.environ.get("PRINTFARM_AGENT_COMMAND_WORKERS") or 6))


class RemoteApiError(RuntimeError):
    pass


def _load_printers_config(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        cfg = yaml.safe_load(handle) or {}

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


def _is_printer_configured(printer: dict[str, Any]) -> bool:
    return bool(printer.get("ip") and printer.get("serial") and printer.get("access_code"))


def _build_printer_client(printer: dict[str, Any]) -> Printer:
    return Printer(
        ip=printer["ip"],
        serial=printer["serial"],
        access_code=printer["access_code"],
        model=printer.get("model") or "",
    )


def retry(fn, tries: int = 3, base_delay: float = 1.0, factor: float = 2.0):
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
            time.sleep(delay)

    raise last_exc


class PrintFarmAgent:
    def __init__(self) -> None:
        if not SERVER_BASE_URL:
            raise RuntimeError("SERVER_BASE_URL is required in agent.py")

        self.printers, self.printers_by_id = _load_printers_config(CONFIG_PATH)
        self.controlled_printers = [printer for printer in self.printers if _is_printer_configured(printer)]
        self.controlled_printer_ids = [printer["id"] for printer in self.controlled_printers]

        self._status_lock = threading.Lock()
        self._status_cache: dict[str, dict[str, Any]] = {}
        self._active_lock = threading.Lock()
        self._active_command_ids: set[str] = set()
        self._stop_evt = threading.Event()
        self._mqtt_manager: Optional[MqttStatusManager] = None
        self._executor = ThreadPoolExecutor(max_workers=AGENT_COMMAND_WORKERS)

    def _make_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Any:
        url = f"{SERVER_BASE_URL}{path}"
        data = None
        headers = {"Accept": "application/json"}

        if AGENT_SHARED_TOKEN:
            headers["Authorization"] = f"Bearer {AGENT_SHARED_TOKEN}"

        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read()
                content_type = str(response.headers.get("Content-Type") or "")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw)
                message = parsed.get("error") or raw
            except Exception:
                message = raw or str(exc)
            raise RemoteApiError(f"{method} {path} failed: {message}") from exc
        except urllib.error.URLError as exc:
            raise RemoteApiError(f"{method} {path} failed: {exc}") from exc

        if not body:
            return None

        if "application/json" in content_type:
            return json.loads(body.decode("utf-8"))

        return body

    def _download_job_artifact(self, job_id: str, filename: str) -> Path:
        normalized_name = os.path.basename(str(filename or "").strip())
        if not job_id or not normalized_name:
            raise RuntimeError("missing job artifact reference")

        target_dir = AGENT_JOBS_DIR / job_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / normalized_name

        quoted_name = urllib.parse.quote(normalized_name)
        path = f"/internal/jobs/{urllib.parse.quote(job_id)}/artifact/{quoted_name}"
        url = f"{SERVER_BASE_URL}{path}"
        headers = {}
        if AGENT_SHARED_TOKEN:
            headers["Authorization"] = f"Bearer {AGENT_SHARED_TOKEN}"

        req = urllib.request.Request(url, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                with target_path.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RemoteApiError(f"download artifact failed: {raw or exc}") from exc
        except urllib.error.URLError as exc:
            raise RemoteApiError(f"download artifact failed: {exc}") from exc

        return target_path

    def _push_job_progress(
        self,
        job_id: Optional[str],
        printer_id: str,
        *,
        stage: str,
        message: Optional[str] = None,
        ok: Optional[bool] = None,
        file: Optional[str] = None,
        progress_percent: Optional[float] = None,
    ) -> None:
        if not job_id:
            return

        payload: dict[str, Any] = {
            "printer_id": printer_id,
            "stage": stage,
        }
        if message is not None:
            payload["message"] = message
        if ok is not None:
            payload["ok"] = ok
        if file:
            payload["file"] = file
        if progress_percent is not None:
            payload["progress_percent"] = progress_percent

        self._make_request(
            f"/internal/jobs/{urllib.parse.quote(job_id)}/progress",
            method="POST",
            payload=payload,
            timeout=60,
        )

    def _complete_command(
        self,
        command_id: str,
        *,
        ok: bool,
        message: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
    ) -> None:
        self._make_request(
            f"/internal/commands/{urllib.parse.quote(command_id)}/result",
            method="POST",
            payload={
                "agent_id": AGENT_ID,
                "ok": ok,
                "message": message,
                "result": result or {},
            },
            timeout=60,
        )

    def _push_status_snapshot(self) -> None:
        with self._status_lock:
            statuses = [dict(status) for status in self._status_cache.values()]

        self._make_request(
            "/internal/status",
            method="POST",
            payload={
                "agent_id": AGENT_ID,
                "printer_ids": self.controlled_printer_ids,
                "statuses": statuses,
            },
            timeout=60,
        )

    def _status_push_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._push_status_snapshot()
            except Exception as exc:
                print(f"[agent] status push failed: {exc}", flush=True)
            self._stop_evt.wait(AGENT_STATUS_PUSH_INTERVAL_SEC)

    def _on_status(self, printer_id: str, status: dict[str, Any]) -> None:
        payload = dict(status or {})
        payload["id"] = printer_id
        with self._status_lock:
            self._status_cache[printer_id] = payload

    def _start_mqtt(self) -> None:
        printer_cfgs = [
            PrinterCfg(
                id=printer["id"],
                ip=printer.get("ip") or "",
                serial=printer.get("serial") or "",
                access_code=printer.get("access_code") or "",
                model=printer.get("model") or "",
            )
            for printer in self.controlled_printers
        ]

        self._mqtt_manager = MqttStatusManager(
            printers=printer_cfgs,
            on_status=self._on_status,
            offline_after_sec=180.0,
            monitor_interval_sec=2.0,
            keepalive=60,
        )
        self._mqtt_manager.start()

    def _restart_mqtt(self) -> None:
        if self._mqtt_manager is not None:
            self._mqtt_manager.stop()
        self._start_mqtt()

    def _get_printer(self, printer_id: str) -> dict[str, Any]:
        printer = self.printers_by_id.get(printer_id)
        if not printer or not _is_printer_configured(printer):
            raise RuntimeError(f"printer {printer_id} is not configured on this agent")
        return printer

    def _handle_control_command(self, command: dict[str, Any], printer: dict[str, Any]) -> str:
        action = str(command.get("type") or "").strip().lower()
        client = _build_printer_client(printer)
        if action == "pause":
            ok = client.pause(timeout=15.0)
        elif action == "resume":
            ok = client.resume(timeout=15.0)
        elif action == "stop":
            ok = client.stop(timeout=15.0)
        else:
            raise RuntimeError(f"unsupported control action: {action}")

        if not ok:
            raise RuntimeError(f"{action}_not_confirmed")
        return action

    def _handle_upload_and_print(self, command: dict[str, Any], printer: dict[str, Any]) -> str:
        payload = dict(command.get("payload") or {})
        job_id = str(payload.get("job_id") or command.get("job_id") or "").strip()
        filename = os.path.basename(str(payload.get("filename") or "").strip())
        project_plate_path = payload.get("project_plate_path")
        printer_id = printer["id"]
        artifact_path: Optional[Path] = None

        try:
            self._push_job_progress(job_id, printer_id, stage="downloading")
            artifact_path = self._download_job_artifact(job_id, filename)

            self._push_job_progress(job_id, printer_id, stage="uploading")

            def on_upload_progress(percent, bytes_sent, total_bytes):
                self._push_job_progress(
                    job_id,
                    printer_id,
                    stage="uploading",
                    progress_percent=percent,
                )

            def do_upload():
                with UPLOAD_SEM:
                    return upload_file_to_printer(
                        printer["ip"],
                        printer["access_code"],
                        str(artifact_path),
                        model=printer.get("model") or "",
                        progress_callback=on_upload_progress,
                    )

            retry(do_upload, tries=3, base_delay=1.0, factor=2.0)
            self._push_job_progress(job_id, printer_id, stage="uploaded", progress_percent=100.0)
            time.sleep(1.5)

            self._push_job_progress(job_id, printer_id, stage="starting")

            def do_start():
                return start_print_on_printer(
                    printer["ip"],
                    printer["access_code"],
                    printer["serial"],
                    filename,
                    plate_num=1,
                    plate_path=project_plate_path,
                    model=printer.get("model") or "",
                )

            retry(do_start, tries=3, base_delay=1.0, factor=2.0)
            self._push_job_progress(job_id, printer_id, stage="started", ok=True, file=filename)
            return "started"
        except Exception as exc:
            self._push_job_progress(job_id, printer_id, stage="error", message=str(exc), ok=False, file=filename or None)
            raise
        finally:
            if artifact_path is not None:
                try:
                    artifact_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _handle_upload_to_sd(self, command: dict[str, Any], printer: dict[str, Any]) -> str:
        payload = dict(command.get("payload") or {})
        job_id = str(payload.get("job_id") or command.get("job_id") or "").strip()
        filename = os.path.basename(str(payload.get("filename") or "").strip())
        printer_id = printer["id"]
        artifact_path: Optional[Path] = None

        try:
            self._push_job_progress(job_id, printer_id, stage="downloading")
            artifact_path = self._download_job_artifact(job_id, filename)

            self._push_job_progress(job_id, printer_id, stage="uploading")

            def on_upload_progress(percent, bytes_sent, total_bytes):
                self._push_job_progress(
                    job_id,
                    printer_id,
                    stage="uploading",
                    progress_percent=percent,
                )

            def do_upload():
                with UPLOAD_SEM:
                    return upload_file_to_printer(
                        printer["ip"],
                        printer["access_code"],
                        str(artifact_path),
                        model=printer.get("model") or "",
                        progress_callback=on_upload_progress,
                    )

            retry(do_upload, tries=3, base_delay=1.0, factor=2.0)
            self._push_job_progress(
                job_id,
                printer_id,
                stage="uploaded",
                message="saved_to_sd",
                ok=True,
                file=filename,
                progress_percent=100.0,
            )
            return "saved_to_sd"
        except Exception as exc:
            self._push_job_progress(job_id, printer_id, stage="error", message=str(exc), ok=False, file=filename or None)
            raise
        finally:
            if artifact_path is not None:
                try:
                    artifact_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _handle_restart_last_printed(self, command: dict[str, Any], printer: dict[str, Any]) -> str:
        payload = dict(command.get("payload") or {})
        job_id = str(payload.get("job_id") or command.get("job_id") or "").strip()
        restart_file = os.path.basename(str(payload.get("restart_file") or "").strip())
        printer_id = printer["id"]

        if not restart_file:
            raise RuntimeError("restart_file is missing")

        try:
            self._push_job_progress(job_id, printer_id, stage="starting", file=restart_file)

            def do_start():
                with RESTART_SEM:
                    return start_print_on_printer(
                        printer["ip"],
                        printer["access_code"],
                        printer["serial"],
                        restart_file,
                        model=printer.get("model") or "",
                    )

            retry(do_start, tries=3, base_delay=1.0, factor=2.0)
            self._push_job_progress(job_id, printer_id, stage="started", ok=True, file=restart_file)
            return restart_file
        except Exception as exc:
            self._push_job_progress(job_id, printer_id, stage="error", message=str(exc), ok=False, file=restart_file)
            raise

    def _execute_command(self, command: dict[str, Any]) -> None:
        command_id = str(command.get("id") or "").strip()
        command_type = str(command.get("type") or "").strip()
        printer_id = str(command.get("printer_id") or "").strip()

        try:
            if command_type == "restart_mqtt":
                self._restart_mqtt()
                self._complete_command(command_id, ok=True, message="mqtt_restarted")
                return

            printer = self._get_printer(printer_id)

            if command_type in {"pause", "resume", "stop"}:
                result_message = self._handle_control_command(command, printer)
            elif command_type == "upload_and_print":
                result_message = self._handle_upload_and_print(command, printer)
            elif command_type == "upload_to_sd":
                result_message = self._handle_upload_to_sd(command, printer)
            elif command_type == "restart_last_printed":
                result_message = self._handle_restart_last_printed(command, printer)
            else:
                raise RuntimeError(f"unsupported command type: {command_type}")

            self._complete_command(command_id, ok=True, message=result_message)
        except Exception as exc:
            print(f"[agent] command {command_id} failed: {exc}", flush=True)
            try:
                self._complete_command(command_id, ok=False, message=str(exc))
            except Exception as complete_exc:
                print(f"[agent] failed to report command result: {complete_exc}", flush=True)
        finally:
            if command_id:
                with self._active_lock:
                    self._active_command_ids.discard(command_id)

    def _poll_commands_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                with self._active_lock:
                    available_slots = max(0, AGENT_COMMAND_WORKERS - len(self._active_command_ids))

                if available_slots <= 0:
                    self._stop_evt.wait(AGENT_POLL_INTERVAL_SEC)
                    continue

                data = self._make_request(
                    "/internal/commands/pull",
                    method="POST",
                    payload={
                        "agent_id": AGENT_ID,
                        "printer_ids": self.controlled_printer_ids,
                        "limit": available_slots,
                    },
                    timeout=60,
                ) or {}
                commands = data.get("commands") or []
                for command in commands:
                    command_id = str(command.get("id") or "").strip()
                    if command_id:
                        with self._active_lock:
                            self._active_command_ids.add(command_id)
                    self._executor.submit(self._execute_command, command)
            except Exception as exc:
                print(f"[agent] command poll failed: {exc}", flush=True)

            self._stop_evt.wait(AGENT_POLL_INTERVAL_SEC)

    def run(self) -> None:
        AGENT_JOBS_DIR.mkdir(parents=True, exist_ok=True)
        self._start_mqtt()

        status_thread = threading.Thread(
            target=self._status_push_loop,
            daemon=True,
            name="agent-status-push",
        )
        poll_thread = threading.Thread(
            target=self._poll_commands_loop,
            daemon=True,
            name="agent-command-poll",
        )

        status_thread.start()
        poll_thread.start()

        print(
            f"[agent] started. server={SERVER_BASE_URL} agent_id={AGENT_ID} printers={len(self.controlled_printer_ids)}",
            flush=True,
        )

        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("[agent] stopping...", flush=True)
            self._stop_evt.set()
            if self._mqtt_manager is not None:
                self._mqtt_manager.stop()
            self._executor.shutdown(wait=False, cancel_futures=False)


if __name__ == "__main__":
    PrintFarmAgent().run()
