import json
import unittest

from waste_return_chain.api import route
from waste_return_chain.service import WasteService
from waste_return_chain.storage import WasteDatabase


REG = {
    "mixing_rules": {"forbidden_pairs": [
        ["waste_oil", "waste_battery"],
        ["waste_oil", "contaminated_packaging"]]},
    "deadlines": {"storage_due_hours": 72, "transfer_confirm_due_hours": 24,
                  "receipt_due_hours": 48},
    "responsibility_roles": {"origin_manager": "主管", "transporter": "承运",
                             "receiver": "接收"},
}


class WasteApiTest(unittest.TestCase):
    def setUp(self):
        self.database = WasteDatabase()
        self.service = WasteService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "机构"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "ad", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "op", "new_actor_id": "op", "display_name": "营地员",
               "role": "operator", "organization_id": "o1"},
              {"X-Actor-Id": "ad"})
        route(self.service, "POST", "/actors",
              {"request_id": "car", "new_actor_id": "car", "display_name": "承运员",
               "role": "operator", "organization_id": "o1"},
              {"X-Actor-Id": "ad"})
        route(self.service, "POST", "/actors",
              {"request_id": "rcv", "new_actor_id": "rcv", "display_name": "接收员",
               "role": "reviewer", "organization_id": "o1"},
              {"X-Actor-Id": "ad"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "camp", "organization_id": "o1",
               "name": "营地", "timezone_name": "UTC"},
              {"X-Actor-Id": "op"})
        route(self.service, "POST", "/waste/regulations",
              {"request_id": "reg", "version_id": "v1", "payload": REG},
              {"X-Actor-Id": "ad"})
        route(self.service, "POST", "/waste/lots",
              {"request_id": "lot", "site_id": "camp", "lot_id": "oil",
               "waste_category": "waste_oil", "hazard_class": "Y9", "quantity": 100,
               "unit": "L", "packaging": "钢桶", "permit_id": "P-1"},
              {"X-Actor-Id": "op"})
        route(self.service, "POST", "/waste/lots",
              {"request_id": "lotbat", "site_id": "camp", "lot_id": "bat",
               "waste_category": "waste_battery", "hazard_class": "Y31", "quantity": 50,
               "unit": "kg", "packaging": "防泄漏箱", "permit_id": "P-2"},
              {"X-Actor-Id": "op"})

    def tearDown(self):
        self.database.close()

    def test_health_still_served_by_foundation(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_illegal_mixing_returns_400(self):
        status, payload = route(self.service, "POST", "/waste/containers/seal",
                                {"request_id": "bad", "container_id": "bx",
                                 "entries": [{"lot_id": "oil", "quantity": 5, "unit": "L"},
                                             {"lot_id": "bat", "quantity": 5, "unit": "kg"}]},
                                {"X-Actor-Id": "op"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_full_chain_over_http_and_trace(self):
        status, payload = route(self.service, "POST", "/waste/containers/seal",
                                {"request_id": "seal", "container_id": "box",
                                 "entries": [{"lot_id": "oil", "quantity": 100, "unit": "L"}],
                                 "gross_weight": 150},
                                {"X-Actor-Id": "op"})
        self.assertEqual(201, status)

        status, payload = route(self.service, "GET",
                                "/waste/containers?container_id=box", None)
        self.assertEqual(200, status)
        self.assertEqual("v1", payload["regulation_version_id"])

        status, payload = route(self.service, "POST", "/waste/transfers/propose",
                                {"request_id": "t1", "container_id": "box",
                                 "transfer_type": "carrier_handover",
                                 "from_party": "营地", "to_party": "承运",
                                 "from_site_id": "camp"},
                                {"X-Actor-Id": "op"})
        transfer_id = payload["resource_id"]
        status, payload = route(self.service, "POST", "/waste/transfers/confirm",
                                {"request_id": "t1c", "transfer_id": transfer_id},
                                {"X-Actor-Id": "car"})
        self.assertEqual(200, status)

        status, payload = route(self.service, "GET",
                                "/waste/containers/responsibility?container_id=box", None)
        self.assertEqual(200, status)
        self.assertEqual("承运", payload["responsible_party"])
        self.assertTrue(payload["in_transit"])

        status, payload = route(self.service, "GET", "/waste/quantity-variance", None)
        self.assertEqual(200, status)
        self.assertEqual({"oil", "bat"}, {item["lot_id"] for item in payload["items"]})
        oil_row = next(item for item in payload["items"] if item["lot_id"] == "oil")
        self.assertEqual(100, oil_row["sealed_quantity"])

        status, payload = route(self.service, "GET",
                                "/waste/trace/lot?lot_id=oil", None)
        self.assertEqual(200, status)
        self.assertEqual("oil", payload["lot"]["lot_id"])

    def test_unknown_waste_route_404(self):
        status, payload = route(self.service, "GET", "/waste/nope", None)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_permission_denied_maps_403(self):
        status, payload = route(self.service, "POST", "/waste/regulations",
                                {"request_id": "regx", "version_id": "vx", "payload": REG},
                                {"X-Actor-Id": "op"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])


if __name__ == "__main__":
    unittest.main()
