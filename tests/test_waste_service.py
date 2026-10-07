import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from polar_station_foundation.service import DomainService
from polar_station_foundation.storage import Database
from polar_station_foundation.waste_service import WasteService

REG_V1 = {
    "mixing_rules": {
        "incompatibilities": [["waste_oil", "spent_battery"]],
        "max_total_kg": 500,
    },
    "deadlines": {
        "handover_confirm_hours": 24,
        "staging_max_hours": 168,
        "transit_max_hours": 72,
        "disposal_max_hours": 336,
    },
    "custodian_requirements": {
        "staging": ["yard_license"],
        "cross_camp": [],
        "carrier": ["dangerous_goods_cert"],
        "destination": ["hazardous_waste_license"],
    },
}
REG_V2 = {
    "mixing_rules": {
        "incompatibilities": [
            ["waste_oil", "spent_battery"],
            ["spent_battery", "contaminated_packaging"],
        ],
        "max_total_kg": 300,
    },
    "deadlines": {
        "handover_confirm_hours": 12,
        "staging_max_hours": 96,
        "transit_max_hours": 48,
        "disposal_max_hours": 240,
    },
    "custodian_requirements": {
        "staging": ["yard_license"],
        "cross_camp": [],
        "carrier": ["dangerous_goods_cert"],
        "destination": ["hazardous_waste_license"],
    },
}


class WasteTestBase(unittest.TestCase):
    clock_start = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(self.clock_start)
        self.service = DomainService(self.database, self.clock)
        self.waste = WasteService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="极地局")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        self.service.register_actor(request_id="op1", actor_id="a1", new_actor_id="op1",
                                    display_name="营地甲操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="op2", actor_id="a1", new_actor_id="op2",
                                    display_name="设施操作员", role="operator", organization_id="o1")
        self.service.register_actor(request_id="au1", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="siteA", actor_id="op1", site_id="campA",
                                   organization_id="o1", name="甲营地", timezone_name="UTC")
        self.waste.register_regulation(request_id="reg1", actor_id="a1", regulation_id="reg-v1",
                                       version_label="2025版", content=REG_V1,
                                       effective_from="2025-11-01T00:00:00Z")
        self.waste.register_custodian(request_id="c-campA", actor_id="a1", custodian_id="camp-a",
                                      organization_id="o1", kind="camp", name="甲野外营地",
                                      site_id="campA")
        self.waste.register_custodian(request_id="c-campB", actor_id="a1", custodian_id="camp-b",
                                      organization_id="o1", kind="camp", name="乙野外营地")
        self.waste.register_custodian(request_id="c-yard", actor_id="a1", custodian_id="yard",
                                      organization_id="o1", kind="staging_yard", name="中心暂存场",
                                      qualifications=["yard_license"])
        self.waste.register_custodian(request_id="c-carrier", actor_id="a1",
                                      custodian_id="carrier-1", organization_id="o1",
                                      kind="carrier", name="持证承运队",
                                      qualifications=["dangerous_goods_cert"])
        self.waste.register_custodian(request_id="c-carrier2", actor_id="a1",
                                      custodian_id="carrier-2", organization_id="o1",
                                      kind="carrier", name="无资质承运队")
        self.waste.register_custodian(request_id="c-fac", actor_id="a1",
                                      custodian_id="facility-1", organization_id="o1",
                                      kind="disposal_facility", name="危废处置中心",
                                      qualifications=["hazardous_waste_license"])
        self.waste.create_shipment(request_id="ship", actor_id="op1", shipment_id="s-2026",
                                   title="年度撤站回运", regulation_id="reg-v1")

    def tearDown(self):
        self.database.close()

    def advance(self, **delta):
        self.clock = FixedClock(self.clock_start + timedelta(**delta))
        self.service.clock = self.clock
        self.waste.clock = self.clock

    def gen(self, request_id, item_id, waste_type, kg, origin="camp-a", actor="op1"):
        return self.waste.generate_waste(
            request_id=request_id, actor_id=actor, shipment_id="s-2026",
            waste_type=waste_type, quantity_kg=kg, origin_custodian_id=origin, item_id=item_id)

    def seal(self, request_id, container_id, item_ids, gross, origin="camp-a", regulation=None):
        return self.waste.seal_container(
            request_id=request_id, actor_id="op1", shipment_id="s-2026",
            container_id=container_id, origin_custodian_id=origin, item_ids=item_ids,
            packaging="UN钢箱", gross_kg=gross, regulation_id=regulation)


