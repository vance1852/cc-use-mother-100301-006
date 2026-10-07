import unittest

from polar_station_foundation.waste_acceptance import run


class WasteAcceptanceTest(unittest.TestCase):
    def test_offline_waste_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["mixed_loading_rejected"])
        self.assertTrue(result["unqualified_carrier_blocked"])
        self.assertEqual(2, result["overdue_seen_before_confirm"])
        self.assertEqual(0, result["overdue_cleared_after_confirm"])
        self.assertEqual(120.0, result["oil_initial_kg"])
        self.assertEqual(0.0, result["oil_current_kg"])
        self.assertEqual("repacked", result["old_box_status"])
        self.assertEqual(["box-a2"], result["new_box_lineage"])
        self.assertTrue(result["item_trace_covers_repack"])
        self.assertTrue(result["event_chain_has_reversal"])
        self.assertTrue(result["event_chain_ends_closed"])
        self.assertEqual("closed", result["shipment_status"])
        self.assertTrue(result["audit_valid"])


if __name__ == "__main__":
    unittest.main()
