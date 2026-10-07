import unittest

from waste_return_chain.acceptance import run


class WasteAcceptanceTest(unittest.TestCase):
    def test_waste_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual("disposed", result["oil_final"])
        self.assertEqual({"bat": 60.0, "pkg": 40.0}, result["repacked_composition"])
        # 旧箱冻结在 v1，重装新箱适用 v2。
        self.assertEqual("reg-v1", result["frozen_regulation"])
        self.assertEqual("reg-v2", result["repacked_under_regulation"])
        self.assertEqual(3, result["lots_closed"])


if __name__ == "__main__":
    unittest.main()
