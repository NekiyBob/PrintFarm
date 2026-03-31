import unittest

from status_utils import extract_loaded_material_from_payload


class ExtractLoadedMaterialTests(unittest.TestCase):
    def test_extracts_material_from_vt_tray(self):
        payload = {
            "print": {
                "vt_tray": {
                    "tray_type": "PLA",
                }
            }
        }

        self.assertEqual(extract_loaded_material_from_payload(payload), "PLA")

    def test_extracts_material_from_virtual_slot_targeted_by_p2s(self):
        payload = {
            "print": {
                "ams": {
                    "tray_now": "0",
                    "tray_tar": "255",
                },
                "vir_slot": [
                    {
                        "id": "255",
                        "tray_type": "PA-CF",
                        "tray_info_idx": "GFN04",
                    }
                ],
            }
        }

        self.assertEqual(extract_loaded_material_from_payload(payload), "PA-CF")

    def test_falls_back_to_first_virtual_slot_with_material(self):
        payload = {
            "print": {
                "vir_slot": [
                    {
                        "id": "3",
                        "tray_type": "",
                        "tray_info_idx": "",
                    },
                    {
                        "id": "4",
                        "tray_type": "PETG",
                    },
                ]
            }
        }

        self.assertEqual(extract_loaded_material_from_payload(payload), "PETG")

    def test_falls_back_to_cached_status_when_payload_has_no_material(self):
        payload = {"print": {}}
        cached_status = {"loaded_material": "ABS"}

        self.assertEqual(
            extract_loaded_material_from_payload(payload, cached_status=cached_status),
            "ABS",
        )


if __name__ == "__main__":
    unittest.main()
