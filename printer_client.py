import ftplib
import ssl
import socket
import bambulabs_api as bl
import time
import printer_lan
import os

# Настроить SSL-контекст: при необходимости отключаем проверку сертификата (для самоподписанного сертификата принтера)
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE


class ImplicitFTP_TLS(ftplib.FTP_TLS):
    """Подкласс FTP_TLS для implicit FTPS: сразу устанавливает TLS на сокете."""
    def __init__(self, *args, **kwargs):
        # Используем наш SSL-контекст, если не задан другой
        if 'context' not in kwargs:
            kwargs['context'] = context
        super().__init__(*args, **kwargs)
        self._sock = None  # инициализация собственного атрибута сокета

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        # При установке сокета оборачиваем его в TLS, если он ещё не SSLSocket
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        """Переопределение для повторного использования TLS-сессии на дата-соединениях."""
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:  # если установлен защищенный режим для данных
            # Оборачиваем data-socket в TLS, используя ту же сессию, что и у контрол-сокета
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        # ускорение
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        try:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
        except Exception:
            pass
        
        conn.settimeout(180)
        return conn, size





def start_print_on_printer(
    ip: str,
    access_code: str,
    serial: str,
    filename: str,
    plate_num: int = 1,
) -> None:
    print(f"[PRINT] Start {filename} on {ip} ({serial}) plate={plate_num}")
    printer = printer_lan.Printer(ip=ip, serial=serial, access_code=access_code)
    
    ok = printer.start_print(filename_on_sd=filename, plate_num=plate_num, timeout=45)
    if not ok:
        # Fallback: подождать чуть-чуть и спросить статус 1-2 раза
        time.sleep(2.0)
        st = printer.getStatus(timeout=6.0)

        # Попробуем вытащить причину
        pr = st.get("print") or {}
        gcode_state = pr.get("gcode_state")
        err = pr.get("print_error") or pr.get("err") or pr.get("fail_reason")

        if err:
            raise RuntimeError(f"start_print not confirmed, state={gcode_state}, err={err}")
        else:
            raise RuntimeError(f"start_print not confirmed, state={gcode_state}")





def upload_file_to_printer(
    host: str,
    password: str,
    local_path: str,
    remote_dir: str = "/",
    user: str = "bblp",
    blocksize=256 * 1024,
):
    ftps = None
    try:
        ftps = ImplicitFTP_TLS()
        ftps.connect(host=host, port=990, timeout=20)
        ftps.login(user=user, passwd=password)
        ftps.prot_p()
        ftps.sock.settimeout(180)  # контрольный канал тоже держим подольше


        if remote_dir:
            ftps.cwd(remote_dir)

        filename = os.path.basename(local_path)
        filesize = os.path.getsize(local_path)

        print(f"Загрузка '{filename}' ({filesize/1024/1024:.2f} MB)...")

        bytes_sent = 0
        last_update = 0

        def handle_block(block):
            nonlocal bytes_sent, last_update
            bytes_sent += len(block)
            now = time.time()
            if now - last_update > 3:
                percent = bytes_sent / filesize * 100
                print(f"\n[{host}] Прогресс: {percent:6.2f}%\n", flush=True)

                last_update = now

        # Важно: таймауты лучше больше
        ftps.sock.settimeout(120)

        with open(local_path, "rb") as f:
            resp = ftps.storbinary(f"STOR {filename}", f, blocksize, callback=handle_block)

        print("\nЗагрузка завершена! FTP:", resp)

        # Проверка результата
        if not resp or not str(resp).startswith("226"):
            raise RuntimeError(f"FTP upload failed, server reply: {resp}")

        return resp

    except (socket.timeout, ssl.SSLError, *ftplib.all_errors) as e:
        # ВАЖНО: не print, а raise (чтобы retry и job-ошибка сработали)
        raise RuntimeError(f"FTPS upload error: {e}") from e

    finally:
        if ftps:
            try:
                if ftps.sock:
                    ftps.quit()
            except Exception:
                try:
                    ftps.close()
                except Exception:
                    pass
