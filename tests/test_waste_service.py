import unittest
from datetime import datetime, timedelta, timezone

from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import (
    ConflictError, NotFoundError, PermissionDenied, ValidationError,
)

from waste_return_chain.service import WasteService
from waste_return_chain.storage import WasteDatabase


V1 = {
    "mixing_rules": {"forbidden_pairs": [
        ["waste_oil", "waste_battery"],
        ["waste_oil", "contaminated_packaging"]]},
    "deadlines": {"storage_due_hours": 72, "transfer_confirm_due_hours": 24,
                  "receipt_due_hours": 48},
    "responsibility_roles": {"origin_manager": "主管", "transporter": "承运",
                             "receiver": "接收"},
}
# V2 放宽混装（电池可与包装同箱）、缩短期限。
V2 = {
    "mixing_rules": {"forbidden_pairs": [["waste_oil", "waste_battery"]]},
    "deadlines": {"storage_due_hours": 24, "transfer_confirm_due_hours": 12,
                  "receipt_due_hours": 24},
    "responsibility_roles": {"origin_manager": "主管", "transporter": "承运",
                             "receiver": "接收"},
}


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def advance(self, hours):
        self.t += timedelta(hours=hours)


class WasteBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.database = WasteDatabase()
        self.service = WasteService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org", actor_id="bootstrap",
                                organization_id="o1", name="机构")
        s.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad",
                         display_name="管理员", role="admin", organization_id="o1")
        s.register_actor(request_id="op", actor_id="ad", new_actor_id="op",
                         display_name="营地员", role="operator", organization_id="o1")
        s.register_actor(request_id="car", actor_id="ad", new_actor_id="car",
                         display_name="承运员", role="operator", organization_id="o1")
        s.register_actor(request_id="rcv", actor_id="ad", new_actor_id="rcv",
                         display_name="接收员", role="reviewer", organization_id="o1")
        for rid, sid in [("sa", "camp"), ("sb", "hub"), ("sf", "facility")]:
            s.register_site(request_id=rid, actor_id="op", site_id=sid,
                            organization_id="o1", name=sid, timezone_name="UTC")
        s.publish_regulation(request_id="reg1", actor_id="ad", version_id="v1", payload=V1)

    def tearDown(self):
        self.database.close()

    def lot(self, lot_id, category, qty, unit, permit):
        self.service.register_lot(request_id=f"lot-{lot_id}", actor_id="op", site_id="camp",
                                  lot_id=lot_id, waste_category=category, hazard_class="H",
                                  quantity=qty, unit=unit, packaging="封装", permit_id=permit)

    def three_lots(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P-OIL")
        self.lot("bat", "waste_battery", 60.0, "kg", "P-BAT")
        self.lot("pkg", "contaminated_packaging", 40.0, "kg", "P-PKG")


class RegulationTest(WasteBase):
    def test_sealing_freezes_regulation_and_new_version_keeps_old_fact(self):
        self.three_lots()
        self.service.seal_container(request_id="seal", actor_id="op", container_id="b1",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])
        self.clock.advance(48)
        self.service.publish_regulation(request_id="reg2", actor_id="ad",
                                        version_id="v2", payload=V2)
        box = self.service.get_container("b1")
        self.assertEqual("v1", box.regulation_version_id)
        self.assertEqual(V1["deadlines"], box.regulation_snapshot["deadlines"])

    def test_invalid_regulation_payload_rejected(self):
        bad = {"mixing_rules": {}, "deadlines": {"storage_due_hours": 1},
               "responsibility_roles": {}}
        with self.assertRaises(ValidationError):
            self.service.publish_regulation(request_id="regbad", actor_id="ad",
                                            version_id="vbad", payload=bad)

    def test_sealing_without_any_regulation_rejected(self):
        db = WasteDatabase()
        svc = WasteService(db, FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc)))
        svc.register_organization(request_id="oo", actor_id="bootstrap",
                                  organization_id="oo", name="x")
        svc.register_actor(request_id="aa", actor_id="bootstrap", new_actor_id="pp",
                           display_name="p", role="admin", organization_id="oo")
        svc.register_site(request_id="ss", actor_id="pp", site_id="zz", organization_id="oo",
                          name="z", timezone_name="UTC")
        svc.register_lot(request_id="ll", actor_id="pp", site_id="zz", lot_id="l1",
                         waste_category="waste_oil", hazard_class="H", quantity=1,
                         unit="L", packaging="x", permit_id="p")
        with self.assertRaises(ValidationError):
            svc.seal_container(request_id="cc", actor_id="pp", container_id="c1",
                               entries=[{"lot_id": "l1", "quantity": 1, "unit": "L"}])
        db.close()


