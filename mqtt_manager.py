import json
import ssl
import time
import uuid
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Any, List

import paho.mqtt.client as mqtt


@dataclass(frozen=True)
class PrinterCfg:
    id: str
    ip: str
    serial: str
    access_code: str


class MqttStatusManager:
    """
    Постоянно держит MQTT TLS соединение к каждому принтеру:
      - подписывается на device/<serial>/report
      - парсит JSON
      - обновляет статусы через callback(pid, status_dict)

    Статус-словарь соответствует тому, что ждёт твой фронт:
      {id, ok, gcode_state, ts, error}
    """

    def __init__(
        self,
        printers: List[PrinterCfg],
        on_status: Callable[[str, Dict[str, Any]], None],
        offline_after_sec: float = 50.0,   # если report не приходил > N сек -> offline
        monitor_interval_sec: float = 3.0, # как часто проверять "протухшие" принтеры
        keepalive: int = 60,
    ):
        self._printers = printers
        self._on_status = on_status
        self._offline_after = offline_after_sec
        self._monitor_interval = monitor_interval_sec
        self._keepalive = keepalive

        self._clients: Dict[str, mqtt.Client] = {}     # pid -> client
        self._last_seen: Dict[str, float] = {}         # pid -> ts последнего report
        self._last_ok: Dict[str, Optional[bool]] = {}  # чтобы не спамить offline-апдейтами
        self._stop_evt = threading.Event()
        self._mon_thread: Optional[threading.Thread] = None
        self._ctl_lock = threading.Lock()


    def start(self) -> None:
        """Запускаем подключения ко всем принтерам + монитор offline."""
        self._stop_evt.clear()

        for p in self._printers:
            if not p.ip or not p.serial or not p.access_code:
                # конфиг неполный -> сразу offline
                self._emit(p.id, ok=False, gcode_state=None, error="missing_config")
                continue

            client = self._make_client(p)
            self._clients[p.id] = client

            # connect_async не блокирует старт (важно при 90 принтерах)
            client.connect_async(p.ip, 8883, keepalive=self._keepalive)
            client.loop_start()

            # до первого report считаем "неизвестно", но ok пока не False
            # (чтобы кнопка не стала тёмно-синей сразу)
            self._last_ok[p.id] = None

        self._mon_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._mon_thread.start()

    def stop(self) -> None:
        """Остановить все MQTT клиенты и монитор."""
        self._stop_evt.set()

        if self._mon_thread and self._mon_thread.is_alive():
            self._mon_thread.join(timeout=2.0)

        for pid, client in list(self._clients.items()):
            try:
                client.loop_stop()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass

        self._clients.clear()
    def restart(self) -> None:
        """Жёстко переподключить все принтеры: stop() + start()."""
        with self._ctl_lock:
            self.stop()
            # маленькая пауза, чтобы ОС успела закрыть сокеты
            time.sleep(0.3)
            self.start()

    # ----------------- internal -----------------

    def _make_client(self, p: PrinterCfg) -> mqtt.Client:
        client_id = f"farm-{p.id}-{uuid.uuid4()}"
        client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

        # Bambu LAN: username фиксированный bblp, password = access_code
        client.username_pw_set("bblp", p.access_code)

        # TLS self-signed
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)

        # авто-реконнект
        client.reconnect_delay_set(min_delay=1, max_delay=20)

        topic_report = f"device/{p.serial}/report"

        def on_connect(cl, userdata, flags, rc):
            # rc=0 ok
            if rc == 0:
                cl.subscribe(topic_report)
                # не ставим ok=True до первого report — чтобы не обманывать интерфейс
            else:
                self._emit(p.id, ok=False, gcode_state=None, error=f"mqtt_connect_rc_{rc}")

        def on_disconnect(cl, userdata, rc):
            # во время массовых FTPS это может быть кратковременно — не темним сразу
            if not self._stop_evt.is_set():
                # оставим решение за monitor_loop (stale_no_report)
                pass


        def on_message(cl, userdata, msg):
            try:
                payload = msg.payload.decode("utf-8", errors="replace")
                data = json.loads(payload)

                pr = data.get("print", {}) or {}
                gcode_state = pr.get("gcode_state")

                # имя текущего файла / задания (бывает в разных полях)
                file_hint = (
                    pr.get("subtask_name")
                    or pr.get("file")
                    or pr.get("gcode_file")
                    or pr.get("project_name")
                )

                # --- Ошибки печати ---
                # 1) HMS ошибки обычно в data["hms"] (иногда пустой список)
                hms_list = data.get("hms") or pr.get("hms") or []
                # 2) print_error иногда приходит отдельным числом
                print_error = pr.get("print_error")

                # Вытаскиваем коды HMS в удобный вид
                hms_codes = []
                for it in hms_list:
                    if isinstance(it, dict):
                        code = it.get("code") or it.get("hms_code") or it.get("hms") or it.get("id")
                        if code is not None:
                            hms_codes.append(str(code))
                    else:
                        hms_codes.append(str(it))

                # Нормализация для сравнения кодов (убираем все кроме 0-9A-F)
                def _norm(code: str) -> str:
                    return "".join(ch for ch in code.upper() if ch in "0123456789ABCDEF")

                # Детект “закончилась нить” (самое важное)
                # External spool runout: HMS_07FE-7000-0002-0003
                # AMS slot runout:      HMS_0700-2000-0002-0001
                filament_runout = False
                for c in hms_codes:
                    nc = _norm(c)
                    if nc == _norm("07FE-7000-0002-0003") or nc == _norm("0700-2000-0002-0001"):
                        filament_runout = True
                        break

                # Формируем “error” для фронта
                # В UI красим ТОЛЬКО "закончился филамент"
                error = "filament_runout" if filament_runout else None


                now = time.time()
                self._last_seen[p.id] = now

                self._emit(
                    p.id,
                    ok=True,
                    gcode_state=gcode_state,
                    error=error,
                    file=file_hint,
                    hms=hms_codes,
                    print_error=print_error,
                )

            except Exception as e:
                self._emit(p.id, ok=False, gcode_state=None, error=f"bad_report: {e}")



        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        return client

    def _monitor_loop(self):
        while not self._stop_evt.is_set():
            now = time.time()

            for p in self._printers:
                pid = p.id
                last = self._last_seen.get(pid)

                # Если ни разу не получали report — не красим offline мгновенно.
                # Но если прошло "offline_after" после старта и всё ещё нет report — тогда offline.
                if last is None:
                    # если менеджер работает давно, а отчёта нет
                    # отметим offline только если раньше не отметили
                    if self._last_ok.get(pid) is None:
                        # пока неизвестно — пропускаем
                        pass
                    else:
                        # уже было ok/false — пропускаем
                        pass
                    continue

                if (now - last) > self._offline_after:
                    # чтобы не спамить одинаковыми offline каждые 2 секунды
                    if self._last_ok.get(pid) is not False:
                        self._emit(pid, ok=False, gcode_state=None, error="stale_no_report")

            time.sleep(self._monitor_interval)

    def _emit(self, pid: str, ok: Optional[bool], gcode_state: Optional[str],
          error: Optional[str], file=None, hms=None, print_error=None):
        self._last_ok[pid] = ok
        status = {
            "id": pid,
            "ok": ok,
            "gcode_state": gcode_state,
            "error": error,
            "file": file,
            "hms": hms or [],
            "print_error": print_error,
            "ts": time.time(),
        }
        self._on_status(pid, status)

    