class FullChainTest(WasteTestBase):
    def test_end_to_end_responsibility_chain_and_closure(self):
        self.gen("g-oil", "item-oil", "waste_oil", 100)
        self.gen("g-pkg", "item-pkg", "contaminated_packaging", 40)
        self.gen("g-bat", "item-bat", "spent_battery", 60)
        # 废油与废电池混装被冻结规则拒绝；废油+包装同箱，电池单独成箱。
        with self.assertRaises(ValidationError):
            self.seal("bad-seal", "box-bad", ["item-oil", "item-bat"], 180)
        self.seal("seal1", "box-1", ["item-oil", "item-pkg"], 160)
        self.seal("seal2", "box-2", ["item-bat"], 80)

        box1 = self.waste.get_container("box-1")
        self.assertEqual("sealed", box1.status)
        self.assertEqual("reg-v1", box1.regulation_id)
        self.assertEqual({"item-oil", "item-pkg"}, set(box1.items))

        # 营地 → 暂存场，双方确认。
        self.waste.propose_handover(
            request_id="m1p", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard",
            container_ids=["box-1", "box-2"], callback_token="cb-m1")
        # 接收方之外的主体不能确认。
        with self.assertRaises(PermissionDenied):
            self.waste.confirm_handover(request_id="m1c-bad", actor_id="op1", move_id=_move_id(self.database, "cb-m1"))
        move1 = _move_id(self.database, "cb-m1")
        self.waste.confirm_handover(request_id="m1c", actor_id="op2", move_id=move1,
                                   callback_token="cb-m1")
        custody = {row["container_id"]: row for row in
                   self.waste.current_custodianship("s-2026")}
        self.assertEqual("yard", custody["box-1"]["current_custodian_id"])
        self.assertEqual("staged", custody["box-1"]["status"])

        # 暂存场 → 乙营地跨营地移交。
        self.waste.propose_handover(
            request_id="m2p", actor_id="op1", shipment_id="s-2026", kind="cross_camp",
            from_custodian_id="yard", to_custodian_id="camp-b", container_ids=["box-1", "box-2"])
        move2 = [r for r in self.waste.open_obligations("s-2026")
                 if r["kind"] == "handover_confirm"][0]["created_move_id"]
        self.waste.confirm_handover(request_id="m2c", actor_id="op2", move_id=move2)
        custody = {row["container_id"]: row for row in
                   self.waste.current_custodianship("s-2026")}
        self.assertEqual("camp-b", custody["box-2"]["current_custodian_id"])

        # 无资质承运队被冻结资格要求拒绝；持证承运队可以承运。
        with self.assertRaises(PermissionDenied):
            self.waste.propose_handover(
                request_id="m3bad", actor_id="op1", shipment_id="s-2026", kind="carrier",
                from_custodian_id="camp-b", to_custodian_id="carrier-2",
                container_ids=["box-1", "box-2"])
        self.waste.propose_handover(
            request_id="m3p", actor_id="op1", shipment_id="s-2026", kind="carrier",
            from_custodian_id="camp-b", to_custodian_id="carrier-1",
            container_ids=["box-1", "box-2"])
        move3 = _latest_move(self.database)
        self.waste.confirm_handover(request_id="m3c", actor_id="op2", move_id=move3)
        self.assertEqual("in_transit", self.waste.get_container("box-1").status)

        # 承运中 → 目的地，接收后才能登记处置。
        with self.assertRaises(ConflictError):
            self.waste.record_disposal(
                request_id="disp-early", actor_id="op2", shipment_id="s-2026",
                container_ids=["box-1", "box-2"], certificate_no="CERT-1",
                disposal_method="焚烧")
        self.waste.propose_handover(
            request_id="m4p", actor_id="op1", shipment_id="s-2026", kind="destination",
            from_custodian_id="carrier-1", to_custodian_id="facility-1",
            container_ids=["box-1", "box-2"])
        move4 = _latest_move(self.database)
        self.waste.confirm_handover(request_id="m4c", actor_id="op2", move_id=move4)
        self.assertEqual("delivered", self.waste.get_container("box-2").status)

        receipt = self.waste.record_disposal(
            request_id="disp", actor_id="op2", shipment_id="s-2026",
            container_ids=["box-1", "box-2"], certificate_no="CERT-2026-001",
            disposal_method="焚烧/固化", evidence_ref="doc://cert/001")
        self.assertFalse(receipt.replayed)
        self.assertEqual("closed", self.waste.get_shipment("s-2026")["status"])
        # 关闭后不能再办理。
        with self.assertRaises(ConflictError):
            self.gen("g-late", "item-late", "waste_oil", 10)

        # 正向溯源覆盖来源到凭证关闭的全部节点。
        types = [e.event_type for e in self.waste.shipment_events("s-2026")]
        self.assertEqual("shipment.created", types[0])
        self.assertIn("waste.generated", types)
        self.assertIn("container.sealed", types)
        self.assertIn("handover.staged", types)
        self.assertIn("handover.transfer", types)
        self.assertIn("handover.carrier", types)
        self.assertIn("handover.delivered", types)
        self.assertIn("disposal.certified", types)
        self.assertEqual("shipment.closed", types[-1])

        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    def test_regulation_freeze_not_rewritten_by_new_version(self):
        self.gen("g1", "item-1", "waste_oil", 50)
        self.gen("g2", "item-2", "contaminated_packaging", 20)
        self.seal("s1", "box-old", ["item-1", "item-2"], 90)
        old_snapshot = json.loads(self.database.connection.execute(
            "SELECT regulation_snapshot_json FROM waste_containers WHERE container_id='box-old'"
        ).fetchone()[0])
        self.assertEqual(500, old_snapshot["mixing_rules"]["max_total_kg"])

        # 新版本发布：电池+包装变为禁配，上限下调。
        self.waste.register_regulation(request_id="reg2", actor_id="a1", regulation_id="reg-v2",
                                       version_label="2026版", content=REG_V2,
                                       effective_from="2026-10-01T00:00:00Z")
        self.gen("g3", "item-3", "spent_battery", 30, origin="camp-a")
        self.gen("g4", "item-4", "contaminated_packaging", 20, origin="camp-a")
        with self.assertRaises(ValidationError):
            self.seal("s2bad", "box-new-bad", ["item-3", "item-4"], 70, regulation="reg-v2")
        self.seal("s2", "box-new", ["item-3"], 50, regulation="reg-v2")
        # 旧箱仍冻结 v1 的规则与哈希，新箱使用 v2。
        self.assertEqual(self.waste.get_container("box-old").regulation_hash,
                         self.waste.get_regulation("reg-v1").content_hash)
        self.assertEqual(self.waste.get_container("box-new").regulation_hash,
                         self.waste.get_regulation("reg-v2").content_hash)
        self.assertEqual(300, self.waste.get_container("box-new")
                         .regulation_snapshot["mixing_rules"]["max_total_kg"])