class SealingTest(WasteBase):
    def test_mixing_oil_and_battery_forbidden(self):
        self.three_lots()
        with self.assertRaises(ValidationError):
            self.service.seal_container(request_id="bad", actor_id="op", container_id="bx",
                                        entries=[{"lot_id": "oil", "quantity": 5, "unit": "L"},
                                                 {"lot_id": "bat", "quantity": 5, "unit": "kg"}])

    def test_seal_cannot_exceed_registered_quantity(self):
        self.lot("oil", "waste_oil", 10.0, "L", "P")
        self.service.seal_container(request_id="s1", actor_id="op", container_id="b1",
                                    entries=[{"lot_id": "oil", "quantity": 8, "unit": "L"}])
        with self.assertRaises(ConflictError):
            self.service.seal_container(request_id="s2", actor_id="op", container_id="b2",
                                        entries=[{"lot_id": "oil", "quantity": 3, "unit": "L"}])

    def test_unit_must_match_lot(self):
        self.lot("oil", "waste_oil", 10.0, "L", "P")
        with self.assertRaises(ValidationError):
            self.service.seal_container(request_id="ss", actor_id="op", container_id="box",
                                        entries=[{"lot_id": "oil", "quantity": 8, "unit": "kg"}])

    def test_duplicate_lot_in_manifest_rejected(self):
        self.lot("oil", "waste_oil", 10.0, "L", "P")
        with self.assertRaises(ValidationError):
            self.service.seal_container(request_id="ss", actor_id="op", container_id="box",
                                        entries=[{"lot_id": "oil", "quantity": 4, "unit": "L"},
                                                 {"lot_id": "oil", "quantity": 4, "unit": "L"}])


class CorrectionTest(WasteBase):
    def test_correction_is_appended_and_original_preserved(self):
        self.lot("oil", "waste_oil", 10.0, "L", "P")
        original = self.service.get_lot("oil")
        self.service.correct_lot_quantity(request_id="c1", actor_id="op", lot_id="oil",
                                          delta=-2, reason="盘点")
        self.assertEqual(10.0, original.quantity)  # 原始登记不变
        variance = {v["lot_id"]: v for v in self.service.quantity_variance()}
        self.assertEqual(8.0, variance["oil"]["registered_effective"])
        self.assertEqual(-2.0, variance["oil"]["correction_total"])
        trace = self.service.trace_lot("oil")
        self.assertEqual(1, len(trace["corrections"]))

    def test_correction_cannot_make_quantity_negative(self):
        self.lot("oil", "waste_oil", 10.0, "L", "P")
        with self.assertRaises(ValidationError):
            self.service.correct_lot_quantity(request_id="cc", actor_id="op", lot_id="oil",
                                              delta=-20, reason="x")


