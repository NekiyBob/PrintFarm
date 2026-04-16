import zipfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional


def get_total_weight_from_gcode_3mf(path: str) -> Optional[float]:
    lower_name = Path(path).name.lower()
    if not (lower_name.endswith(".3mf") or lower_name.endswith(".gcode.3mf")):
        return None

    try:
        with zipfile.ZipFile(path, "r") as archive:
            with archive.open("Metadata/slice_info.config") as handle:
                xml_data = handle.read()

        root = ET.fromstring(xml_data)
    except (OSError, KeyError, zipfile.BadZipFile, zipfile.LargeZipFile, ET.ParseError):
        return None

    total_g = Decimal("0")
    found = False

    for filament in root.iter("filament"):
        used_g = filament.attrib.get("used_g")
        if not used_g:
            continue

        try:
            total_g += Decimal(used_g)
            found = True
        except (InvalidOperation, ValueError):
            continue

    if not found:
        return None

    return float(total_g)
