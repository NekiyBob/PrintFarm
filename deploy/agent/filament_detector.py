import copy
from typing import Any, Optional


def deep_merge(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def extract_print(status: dict) -> dict:
    if isinstance(status.get("print"), dict):
        return status["print"]

    report = status.get("report")
    if isinstance(report, dict) and isinstance(report.get("print"), dict):
        return report["print"]

    return status


def get_nested(data: Any, path: list[Any], default=None):
    cur = data
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        elif isinstance(cur, list) and isinstance(key, int) and 0 <= key < len(cur):
            cur = cur[key]
        else:
            return default
    return cur


class LoadFilamentDetector:
    STATE_IDLE = "IDLE"
    STATE_LOADING = "LOADING"
    STATE_COOLDOWN = "COOLDOWN"

    def __init__(self, finish_stable_cycles: int = 2, cooldown_cycles: int = 3):
        self.merged_status: dict[str, Any] = {}
        self.prev_snapshot: Optional[dict[str, Any]] = None

        self.state = self.STATE_IDLE
        self.stable_cycles = 0
        self.cooldown_cycles = 0

        self.finish_stable_cycles = finish_stable_cycles
        self.required_cooldown_cycles = cooldown_cycles

    def update(self, raw_status: dict[str, Any]) -> Optional[str]:
        self.merged_status = deep_merge(copy.deepcopy(self.merged_status), raw_status)
        p = extract_print(self.merged_status)

        snapshot = {
            "gcode_state": p.get("gcode_state"),
            "state": p.get("state"),
            "percent": p.get("percent"),
            "command": p.get("command"),
            "result": p.get("result"),
            "tar_temp": p.get("tar_temp"),
            "ams_status": p.get("ams_status"),
            "ams_rfid_status": p.get("ams_rfid_status"),
            "tray_now": get_nested(p, ["ams", "tray_now"]),
            "tray_pre": get_nested(p, ["ams", "tray_pre"]),
            "tray_tar": get_nested(p, ["ams", "tray_tar"]),
            "hw_switch_state": p.get("hw_switch_state"),
            "nozzle_target_temper": p.get("nozzle_target_temper"),
            "nozzle_temper": p.get("nozzle_temper"),
            "extruder_stat": get_nested(p, ["device", "extruder", "info", 0, "stat"]),
        }

        prev = self.prev_snapshot

        non_printing = (
            snapshot["gcode_state"] in (None, "FINISH", "IDLE", "FAILED")
            or (snapshot["state"] == 6 and snapshot["percent"] == 100)
        )

        direct_command = (
            snapshot["command"] == "gcode_line"
            and str(snapshot["result"]).upper() == "SUCCESS"
            and (snapshot["tar_temp"] or 0) >= 220
        )

        hot_nozzle_target = max(
            snapshot["tar_temp"] or 0,
            snapshot["nozzle_target_temper"] or 0,
        ) >= 220

        tray_values = [snapshot["tray_now"], snapshot["tray_pre"], snapshot["tray_tar"]]
        tray_values = [x for x in tray_values if x is not None]
        tray_mismatch = bool(tray_values) and len(set(tray_values)) > 1

        ams_status_changed = False
        ams_rfid_changed = False
        tray_changed = False
        hw_switch_changed = False
        extruder_changed = False

        if prev is not None:
            ams_status_changed = snapshot["ams_status"] != prev["ams_status"]
            ams_rfid_changed = snapshot["ams_rfid_status"] != prev["ams_rfid_status"]
            tray_changed = (
                snapshot["tray_now"] != prev["tray_now"]
                or snapshot["tray_pre"] != prev["tray_pre"]
                or snapshot["tray_tar"] != prev["tray_tar"]
            )
            hw_switch_changed = snapshot["hw_switch_state"] != prev["hw_switch_state"]
            extruder_changed = snapshot["extruder_stat"] != prev["extruder_stat"]

        start_score = 0
        if direct_command:
            start_score += 4
        if hot_nozzle_target:
            start_score += 1
        if tray_mismatch:
            start_score += 1
        if ams_status_changed:
            start_score += 2
        if ams_rfid_changed:
            start_score += 1
        if tray_changed:
            start_score += 2
        if hw_switch_changed:
            start_score += 1
        if extruder_changed:
            start_score += 2

        active_signal = any(
            [
                direct_command,
                tray_mismatch,
                ams_status_changed,
                ams_rfid_changed,
                tray_changed,
                hw_switch_changed,
                extruder_changed,
            ]
        )

        stable_signal = (
            not direct_command
            and not tray_mismatch
            and not ams_status_changed
            and not ams_rfid_changed
            and not tray_changed
            and not hw_switch_changed
            and not extruder_changed
        )

        event = None

        if self.state == self.STATE_IDLE:
            if non_printing and start_score >= 3:
                self.state = self.STATE_LOADING
                self.stable_cycles = 0
                event = "LOAD_FILAMENT_STARTED"

        elif self.state == self.STATE_LOADING:
            if active_signal:
                self.stable_cycles = 0
            elif stable_signal:
                self.stable_cycles += 1
            else:
                self.stable_cycles = 0

            if self.stable_cycles >= self.finish_stable_cycles:
                self.state = self.STATE_COOLDOWN
                self.cooldown_cycles = 0
                self.stable_cycles = 0
                event = "LOAD_FILAMENT_FINISHED"

        elif self.state == self.STATE_COOLDOWN:
            if stable_signal:
                self.cooldown_cycles += 1
                if self.cooldown_cycles >= self.required_cooldown_cycles:
                    self.state = self.STATE_IDLE
                    self.cooldown_cycles = 0
            else:
                self.cooldown_cycles = 0

        self.prev_snapshot = snapshot
        return event