class TransferTest(WasteBase):
    def _sealed_box(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])

    def test_proposer_cannot_confirm(self):
        self._sealed_box()
        t = self.service.propose_transfer(
            request_id="tt", actor_id="op", container_id="box", transfer_type="cross_camp",
            from_party="营地", to_party="中转", from_site_id="camp", to_site_id="hub")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_transfer(request_id="tc", actor_id="op",
                                          transfer_id=t.resource_id)

    def test_responsibility_moves_only_on_confirmation(self):
        self._sealed_box()
        t = self.service.propose_transfer(
            request_id="tt", actor_id="op", container_id="box", transfer_type="cross_camp",
            from_party="营地", to_party="中转", from_site_id="camp", to_site_id="hub")
        # 未确认时责任仍在起点。
        self.assertEqual("op", self.service.current_responsibility("box")["responsible_party"])
        self.service.confirm_transfer(request_id="tc", actor_id="car",
                                      transfer_id=t.resource_id)
        self.assertEqual("中转", self.service.current_responsibility("box")["responsible_party"])

    def test_cancel_proposed_transfer_but_confirmed_cannot_cancel(self):
        self._sealed_box()
        t = self.service.propose_transfer(
            request_id="tt", actor_id="op", container_id="box", transfer_type="cross_camp",
            from_party="营地", to_party="中转", from_site_id="camp", to_site_id="hub")
        self.service.cancel_transfer(request_id="tx", actor_id="op",
                                     transfer_id=t.resource_id, reason="天气取消")
        with self.assertRaises(ConflictError):
            self.service.confirm_transfer(request_id="tc", actor_id="car",
                                          transfer_id=t.resource_id)

    def test_confirmed_transfer_cannot_be_cancelled_only_returned(self):
        self._sealed_box()
        t = self.service.propose_transfer(
            request_id="tt", actor_id="op", container_id="box", transfer_type="carrier_handover",
            from_party="营地", to_party="承运", from_site_id="camp")
        self.service.confirm_transfer(request_id="tc", actor_id="car",
                                      transfer_id=t.resource_id)
        with self.assertRaises(ConflictError):
            self.service.cancel_transfer(request_id="tx", actor_id="op",
                                         transfer_id=t.resource_id, reason="x")

    def test_carrier_cancellation_uses_return_transfer_without_rollback(self):
        self._sealed_box()
        t = self.service.propose_transfer(
            request_id="tt", actor_id="op", container_id="box", transfer_type="carrier_handover",
            from_party="营地", to_party="承运", from_site_id="camp")
        self.service.confirm_transfer(request_id="tc", actor_id="car",
                                      transfer_id=t.resource_id)
        self.assertEqual("in_transit", self.service.get_container("box").status)
        # 运输取消：冲销回运，原承运确认记录保留。
        back = self.service.propose_transfer(
            request_id="back", actor_id="car", container_id="box",
            transfer_type="return_transfer", from_party="承运", to_party="营地",
            from_site_id="camp", to_site_id="camp", reversal_of=t.resource_id)
        self.service.confirm_transfer(request_id="backc", actor_id="op",
                                      transfer_id=back.resource_id)
        self.assertEqual("at_camp", self.service.get_container("box").status)
        original = next(x for x in self.service.list_transfers("box")
                        if x.transfer_id == t.resource_id)
        self.assertEqual("confirmed", original.status)

    def test_receive_rejection_then_return(self):
        self._sealed_box()
        carrier = self.service.propose_transfer(
            request_id="t1", actor_id="op", container_id="box",
            transfer_type="carrier_handover", from_party="营地", to_party="承运",
            from_site_id="camp")
        self.service.confirm_transfer(request_id="t1c", actor_id="car",
                                      transfer_id=carrier.resource_id)
        dest = self.service.propose_transfer(
            request_id="t2", actor_id="car", container_id="box",
            transfer_type="destination_delivery", from_party="承运", to_party="设施",
            from_site_id="camp", to_site_id="facility")
        self.service.confirm_transfer(request_id="t2c", actor_id="rcv",
                                      transfer_id=dest.resource_id)
        self.service.reject_received_container(request_id="rej", actor_id="rcv",
                                               container_id="box", reason="破损")
        # 拒收后不能直接承运，只能回运。
        with self.assertRaises(ConflictError):
            self.service.propose_transfer(
                request_id="bad", actor_id="op", container_id="box",
                transfer_type="carrier_handover", from_party="设施", to_party="承运",
                from_site_id="facility")
        back = self.service.propose_transfer(
            request_id="back", actor_id="rcv", container_id="box",
            transfer_type="return_transfer", from_party="设施", to_party="营地",
            from_site_id="facility", to_site_id="camp", reversal_of=dest.resource_id)
        self.service.confirm_transfer(request_id="backc", actor_id="op",
                                      transfer_id=back.resource_id)
        self.assertEqual("at_camp", self.service.get_container("box").status)


