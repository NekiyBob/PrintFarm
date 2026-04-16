import os
import sys
import threading
import traceback
from pathlib import Path

import servicemanager
import win32service
import win32serviceutil


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env.agent"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "agent-service.log"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        key, sep, value = line.partition("=")
        if not sep:
            continue

        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ[key] = value


class _FileStream:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def write(self, data: str) -> int:
        if not data:
            return 0

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(data)
        return len(data)

    def flush(self) -> None:
        return


def _configure_runtime() -> None:
    os.chdir(BASE_DIR)
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    _load_env_file(ENV_PATH)

    file_stream = _FileStream(LOG_PATH)
    sys.stdout = file_stream
    sys.stderr = file_stream


_configure_runtime()

from agent import PrintFarmAgent


class PrintFarmAgentService(win32serviceutil.ServiceFramework):
    _svc_name_ = "PrintFarmAgent"
    _svc_display_name_ = "PrintFarm Agent"
    _svc_description_ = "PrintFarm LAN agent that polls commands and syncs printer status."

    def __init__(self, args):
        super().__init__(args)
        self._agent = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        servicemanager.LogInfoMsg(f"{self._svc_name_}: stop requested")
        if self._agent is not None:
            try:
                self._agent.stop()
            except Exception:
                servicemanager.LogErrorMsg(
                    f"{self._svc_name_}: stop failed\n{traceback.format_exc()}"
                )

    def SvcDoRun(self):
        servicemanager.LogInfoMsg(f"{self._svc_name_}: starting from {BASE_DIR}")
        try:
            self._agent = PrintFarmAgent()
            self._agent.run()
            servicemanager.LogInfoMsg(f"{self._svc_name_}: stopped")
        except Exception:
            error_text = traceback.format_exc()
            print(error_text, flush=True)
            servicemanager.LogErrorMsg(f"{self._svc_name_}: fatal error\n{error_text}")
            raise


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(PrintFarmAgentService)