class ExceptionEventsTest(WasteTestBase):
    def _prepare_box(self):
        self.gen("g1", "item-1", "waste_oil", 100)
        self.gen("g2", "item-2", "contaminated_packaging", 40)
        self.seal("s1", "box-1", ["item-1", "item-2"], 160)

    def test_damage_reversal_and_repack_then_continue(self):
        self._prepare_box()
        events_before = len(self.waste.shipment_events("s-2026"))
        self.waste.record_damage(
            request_id="dmg", actor_id="op1", shipment_id="s-2026", container_id="box-1",
            severity="breached", description="吊运中箱体开裂",
            losses=[{"item_id": "item-1", "lost_kg": 12.5}])
        self.assertEqual(87.5, self.waste.get_item("item-1").current_kg)
        self.assertEqual("damaged", self.waste.get_container("box-1").status)
        # 破损箱不能直接移交，必须重装。
        with self.assertRaises(ConflictError):
            self.waste.propose_handover(
                request_id="bad-move", actor_id="op1", shipment_id="s-2026", kind="staging",
                from_custodian_id="camp-a", to_custodian_id="yard", container_ids=["box-1"])
        self.waste.repack_container(
            request_id="rp", actor_id="op1", shipment_id="s-2026",
            source_container_id="box-1", new_container_id="box-1b",
            packaging="更换UN钢箱", gross_kg=145)
        self.assertEqual("repacked", self.waste.get_container("box-1").status)
        self.assertEqual("sealed", self.waste.get_container("box-1b").status)
        self.assertEqual(87.5, self.waste.get_item("item-1").current_kg)
        self.assertEqual("box-1b", self.waste.get_item("item-1").current_container_id)
        # 旧事实未被回滚：冲销事件追加在原始事件之后。
        types = [e.event_type for e in self.waste.shipment_events("s-2026")]
        self.assertLess(types.index("container.sealed"), types.index("item.quantity_reversed"))
        self.assertLess(types.index("item.quantity_reversed"), types.index("container.repacked"))
        self.assertGreater(len(types), events_before)

        # 箱子反查：组成与重装谱系双向可见。
        inspection = self.waste.inspect_container("box-1b")
        self.assertEqual({"item-1", "item-2"}, {i["item_id"] for i in inspection["current_items"]})
        self.assertEqual("box-1", inspection["lineage"][0]["source_container_id"])
        self.assertEqual("box-1b", inspection["lineage"][0]["new_container_id"])
        old_inspection = self.waste.inspect_container("box-1")
        self.assertEqual("box-1b", old_inspection["lineage"][0]["new_container_id"])

        # 批次反查经过的全部箱子。
        item_events = self.waste.item_events("item-1")
        touched = set()
        for e in item_events:
            if "container_id" in e.payload:
                touched.add(e.payload["container_id"])
            if "source_container_id" in e.payload:
                touched.add(e.payload["source_container_id"])
                touched.add(e.payload["new_container_id"])
        self.assertEqual({"box-1", "box-1b"}, touched)

        # 重装后可继续正常移交。
        self.waste.propose_handover(
            request_id="m1", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard", container_ids=["box-1b"])
        self.waste.confirm_handover(request_id="m1c", actor_id="op2",
                                   move_id=_latest_move(self.database))

    def test_quantity_correction_is_reversal_only(self):
        self._prepare_box()
        sealed = self.waste.inspect_container("box-1")["sealed_composition"]
        self.assertAlmostEqual(100, sealed["composition"][0]["kg"])
        self.waste.correct_quantity(
            request_id="corr", actor_id="op1", shipment_id="s-2026",
            item_id="item-2", new_kg=33, reason="复称多出包装皮重7kg")
        item = self.waste.get_item("item-2")
        self.assertEqual(33, item.current_kg)
        # 封箱快照里的旧数量不变，差异体现在 variance 视图。
        sealed = self.waste.inspect_container("box-1")["sealed_composition"]
        self.assertAlmostEqual(40, [c for c in sealed["composition"]
                                    if c["item_id"] == "item-2"][0]["kg"])
        variances = self.waste.quantity_variances("s-2026")
        item_variance = [v for v in variances["items"] if v["item_id"] == "item-2"][0]
        self.assertEqual(7, item_variance["variance_kg"])
        self.assertEqual(1, len(item_variance["reversals"]))
        box_variance = [v for v in variances["containers"] if v["container_id"] == "box-1"][0]
        self.assertEqual(7, box_variance["variance_kg"])
        # 冲减不能超过在管量。
        with self.assertRaises(ValidationError):
            self.waste.correct_quantity(
                request_id="corr2", actor_id="op1", shipment_id="s-2026",
                item_id="item-2", new_kg=-1, reason="非法")

    def test_rejected_handover_keeps_custody_and_reproposal_works(self):
        self._prepare_box()
        self.waste.propose_handover(
            request_id="m1p", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard", container_ids=["box-1"])
        move = _latest_move(self.database)
        self.waste.reject_handover(request_id="m1r", actor_id="op2", move_id=move,
                                   reason="随附许可文件缺失")
        self.assertEqual("camp-a", self.waste.get_container("box-1").current_custodian_id)
        self.assertEqual("sealed", self.waste.get_container("box-1").status)
        # 已拒绝的单不能再确认。
        with self.assertRaises(ConflictError):
            self.waste.confirm_handover(request_id="m1c-late", actor_id="op2", move_id=move)
        # 重新提出移交并确认，责任继续流转。
        self.waste.propose_handover(
            request_id="m2p", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard", container_ids=["box-1"])
        self.waste.confirm_handover(request_id="m2c", actor_id="op2",
                                   move_id=_latest_move(self.database))
        self.assertEqual("yard", self.waste.get_container("box-1").current_custodian_id)

    def test_cancel_proposed_but_confirmed_move_is_immutable(self):
        self._prepare_box()
        self.waste.propose_handover(
            request_id="m1p", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard",
            container_ids=["box-1"], callback_token="cb-x")
        move = _latest_move(self.database)
        self.waste.cancel_handover(request_id="m1x", actor_id="op2", move_id=move,
                                   reason="补给航班取消")
        self.assertEqual("camp-a", self.waste.get_container("box-1").current_custodian_id)
        # 已取消的单不能确认。
        with self.assertRaises(ConflictError):
            self.waste.confirm_handover(request_id="m1c", actor_id="op2", move_id=move)
        # 重新发运并确认后，不允许取消，只能走回程。
        self.waste.propose_handover(
            request_id="m2p", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard", container_ids=["box-1"])
        move2 = _latest_move(self.database)
        self.waste.confirm_handover(request_id="m2c", actor_id="op2", move_id=move2)
        with self.assertRaises(ConflictError):
            self.waste.cancel_handover(request_id="m2x", actor_id="op2", move_id=move2,
                                       reason="想撤销")
        events = [e.event_type for e in self.waste.shipment_events("s-2026")]
        self.assertEqual(events.count("handover.cancelled"), 1)
        self.assertEqual(events.count("handover.staged"), 1)


class IdempotencyTest(WasteTestBase):
    def _prepare(self):
        self.gen("g1", "item-1", "waste_oil", 100)
        self.seal("s1", "box-1", ["item-1"], 120)
        self.waste.propose_handover(
            request_id="mp", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard",
            container_ids=["box-1"], callback_token="cb-1")

    def test_duplicate_confirmation_and_callback_are_idempotent(self):
        self._prepare()
        move = _latest_move(self.database)
        first = self.waste.confirm_handover(request_id="mc", actor_id="op2", move_id=move,
                                            callback_token="cb-1")
        second = self.waste.confirm_handover(request_id="mc", actor_id="op2", move_id=move,
                                             callback_token="cb-1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        # 回调重试用新 request_id 携带同一 token：不产生第二条确认、不改责任。
        types_before = [e.event_type for e in self.waste.shipment_events("s-2026")]
        retried = self.waste.confirm_handover(request_id="mc-retry-1", actor_id="op1",
                                              move_id=move, callback_token="cb-1")
        self.assertFalse(retried.replayed)  # 新的业务请求编号
        types_after = [e.event_type for e in self.waste.shipment_events("s-2026")]
        self.assertEqual(types_before, types_after)
        self.assertEqual(1, types_after.count("handover.staged"))
        # 再重复一次重试请求，按 request_id 回放。
        retried_again = self.waste.confirm_handover(request_id="mc-retry-1", actor_id="op1",
                                                    move_id=move, callback_token="cb-1")
        self.assertTrue(retried_again.replayed)

    def test_same_request_id_with_changed_payload_conflicts(self):
        self._prepare()
        move = _latest_move(self.database)
        self.waste.confirm_handover(request_id="mc", actor_id="op2", move_id=move)
        with self.assertRaises(ConflictError):
            self.waste.confirm_handover(request_id="mc", actor_id="op2", move_id="other-move")

    def test_state_recovers_after_process_restart(self):
        self._prepare()
        move = _latest_move(self.database)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "waste.sqlite3"
            # 把内存库快照写入一个全新文件库，再用新进程视角重新打开。
            import sqlite3
            raw = sqlite3.connect(path)
            raw.executescript("\n".join(self.database.connection.iterdump()))
            raw.commit()
            raw.close()
            file_db = Database(path)
            restarted = WasteService(file_db, FixedClock(
                datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)))
            # 旧的 propose 请求重放不重复建单。
            replay = restarted.propose_handover(
                request_id="mp", actor_id="op1", shipment_id="s-2026", kind="staging",
                from_custodian_id="camp-a", to_custodian_id="yard",
                container_ids=["box-1"], callback_token="cb-1")
            self.assertTrue(replay.replayed)
            self.assertEqual(move, replay.resource_id)
            # 恢复后继续办理确认。
            receipt = restarted.confirm_handover(request_id="mc", actor_id="op2", move_id=move)
            self.assertFalse(receipt.replayed)
            self.assertEqual("yard", restarted.get_container("box-1").current_custodian_id)
            valid, _count = DomainService(file_db).verify_audit()
            self.assertTrue(valid)
            file_db.close()


class DeadlineTest(WasteTestBase):
    def test_overdue_obligations_are_detected(self):
        self.gen("g1", "item-1", "waste_oil", 100)
        self.seal("s1", "box-1", ["item-1"], 120)
        self.waste.propose_handover(
            request_id="mp", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard", container_ids=["box-1"])
        move = _latest_move(self.database)
        # 10 小时后：确认期限 24h，尚未逾期。
        self.advance(hours=10)
        self.assertEqual([], self.waste.overdue("s-2026"))
        # 30 小时后仍未确认：确认义务逾期，责任方是接收方暂存场。
        self.advance(hours=30)
        overdue = self.waste.overdue("s-2026")
        self.assertEqual(1, len(overdue))
        self.assertEqual("handover_confirm", overdue[0]["kind"])
        self.assertEqual("yard", overdue[0]["responsible_custodian_id"])
        # 确认后逾期义务核销，并产生暂存期限新义务。
        self.waste.confirm_handover(request_id="mc", actor_id="op2", move_id=move)
        self.assertEqual([], self.waste.overdue("s-2026"))
        obligations = {o["kind"]: o for o in self.waste.open_obligations("s-2026")}
        self.assertIn("staging", obligations)
        self.assertEqual("yard", obligations["staging"]["responsible_custodian_id"])
        # 超过暂存 168h 上限后再次逾期。
        self.advance(days=10)
        overdue = self.waste.overdue("s-2026")
        self.assertEqual("staging", overdue[0]["kind"])

    def test_obligations_are_settled_when_stage_advances(self):
        self.gen("g1", "item-1", "waste_oil", 100)
        self.seal("s1", "box-1", ["item-1"], 120)
        # 暂存确认 -> 产生 staging 义务；发运确认 -> 旧 staging 义务核销。
        self.waste.propose_handover(
            request_id="mp", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard", container_ids=["box-1"])
        self.waste.confirm_handover(request_id="mc", actor_id="op2",
                                   move_id=_latest_move(self.database))
        self.assertIn("staging", {o["kind"] for o in self.waste.open_obligations("s-2026")})
        # 暂存场发运给乙营地（cross_camp 确认后仍处 staged，staging 义务保留）。
        self.waste.propose_handover(
            request_id="m2p", actor_id="op2", shipment_id="s-2026", kind="cross_camp",
            from_custodian_id="yard", to_custodian_id="camp-b", container_ids=["box-1"])
        self.waste.confirm_handover(request_id="m2c", actor_id="op1",
                                   move_id=_latest_move(self.database))
        open_kinds = {o["kind"] for o in self.waste.open_obligations("s-2026")}
        self.assertIn("staging", open_kinds)
        # 乙营地交承运确认后进入 in_transit，staging 义务核销、transit 义务产生。
        self.waste.propose_handover(
            request_id="m3p", actor_id="op1", shipment_id="s-2026", kind="carrier",
            from_custodian_id="camp-b", to_custodian_id="carrier-1",
            container_ids=["box-1"])
        self.waste.confirm_handover(request_id="m3c", actor_id="op2",
                                   move_id=_latest_move(self.database))
        open_kinds = {o["kind"] for o in self.waste.open_obligations("s-2026")}
        self.assertNotIn("staging", open_kinds)
        self.assertIn("transit", open_kinds)


class PermissionAndValidationTest(WasteTestBase):
    def test_auditor_cannot_write_waste(self):
        with self.assertRaises(PermissionDenied):
            self.waste.generate_waste(
                request_id="x", actor_id="au1", shipment_id="s-2026",
                waste_type="waste_oil", quantity_kg=10, origin_custodian_id="camp-a")

    def test_gross_weight_below_net_rejected(self):
        self.gen("g1", "item-1", "waste_oil", 100)
        with self.assertRaises(ValidationError):
            self.seal("s1", "box-1", ["item-1"], 50)

    def test_max_total_kg_enforced_from_frozen_snapshot(self):
        self.gen("g1", "item-1", "waste_oil", 510)
        with self.assertRaises(ValidationError):
            self.seal("s1", "box-1", ["item-1"], 530)

    def test_unknown_shipment_and_item(self):
        with self.assertRaises(NotFoundError):
            self.waste.seal_container(
                request_id="s1", actor_id="op1", shipment_id="nope",
                container_id="box-x", origin_custodian_id="camp-a",
                item_ids=["item-1"], packaging="箱", gross_kg=10)

    def test_invalid_regulation_content_rejected(self):
        with self.assertRaises(ValueError):
            self.waste.register_regulation(
                request_id="bad", actor_id="a1", regulation_id="reg-bad",
                version_label="x", content={"mixing_rules": {"incompatibilities": []}},
                effective_from="2026-01-01T00:00:00Z")


class VarianceReportingTest(WasteTestBase):
    def test_transit_damage_shortfall_is_explained_by_reversal(self):
        self.gen("g1", "item-1", "waste_oil", 100)
        self.seal("s1", "box-1", ["item-1"], 120)
        self.waste.propose_handover(
            request_id="mp", actor_id="op1", shipment_id="s-2026", kind="staging",
            from_custodian_id="camp-a", to_custodian_id="yard", container_ids=["box-1"])
        self.waste.confirm_handover(request_id="mc", actor_id="op2",
                                   move_id=_latest_move(self.database))
        # 跨营地到乙营地，随后承运确认，清单记录 100kg。
        self.waste.propose_handover(
            request_id="m2p", actor_id="op2", shipment_id="s-2026", kind="cross_camp",
            from_custodian_id="yard", to_custodian_id="camp-b", container_ids=["box-1"])
        self.waste.confirm_handover(request_id="m2c", actor_id="op1",
                                   move_id=_latest_move(self.database))
        self.waste.propose_handover(
            request_id="m3p", actor_id="op1", shipment_id="s-2026", kind="carrier",
            from_custodian_id="camp-b", to_custodian_id="carrier-1",
            container_ids=["box-1"])
        self.waste.confirm_handover(request_id="m3c", actor_id="op2",
                                   move_id=_latest_move(self.database))
        self.waste.record_damage(
            request_id="dmg", actor_id="op2", shipment_id="s-2026", container_id="box-1",
            severity="breached", description="泄漏",
            losses=[{"item_id": "item-1", "lost_kg": 8}])
        variances = self.waste.quantity_variances("s-2026")
        shortfalls = [s for s in variances["manifest_shortfalls"]
                      if s["item_id"] == "item-1"]
        # 承运清单 100kg、当前 92kg：8kg 短少被破损冲销事件完全解释。
        carrier_shortfall = [s for s in shortfalls if s["kind"] == "carrier"][0]
        self.assertEqual(8, carrier_shortfall["shortfall_kg"])
        self.assertEqual(8, carrier_shortfall["explained_by_reversals_kg"])
        self.assertEqual(0, carrier_shortfall["unexplained_kg"])
        # 破损箱的净重差异不报为异常（待重装）。
        box = [c for c in variances["containers"] if c["container_id"] == "box-1"][0]
        self.assertEqual(8, box["variance_kg"])


def _move_id(database: Database, callback_token: str) -> str:
    return database.connection.execute(
        "SELECT move_id FROM waste_moves WHERE callback_token=?", (callback_token,)
    ).fetchone()["move_id"]


def _latest_move(database: Database) -> str:
    return database.connection.execute(
        "SELECT move_id FROM waste_moves ORDER BY proposed_at DESC, rowid DESC LIMIT 1"
    ).fetchone()["move_id"]


if __name__ == "__main__":
    unittest.main()
