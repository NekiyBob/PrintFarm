import time
import bambulabs_api as bl
from io import BytesIO
import time
import yaml
import zipfile
import bambulabs_api as bl
import os
import printer_client
from bambulabs_api import PrinterMQTTClient
import datetime


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
    p = get_printer("R2-S3-L1-P6")
    IP = p["ip"]
    SERIAL = p["serial"]
    ACCESS_CODE = p["access_code"]
    path = "jobs/t1.gcode.3mf"


    # printer_client.upload_file_to_printer(IP, ACCESS_CODE, path)
    # Create a new instance of the API
    printer = bl.Printer(IP, ACCESS_CODE, SERIAL)

    # Connect to the Bambulabs 3D printer without connecting to the camera

    printer.mqtt_start()
    time.sleep(2)

    print(printer.pause_print())
    print(printer.get_state())
    print(printer.gcode_file())
    print(printer.gcode())
    # while True:
    #         time.sleep(5)

    #         # Get the printer status
    #         status = printer.get_state()
    #         percentage = printer.get_percentage()
    #         layer_num = printer.current_layer_num()
    #         total_layer_num = printer.total_layer_num()
    #         bed_temperature = printer.get_bed_temperature()
    #         nozzle_temperature = printer.get_nozzle_temperature()
    #         remaining_time = printer.get_time()
    #         if remaining_time is not None:
    #             finish_time = datetime.datetime.now() + datetime.timedelta(
    #                 minutes=int(remaining_time))
    #             finish_time_format = finish_time.strftime("%Y-%m-%d %H:%M:%S")
    #         else:
    #             finish_time_format = "NA"

    #         print(
    #             f'''Printer status: {status}
    #             Layers: {layer_num}/{total_layer_num}
    #             percentage: {percentage}%
    #             Bed temp: {bed_temperature} ºC
    #             Nozzle temp: {nozzle_temperature} ºC
    #             Remaining time: {remaining_time}m
    #             Finish time: {finish_time_format}
    #             '''
    #         )


    # print("AFTER START ")
    # print(printer.mqtt_client_connected())
    # print(printer.mqtt_client_ready())

    # # printer.start_print("t1.gcode.3mf", 1, False,flow_calibration=False)

    # print(printer.get_bed_temperature())

    # # print(str(printer.current_layer_num()) + " / " + str(printer.total_layer_num()))

    # print(printer.get_time())

    
    


    # time.sleep(1)
    # # Disconnect the mqtt client
    # printer.mqtt_stop()

 


    '''
    Работает:
    
    '''
