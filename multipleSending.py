import ftplib
import ssl
import time
import socket
import time
import bambulabs_api as bl
import time
import yaml


# Настроить SSL-контекст: при необходимости отключаем проверку сертификата (для самоподписанного сертификата принтера)
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
user = "bblp"    # имя пользователя Bambu Lab (фиксированное)
# По желанию, можно указать конкретную версию TLS:
# context.minimum_version = ssl.TLSVersion.TLSv1_2  # требовать хотя бы TLS 1.2

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
        return conn, size

def upload_file_to_printer(
    host: str,
    user: str,
    password: str,
    local_path: str,
    remote_dir: str = "/",
    blocksize = 32 * 1024,   # размер блока 32 KB
):
    ftps = None
    try:
        ftps = ImplicitFTP_TLS()
        ftps.connect(host=host, port=990, timeout=10)
        ftps.login(user=user, passwd=password)
        ftps.prot_p()  # шифруем data-канал

        # Переходим в нужную папку на принтере
        if remote_dir:
            ftps.cwd(remote_dir)

        # Имя файла
        import os
        filename = os.path.basename(local_path)
        filesize = os.path.getsize(local_path)

        print(f"Загрузка '{filename}' ({filesize/1024/1024:.2f} MB)...")

        bytes_sent = 0
        last_update = 0
        # Функция для отображения прогресса
        def handle_block(block):
            nonlocal bytes_sent, last_update
            bytes_sent += len(block)
            now = time.time()
            if now - last_update > 0.3:  # обновлять 3 раза в секунду
                percent = bytes_sent / filesize * 100
                print(f"\rПрогресс: {percent:6.2f}%", end="")
                last_update = now

        ftps.sock.settimeout(30)

        # Загружаем файл
        with open(local_path, "rb") as f:
            ftps.storbinary(f"STOR {filename}", f, blocksize, callback=handle_block)
        print("\nЗагрузка завершена!")

    except (socket.timeout, ssl.SSLError, *ftplib.all_errors) as e:
        print(f"\nОшибка при загрузке файла: {e}")
    finally:
        if ftps:
            try:
                if ftps.sock:
                    ftps.quit()
            except:
                try:
                    ftps.close()
                except:
                    pass


def upload_on_rack(yaml_f:str, file_path:str, row : int, rack : int):
    with open("printers.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    PRINTERS = cfg["printers"]

    #Printers from same rack and row
    accessible_printers = []
    for p in PRINTERS:
        if p["row"] == row and p["rack"] == rack and p["access_code"] != "":
            accessible_printers.append(p)
    
    # Upload File and start it
    for p in accessible_printers:
        ip = p["ip"]
        access_code = p["access_code"] 
        serial = p["serial"]
        
        try:
            ftps = ImplicitFTP_TLS()
            ftps.connect(host=ip, port=990, timeout=10)
            ftps.login(user=user, passwd=access_code)
            ftps.prot_p()
            upload_file_to_printer(ip, user, access_code, file_path)


        except (socket.timeout, ssl.SSLError, *ftplib.all_errors) as e:
            # Обработка ошибок соединения, TLS и FTP
            print(f"Ошибка при работе с FTPS: {e}")
        finally:
            if ftps:
                try:
                    # Проверяем, открыт ли сокет
                    if ftps.sock:
                        ftps.quit()
                except Exception as e:
                    print(f"Ошибка при закрытии соединения: {e}")
                    try:
                        ftps.close()
                    except:
                        pass
        # Запуск загруженного файла 
        printer = bl.Printer(ip, access_code, serial)
        printer.mqtt_start()

        time.sleep(0.5)

        print(printer.start_print(file_path,1))

        time.sleep(0.5)
        printer.mqtt_stop()


#Вызов метода
upload_on_rack("printers.yaml","t1.gcode.3mf", 1, 2)