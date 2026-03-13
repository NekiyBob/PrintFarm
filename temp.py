import zipfile
import printer_lan, printer_client

ip = "192.168.1.153"
ac = "4868cf88"
serial = "00M09A3A1700267"
p = printer_lan.Printer(ip,serial,ac)
print(p.getStatus())