class DamagedRepackTest(WasteBase):
    def test_damaged_box_must_repack_and_lineage_is_traced(self):
        self.three_lots()
        self.service.seal_container(
            request_id="seal", actor_id="op", container_id="box",
            entries=[{"lot_id": "bat", "quantity": 60, "unit": "kg"},
                     {"lot_id": "pkg", "quantity": 40, "unit": "kg"}])
        self.service.mark_container_damaged(request_id="dmg", actor_id="op",
                                            container_id="box", description="开裂")
        self.assertEqual("damaged", self.service.get_container("box").status)
        with self.assertRaises(ConflictError):
            self.service.propose_transfer(
                request_id="ship", actor_id="op", container_id="box",
                transfer_type="carrier_handover", from_party="营地", to_party="承运",
                from_site_id="camp")
        self.clock.advance(48)
        self.service.publish_regulation(request_id="reg2", actor_id="ad",
                                        version_id="v2", payload=V2)
        self.service.repack_container(
            request_id="rp", actor_id="op", new_container_id="b2", site_id="camp",
            items=[{"lot_id": "bat", "quantity": 60, "unit": "kg",
                    "source_container_id": "box"},
                   {"lot_id": "pkg", "quantity": 40, "unit": "kg",
                    "source_container_id": "box"}])
        self.assertEqual("repacked", self.service.get_container("box").status)
        self.assertEqual("v2", self.service.get_container("b2").regulation_version_id)
        comp = {c["lot_id"]: c["quantity"]
                for c in self.service.trace_container("b2")["composition"]}
        self.assertEqual({"bat": 60.0, "pkg": 40.0}, comp)
        # 旧箱仍可反查原始组成。
        old = {c["lot_id"]: c["quantity"]
               for c in self.service.trace_container("box")["composition"]}
        self.assertEqual({"bat": 60.0, "pkg": 40.0}, old)

    def test_partial_repack_leaves_balance(self):
        self.lot("bat", "waste_battery", 60.0, "kg", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "bat", "quantity": 60, "unit": "kg"}])
        self.service.mark_container_damaged(request_id="dmg", actor_id="op",
                                            container_id="box", description="x")
        self.service.repack_container(
            request_id="rp", actor_id="op", new_container_id="b2", site_id="camp",
            items=[{"lot_id": "bat", "quantity": 25, "unit": "kg",
                    "source_container_id": "box"}])
        # 旧箱仍持有 35，状态保持 damaged。
        self.assertEqual("damaged", self.service.get_container("box").status)
        with self.assertRaises(ConflictError):
            self.service.repack_container(
                request_id="rp2", actor_id="op", new_container_id="b3", site_id="camp",
                items=[{"lot_id": "bat", "quantity": 40, "unit": "kg",
                        "source_container_id": "box"}])
        self.service.repack_container(
            request_id="rp3", actor_id="op", new_container_id="b3", site_id="camp",
            items=[{"lot_id": "bat", "quantity": 35, "unit": "kg",
                    "source_container_id": "box"}])
        self.assertEqual("repacked", self.service.get_container("box").status)


class ReceiptDisposalTest(WasteBase):
    def _deliver(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])
        c = self.service.propose_transfer(
            request_id="t1", actor_id="op", container_id="box",
            transfer_type="carrier_handover", from_party="营地", to_party="承运",
            from_site_id="camp")
        self.service.confirm_transfer(request_id="t1c", actor_id="car",
                                      transfer_id=c.resource_id)
        d = self.service.propose_transfer(
            request_id="t2", actor_id="car", container_id="box",
            transfer_type="destination_delivery", from_party="承运", to_party="设施",
            from_site_id="camp", to_site_id="facility")
        self.service.confirm_transfer(request_id="t2c", actor_id="rcv",
                                      transfer_id=d.resource_id)
        return d.resource_id

    def test_receipt_with_wrong_quantity_rejected(self):
        dest = self._deliver()
        with self.assertRaises(ConflictError):
            self.service.acknowledge_receipt(
                request_id="rr", actor_id="rcv", transfer_id=dest,
                items=[{"lot_id": "oil", "quantity": 90, "unit": "L"}])

    def test_full_close_and_trace(self):
        dest = self._deliver()
        self.service.acknowledge_receipt(request_id="rr", actor_id="rcv", transfer_id=dest)
        self.service.certify_disposal(request_id="cert", actor_id="rcv", container_id="box",
                                      facility_site_id="facility", certificate_ref="C-1")
        self.assertEqual("closed", self.service.get_container("box").status)
        resp = self.service.current_responsibility("box")
        self.assertTrue(resp["responsibility_closed"])
        trace = self.service.trace_lot("oil")
        self.assertEqual("disposed", trace["final_disposition"])
        self.assertEqual("C-1", trace["certificates"][0]["certificate_ref"])

    def test_duplicate_callbacks_idempotent_even_after_close(self):
        dest = self._deliver()
        first = self.service.acknowledge_receipt(request_id="rr", actor_id="rcv",
                                                 transfer_id=dest)
        replay = self.service.acknowledge_receipt(request_id="rr", actor_id="rcv",
                                                  transfer_id=dest)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        cert = self.service.certify_disposal(request_id="cert", actor_id="rcv",
                                             container_id="box", facility_site_id="facility",
                                             certificate_ref="C-1")
        cert_replay = self.service.certify_disposal(request_id="cert", actor_id="rcv",
                                                    container_id="box",
                                                    facility_site_id="facility",
                                                    certificate_ref="C-1")
        self.assertFalse(cert.replayed)
        self.assertTrue(cert_replay.replayed)
        # 不同 request_id 重复处置必须拒绝。
        with self.assertRaises(ConflictError):
            self.service.certify_disposal(request_id="cert2", actor_id="rcv",
                                          container_id="box", facility_site_id="facility",
                                          certificate_ref="C-2")


