import unittest

from printer_lan import _normalize_print_state, _print_has_started


class PrinterLanStartHeuristicsTests(unittest.TestCase):
    def test_p1s_inferrs_running_from_remaining_time(self):
        print_block = {"mc_remaining_time": 42}

        self.assertEqual(_normalize_print_state("P1S", print_block), "RUNNING")
        self.assertTrue(_print_has_started("P1S", print_block))

    def test_p1s_inferrs_running_from_progress(self):
        print_block = {"mc_percent": 5}

        self.assertEqual(_normalize_print_state("P1S", print_block), "RUNNING")
        self.assertTrue(_print_has_started("P1S", print_block))

    def test_explicit_prepare_state_counts_as_started(self):
        print_block = {"gcode_state": "PREPARE"}

        self.assertEqual(_normalize_print_state("X1C", print_block), "PREPARE")
        self.assertTrue(_print_has_started("X1C", print_block))

    def test_idle_like_payload_does_not_count_as_started(self):
        print_block = {"mc_percent": 0, "mc_remaining_time": 0}

        self.assertIsNone(_normalize_print_state("P1S", print_block))
        self.assertFalse(_print_has_started("P1S", print_block))


if __name__ == "__main__":
    unittest.main()
