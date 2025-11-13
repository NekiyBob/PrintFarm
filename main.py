import ftplib
import ssl
import socket

# Настроить SSL-контекст: при необходимости отключаем проверку сертификата (для самоподписанного сертификата принтера)
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
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
    remote_dir: str = "/",   # можно поменять на нужную папку
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

        # Имя файла на принтере
        import os
        filename = os.path.basename(local_path)

        # Открываем локальный файл в бинарном режиме и отправляем
        with open(local_path, "rb") as f:
            cmd = f"STOR {filename}"
            ftps.storbinary(cmd, f)

        print(f"Файл '{filename}' отправлен в '{remote_dir}' на принтер.")

        # Можно проверить, что файл появился:
        print("Содержимое директории после загрузки:")
        for name in ftps.nlst():
            print(" ", name)

    except (socket.timeout, ssl.SSLError, *ftplib.all_errors) as e:
        print(f"Ошибка при загрузке файла: {e}")
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


# Параметры подключения
host = "192.168.1.130"      # IP-адрес принтера в сети
user = "bblp"            # имя пользователя Bambu Lab (фиксированное)
password = "241cf96e" # пароль: код доступа с экрана принтера
local_path = "C:\Work\PrintFarm\Екулеоуцке.3mf"
ftps = None
try:
    ftps = ImplicitFTP_TLS()
    ftps.connect(host=host, port=990, timeout=10)
    ftps.login(user=user, passwd=password)
    ftps.prot_p()
    # список файлов
    files = ftps.nlst()
    print("Список файлов на SD-карте принтера:")
    for filename in files:
        print(filename)
    upload_file_to_printer(host, user, password, local_path)


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
