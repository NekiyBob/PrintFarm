import time
import bambulabs_api as bl
from io import BytesIO
import time
import yaml
import zipfile
import bambulabs_api as bl
import os
import printer_client
import printer_lan


if __name__ == '__main__':
    with open("printers.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    PRINTERS = cfg["printers"]


    def get_printer(printer_id: str):
        for p in PRINTERS:
            if p["id"] == printer_id:
                return p
        return None
    

    #Нужный для отладки принтер 
    p = get_printer("R1-S2-L2-P5")
    IP = p["ip"]
    SERIAL = p["serial"]
    ACCESS_CODE = p["access_code"]
    path = "jobs/t1.gcode.3mf"

    

    
    printer = printer_lan.Printer(ip=IP, access_code=ACCESS_CODE, serial=SERIAL)
   
    st = printer.getStatus()
    print(st)
   
    
    
    
   
    
    
    



   
    
    


    time.sleep(3)

   

    # Disconnect the mqtt client
    