class QueryTest(WasteBase):
    def test_overdue_storage_and_pending_transfer(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])
        self.service.check_in_storage(request_id="in", actor_id="op", container_id="box",
                                      site_id="camp", keeper="仓管", handed_by="op")
        self.clock.advance(80)
        overdue = self.service.overdue_nodes()
        self.assertEqual(["box"], [x["container_id"] for x in overdue["overdue_storage"]])

    def test_overdue_pending_transfer(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])
        self.service.propose_transfer(
            request_id="tt", actor_id="op", container_id="box", transfer_type="cross_camp",
            from_party="营地", to_party="中转", from_site_id="camp", to_site_id="hub")
        self.clock.advance(30)
        overdue = self.service.overdue_nodes()
        self.assertEqual(1, len(overdue["pending_transfers"]))

    def test_variance_reports_open_responsibility(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 60, "unit": "L"}])
        row = next(v for v in self.service.quantity_variance() if v["lot_id"] == "oil")
        self.assertEqual(40.0, row["unaccounted_vs_registered"])
        self.assertTrue(row["open_responsibility"])


class IdempotencyTest(WasteBase):
    def test_same_request_replays_after_state_advanced(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])
        self.service.check_in_storage(request_id="in", actor_id="op", container_id="box",
                                      site_id="camp", keeper="k", handed_by="op")
        # 已入库后重放入库请求必须核销，而不是报“已有暂存”。
        replay = self.service.check_in_storage(request_id="in", actor_id="op",
                                               container_id="box", site_id="camp",
                                               keeper="k", handed_by="op")
        self.assertTrue(replay.replayed)

    def test_same_request_with_changed_payload_conflicts(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])
        with self.assertRaises(ConflictError):
            self.service.seal_container(request_id="seal", actor_id="op",
                                        container_id="b-other",
                                        entries=[{"lot_id": "oil", "quantity": 1, "unit": "L"}])


class StorageTest(WasteBase):
    def test_storage_keeper_is_current_responsibility(self):
        self.lot("oil", "waste_oil", 100.0, "L", "P")
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])
        self.service.check_in_storage(request_id="in", actor_id="op", container_id="box",
                                      site_id="camp", keeper="仓管", handed_by="op")
        self.assertEqual("仓管",
                         self.service.current_responsibility("box")["responsible_party"])
        # 暂存中不能移交。
        with self.assertRaises(ConflictError):
            self.service.propose_transfer(
                request_id="tt", actor_id="op", container_id="box",
                transfer_type="carrier_handover", from_party="营地", to_party="承运",
                from_site_id="camp")
        self.service.check_out_storage(request_id="out", actor_id="op", container_id="box",
                                       released_by="仓管")
        self.assertEqual("sealed", self.service.get_container("box").status)


class AuditTest(WasteBase):
    def test_audit_chain_intact(self):
        self.three_lots()
        self.service.seal_container(request_id="seal", actor_id="op", container_id="box",
                                    entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}])
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
