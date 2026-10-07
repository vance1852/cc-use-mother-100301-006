import unittest

from polar_station_foundation.api import route
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database
from tests.test_waste_service import REG_V1


class WasteApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = DomainService(self.database)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="极地局")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                    display_name="营地操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="接收操作员", role="operator", organization_id="o1")
        self._call("POST", "/waste/regulations", {
            "request_id": "reg1", "actor_id": "a1", "regulation_id": "reg-v1",
            "version_label": "2025版", "content": REG_V1,
            "effective_from": "2025-11-01T00:00:00Z"}, actor="a1")
        self._call("POST", "/waste/custodians", {
            "request_id": "c1", "actor_id": "a1", "custodian_id": "camp-a",
            "organization_id": "o1", "kind": "camp", "name": "甲营地"}, actor="a1")
        self._call("POST", "/waste/custodians", {
            "request_id": "c2", "actor_id": "a1", "custodian_id": "yard",
            "organization_id": "o1", "kind": "staging_yard", "name": "暂存场",
            "qualifications": ["yard_license"]}, actor="a1")
        self._call("POST", "/waste/shipments", {
            "request_id": "s1", "actor_id": "op1", "shipment_id": "ship-1",
            "title": "回运", "regulation_id": "reg-v1"})

    def tearDown(self):
        self.database.close()

    def _call(self, method, path, body=None, actor="op1"):
        return route(self.service, method, path, body or {},
                     {"X-Actor-Id": actor})

    def _seed_box(self):
        status, _ = self._call("POST", "/waste/items", {
            "request_id": "g1", "actor_id": "op1", "shipment_id": "ship-1",
            "waste_type": "waste_oil", "quantity_kg": 100,
            "origin_custodian_id": "camp-a", "item_id": "item-1"})
        self.assertEqual(201, status)
        status, payload = self._call("POST", "/waste/containers/seal", {
            "request_id": "seal1", "actor_id": "op1", "shipment_id": "ship-1",
            "container_id": "box-1", "origin_custodian_id": "camp-a",
            "item_ids": ["item-1"], "packaging": "钢箱", "gross_kg": 120})
        self.assertEqual(201, status)
        return payload

    def test_seal_replay_returns_200_same_resource(self):
        body = {
            "request_id": "g1", "actor_id": "op1", "shipment_id": "ship-1",
            "waste_type": "waste_oil", "quantity_kg": 100,
            "origin_custodian_id": "camp-a", "item_id": "item-1"}
        self.assertEqual(201, self._call("POST", "/waste/items", body)[0])
        self.assertEqual(200, self._call("POST", "/waste/items", body)[0])

    def test_mixed_loading_violation_returns_400(self):
        self._call("POST", "/waste/items", {
            "request_id": "g1", "actor_id": "op1", "shipment_id": "ship-1",
            "waste_type": "waste_oil", "quantity_kg": 10,
            "origin_custodian_id": "camp-a", "item_id": "i-oil"})
        self._call("POST", "/waste/items", {
            "request_id": "g2", "actor_id": "op1", "shipment_id": "ship-1",
            "waste_type": "spent_battery", "quantity_kg": 10,
            "origin_custodian_id": "camp-a", "item_id": "i-bat"})
        status, payload = self._call("POST", "/waste/containers/seal", {
            "request_id": "sealbad", "actor_id": "op1", "shipment_id": "ship-1",
            "container_id": "box-x", "origin_custodian_id": "camp-a",
            "item_ids": ["i-oil", "i-bat"], "packaging": "箱", "gross_kg": 40})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_confirmed_handover_shows_on_dashboard(self):
        self._seed_box()
        status, payload = self._call("POST", "/waste/handovers", {
            "request_id": "mp", "actor_id": "op1", "shipment_id": "ship-1",
            "kind": "staging", "from_custodian_id": "camp-a",
            "to_custodian_id": "yard", "container_ids": ["box-1"]})
        self.assertEqual(201, status)
        move_id = payload["resource_id"]
        status, payload = self._call("POST", "/waste/handovers/confirm", {
            "request_id": "mc", "actor_id": "op2", "move_id": move_id}, actor="op2")
        self.assertEqual(201, status)
        status, dashboard = self._call("GET", "/waste/shipments/ship-1/dashboard", {})
        self.assertEqual(200, status)
        self.assertEqual("yard", dashboard["custodianship"][0]["current_custodian_id"])

    def test_duplicate_confirm_callback_replays_without_new_event(self):
        self._seed_box()
        _, payload = self._call("POST", "/waste/handovers", {
            "request_id": "mp", "actor_id": "op1", "shipment_id": "ship-1",
            "kind": "staging", "from_custodian_id": "camp-a",
            "to_custodian_id": "yard", "container_ids": ["box-1"]})
        move_id = payload["resource_id"]
        self._call("POST", "/waste/handovers/confirm",
                   {"request_id": "mc", "actor_id": "op2", "move_id": move_id}, actor="op2")
        status, retry = self._call("POST", "/waste/handovers/confirm", {
            "request_id": "mc-retry", "actor_id": "op2", "move_id": move_id}, actor="op2")
        self.assertEqual(201, status)
        self.assertEqual(move_id, retry["resource_id"])
        _, events = route(self.service, "GET", "/waste/shipments/ship-1/events", {},
                          {"X-Actor-Id": "op1"})
        self.assertEqual(1, sum(1 for e in events["items"]
                                if e["event_type"] == "handover.staged"))
        # 同一重试请求再次送达，按 request_id 回放。
        status, replay = self._call("POST", "/waste/handovers/confirm", {
            "request_id": "mc-retry", "actor_id": "op2", "move_id": move_id}, actor="op2")
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

    def test_container_inspection_and_item_trace(self):
        self._seed_box()
        status, payload = self._call("GET", "/waste/containers/box-1", {})
        self.assertEqual(200, status)
        self.assertEqual(["item-1"], [i["item_id"] for i in payload["current_items"]])
        self.assertEqual("reg-v1", payload["container"]["regulation_id"])
        status, payload = self._call("GET", "/waste/items/item-1/events", {})
        self.assertEqual(200, status)
        self.assertEqual({"waste.generated", "container.sealed"},
                         {e["event_type"] for e in payload["items"]})


if __name__ == "__main__":
    unittest.main()
