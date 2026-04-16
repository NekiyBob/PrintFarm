import json
import os
import threading
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional


class RemoteAgentStore:
    def __init__(self, base_dir: str | Path, *, command_lease_sec: float = 3600.0):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.commands_path = self.base_dir / "commands.json"
        self.statuses_path = self.base_dir / "statuses.json"
        self.agents_path = self.base_dir / "agents.json"

        self.command_lease_sec = max(30.0, float(command_lease_sec))
        self._lock = threading.Lock()
        self._commands: list[dict[str, Any]] = []
        self._statuses: dict[str, dict[str, Any]] = {}
        self._agents: dict[str, dict[str, Any]] = {}
        self._load()

    def _load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return deepcopy(default)

        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return deepcopy(default)

    def _save_json(self, path: Path, payload: Any) -> None:
        tmp_path = path.with_suffix(f"{path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _load(self) -> None:
        raw_commands = self._load_json(self.commands_path, [])
        raw_statuses = self._load_json(self.statuses_path, {})
        raw_agents = self._load_json(self.agents_path, {})

        self._commands = raw_commands if isinstance(raw_commands, list) else []
        self._statuses = raw_statuses if isinstance(raw_statuses, dict) else {}
        self._agents = raw_agents if isinstance(raw_agents, dict) else {}

    def list_statuses(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self._statuses)

    def set_status(self, printer_id: str, status: dict[str, Any]) -> None:
        normalized_id = str(printer_id or "").strip()
        if not normalized_id:
            return

        payload = dict(status or {})
        payload["id"] = normalized_id
        payload["updated_ts"] = time.time()

        with self._lock:
            self._statuses[normalized_id] = payload
            self._save_json(self.statuses_path, self._statuses)

    def set_statuses(self, statuses: dict[str, dict[str, Any]]) -> None:
        if not statuses:
            return

        now = time.time()
        with self._lock:
            for printer_id, status in statuses.items():
                normalized_id = str(printer_id or "").strip()
                if not normalized_id:
                    continue
                payload = dict(status or {})
                payload["id"] = normalized_id
                payload["updated_ts"] = now
                self._statuses[normalized_id] = payload

            self._save_json(self.statuses_path, self._statuses)

    def record_agent_heartbeat(
        self,
        agent_id: str,
        *,
        printer_ids: list[str],
        meta: Optional[dict[str, Any]] = None,
    ) -> None:
        normalized_id = str(agent_id or "").strip()
        if not normalized_id:
            return

        record = {
            "agent_id": normalized_id,
            "printer_ids": [str(item).strip() for item in printer_ids if str(item).strip()],
            "updated_ts": time.time(),
        }
        if meta:
            record["meta"] = dict(meta)

        with self._lock:
            self._agents[normalized_id] = record
            self._save_json(self.agents_path, self._agents)

    def create_command(
        self,
        command_type: str,
        *,
        printer_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        job_id: Optional[str] = None,
        scope: str = "printer",
        target_agent_id: Optional[str] = None,
    ) -> dict[str, Any]:
        command = {
            "id": uuid.uuid4().hex,
            "type": str(command_type or "").strip(),
            "scope": "farm" if scope == "farm" else "printer",
            "printer_id": str(printer_id or "").strip() or None,
            "job_id": str(job_id or "").strip() or None,
            "target_agent_id": str(target_agent_id or "").strip() or None,
            "payload": dict(payload or {}),
            "status": "queued",
            "created_ts": time.time(),
            "updated_ts": time.time(),
            "claimed_by": None,
            "claimed_ts": None,
            "lease_until_ts": None,
            "attempts": 0,
            "result": None,
        }

        with self._lock:
            self._commands.append(command)
            self._save_json(self.commands_path, self._commands)

        return deepcopy(command)

    def claim_commands(
        self,
        *,
        agent_id: str,
        printer_ids: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        normalized_agent_id = str(agent_id or "").strip()
        if not normalized_agent_id:
            return []

        normalized_printers = {str(item).strip() for item in printer_ids if str(item).strip()}
        claim_limit = max(1, int(limit))
        now = time.time()
        claimed: list[dict[str, Any]] = []

        with self._lock:
            busy_printers = {
                str(command.get("printer_id") or "").strip()
                for command in self._commands
                if command.get("status") == "claimed"
                and (command.get("lease_until_ts") or 0) >= now
                and str(command.get("printer_id") or "").strip()
            }

            for command in self._commands:
                if len(claimed) >= claim_limit:
                    break

                if command.get("target_agent_id") and command.get("target_agent_id") != normalized_agent_id:
                    continue

                scope = command.get("scope") or "printer"
                printer_id = command.get("printer_id")
                if scope != "farm" and printer_id not in normalized_printers:
                    continue
                if scope != "farm" and printer_id in busy_printers:
                    continue

                status = command.get("status")
                lease_until = command.get("lease_until_ts") or 0
                lease_expired = lease_until < now
                can_claim = status == "queued" or (status == "claimed" and lease_expired)
                if not can_claim:
                    continue

                command["status"] = "claimed"
                command["claimed_by"] = normalized_agent_id
                command["claimed_ts"] = now
                command["lease_until_ts"] = now + self.command_lease_sec
                command["updated_ts"] = now
                command["attempts"] = int(command.get("attempts") or 0) + 1
                if scope != "farm" and printer_id:
                    busy_printers.add(printer_id)
                claimed.append(deepcopy(command))

            if claimed:
                self._save_json(self.commands_path, self._commands)

        return claimed

    def complete_command(
        self,
        command_id: str,
        *,
        ok: bool,
        message: Optional[str] = None,
        result: Optional[dict[str, Any]] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        normalized_command_id = str(command_id or "").strip()
        if not normalized_command_id:
            return None

        normalized_agent_id = str(agent_id or "").strip() or None
        now = time.time()

        with self._lock:
            for command in self._commands:
                if command.get("id") != normalized_command_id:
                    continue

                if normalized_agent_id and command.get("claimed_by") not in (None, normalized_agent_id):
                    return None

                command["status"] = "done" if ok else "error"
                command["updated_ts"] = now
                command["lease_until_ts"] = None
                command["result"] = {
                    "ok": bool(ok),
                    "message": message,
                    "payload": dict(result or {}),
                    "completed_ts": now,
                }
                self._save_json(self.commands_path, self._commands)
                return deepcopy(command)

        return None

    def cleanup_commands(self, *, keep_sec: float) -> int:
        cutoff = time.time() - max(60.0, float(keep_sec))

        with self._lock:
            before = len(self._commands)
            self._commands = [
                command
                for command in self._commands
                if command.get("status") in {"queued", "claimed"}
                or (command.get("updated_ts") or 0) >= cutoff
            ]
            removed = before - len(self._commands)
            if removed:
                self._save_json(self.commands_path, self._commands)
            return removed
