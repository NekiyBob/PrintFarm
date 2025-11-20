import time
import bambulabs_api as bl
from io import BytesIO
import time
import zipfile
import bambulabs_api as bl
import os

IP = '192.168.1.130'
SERIAL = '00M09D461602386'
ACCESS_CODE = '241cf96e'

if __name__ == '__main__':
    print('Starting bambulabs_api example')
    print('Connecting to Bambulabs 3D printer')
    print(f'IP: {IP}')
    print(f'Serial: {SERIAL}')
    print(f'Access Code: {ACCESS_CODE}')

    # Create a new instance of the API
    printer = bl.Printer(IP, ACCESS_CODE, SERIAL)

    # Connect to the Bambulabs 3D printer without connecting to the camera
    printer.mqtt_start()

    time.sleep(2)

    # Get the printer status
    status = printer.get_state()
    print(f'Printer status: {status}')
    print(printer.get_bed_temperature())
    # with open(r"t1.gcode.3mf", "rb") as f:
    #     a = printer.upload_file(f, 't1.gcode.3mf')
    #     print(a)

    #==================================================================PRINT FILE ===================================================

    printer.start_print("t1.gcode.3mf", 1)
    


    time.sleep(2)

   

    # Disconnect the mqtt client
    printer.mqtt_stop()