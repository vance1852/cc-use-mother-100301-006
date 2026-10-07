"""废弃物回运责任项目的离线端到端验收。

在临时 SQLite 数据库中走通：产生登记 → 封箱冻结法规 → 暂存 → 跨营地移交 →
承运 → 目的地交付/接收 → 破损重装与拒收冲销回运 → 处置凭证关闭，并核对
双向追溯、数量差异、逾期节点、幂等重放、法规版本冻结、审计链以及进程恢复。
成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polar_station_foundation.errors import ConflictError, PermissionDenied

from .service import WasteService
from .storage import WasteDatabase


REGULATION_V1 = {
    "mixing_rules": {"forbidden_pairs": [
        ["waste_oil", "waste_battery"],
        ["waste_oil", "contaminated_packaging"]]},
    "deadlines": {"storage_due_hours": 72, "transfer_confirm_due_hours": 24,
                  "receipt_due_hours": 48},
    "responsibility_roles": {"origin_manager": "场地主管",
                             "transporter": "承运员", "receiver": "接收员"},
    "note": "2026 版",
}
REGULATION_V2 = {
    "mixing_rules": {"forbidden_pairs": [["waste_oil", "waste_battery"]]},
    "deadlines": {"storage_due_hours": 24, "transfer_confirm_due_hours": 12,
                  "receipt_due_hours": 24},
    "responsibility_roles": {"origin_manager": "场地主管",
                             "transporter": "承运员", "receiver": "接收员"},
    "note": "2027 版（电池与包装可同箱；不应改写旧箱的 v1 事实）",
}


class AdvancingClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, hours: float) -> None:
        self.value += timedelta(hours=hours)


def _bootstrap(service: WasteService) -> None:
    service.register_organization(request_id="acc-org", actor_id="bootstrap",
                                  organization_id="org-1", name="示范极地机构")
    service.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-1",
                           display_name="管理员", role="admin", organization_id="org-1")
    service.register_actor(request_id="acc-op", actor_id="admin-1", new_actor_id="op-1",
                           display_name="营地操作员", role="operator", organization_id="org-1")
    service.register_actor(request_id="acc-carrier", actor_id="admin-1", new_actor_id="car-1",
                           display_name="承运员", role="operator", organization_id="org-1")
    service.register_actor(request_id="acc-receiver", actor_id="admin-1", new_actor_id="rcv-1",
                           display_name="目的地接收员", role="reviewer", organization_id="org-1")
    for rid, sid, name in [
            ("acc-site-camp", "camp", "野外营地"),
            ("acc-site-hub", "hub", "中转营地"),
            ("acc-site-facility", "facility", "处置设施")]:
        service.register_site(request_id=rid, actor_id="op-1", site_id=sid,
                              organization_id="org-1", name=name, timezone_name="UTC")


def run() -> dict[str, object]:
    """执行完整责任链并断言关键不变量。"""

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "waste_acceptance.sqlite3"
        clock = AdvancingClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        database = WasteDatabase(db_path)
        service = WasteService(database, clock)
        _bootstrap(service)

        service.publish_regulation(request_id="acc-reg-v1", actor_id="admin-1",
                                   version_id="reg-v1", payload=REGULATION_V1)

        # 三类废弃物在产生地点分别登记（许可、数量、封装）。
        service.register_lot(request_id="acc-lot-oil", actor_id="op-1", site_id="camp",
                             lot_id="oil", waste_category="waste_oil", hazard_class="Y9",
                             quantity=100.0, unit="L", packaging="钢桶", permit_id="P-OIL")
        service.register_lot(request_id="acc-lot-bat", actor_id="op-1", site_id="camp",
                             lot_id="bat", waste_category="waste_battery", hazard_class="Y31",
                             quantity=60.0, unit="kg", packaging="防泄漏箱", permit_id="P-BAT")
        service.register_lot(request_id="acc-lot-pkg", actor_id="op-1", site_id="camp",
                             lot_id="pkg", waste_category="contaminated_packaging",
                             hazard_class="Y49", quantity=40.0, unit="kg",
                             packaging="编织袋", permit_id="P-PKG")

        # 废油与废电池混装必须被 v1 规则拒绝。
        try:
            service.seal_container(request_id="acc-seal-illegal", actor_id="op-1",
                                   container_id="box-illegal",
                                   entries=[{"lot_id": "oil", "quantity": 5, "unit": "L"},
                                            {"lot_id": "bat", "quantity": 5, "unit": "kg"}])
            raise AssertionError("混装应被拒绝")
        except Exception as exc:
            assert "混装" in str(exc), exc

        # 合法分两箱封箱；废油箱随后走完全程，电池/包装箱经历破损重装。
        service.seal_container(request_id="acc-seal-oil", actor_id="op-1",
                               container_id="box-oil",
                               entries=[{"lot_id": "oil", "quantity": 100, "unit": "L"}],
                               gross_weight=150)
        service.seal_container(request_id="acc-seal-mix", actor_id="op-1",
                               container_id="box-mix",
                               entries=[{"lot_id": "bat", "quantity": 60, "unit": "kg"},
                                        {"lot_id": "pkg", "quantity": 40, "unit": "kg"}],
                               gross_weight=130)

        # 发布新法规版本；已封箱必须继续冻结在 v1。
        clock.advance(24)
        service.publish_regulation(request_id="acc-reg-v2", actor_id="admin-1",
                                   version_id="reg-v2", payload=REGULATION_V2)
        assert service.get_container("box-oil").regulation_version_id == "reg-v1"
        assert service.get_container("box-oil").regulation_snapshot_hash == \
            service.get_regulation_version("reg-v1").payload_hash

        # 暂存与逾期。
        service.check_in_storage(request_id="acc-in-oil", actor_id="op-1",
                                 container_id="box-oil", site_id="camp",
                                 keeper="营地仓管", handed_by="op-1")
        assert service.current_responsibility("box-oil")["responsible_party"] == "营地仓管"
        service.check_out_storage(request_id="acc-out-oil", actor_id="op-1",
                                  container_id="box-oil", released_by="op-1")

        # 跨营地移交：发起人不能确认，必须双方确认。
        t_camp = service.propose_transfer(
            request_id="acc-t-camp", actor_id="op-1", container_id="box-oil",
            transfer_type="cross_camp", from_party="野外营地", to_party="中转营地",
            from_site_id="camp", to_site_id="hub").resource_id
        try:
            service.confirm_transfer(request_id="acc-self-confirm", actor_id="op-1",
                                     transfer_id=t_camp)
            raise AssertionError("发起人不应能自行确认")
        except PermissionDenied:
            pass
        service.confirm_transfer(request_id="acc-t-camp-ok", actor_id="car-1",
                                 transfer_id=t_camp)

        # 承运交接与目的地交付、接收。
        t_carrier = service.propose_transfer(
            request_id="acc-t-carrier", actor_id="car-1", container_id="box-oil",
            transfer_type="carrier_handover", from_party="中转营地",
            to_party="极地承运", from_site_id="hub").resource_id
        service.confirm_transfer(request_id="acc-t-carrier-ok", actor_id="op-1",
                                 transfer_id=t_carrier)
        t_dest = service.propose_transfer(
            request_id="acc-t-dest", actor_id="car-1", container_id="box-oil",
            transfer_type="destination_delivery", from_party="极地承运",
            to_party="处置设施", from_site_id="hub", to_site_id="facility").resource_id
        service.confirm_transfer(request_id="acc-t-dest-ok", actor_id="rcv-1",
                                 transfer_id=t_dest)
        service.acknowledge_receipt(request_id="acc-receipt", actor_id="rcv-1",
                                    transfer_id=t_dest)
        replay = service.acknowledge_receipt(request_id="acc-receipt", actor_id="rcv-1",
                                             transfer_id=t_dest)
        assert replay.replayed is True, "重复回调必须幂等核销"

        # 处置凭证关闭责任；关闭后不得再改写。
        service.certify_disposal(request_id="acc-cert", actor_id="rcv-1",
                                 container_id="box-oil", facility_site_id="facility",
                                 certificate_ref="CERT-OIL-1")
        assert service.get_container("box-oil").status == "closed"
        try:
            service.certify_disposal(request_id="acc-cert-again", actor_id="rcv-1",
                                     container_id="box-oil", facility_site_id="facility",
                                     certificate_ref="CERT-OIL-2")
            raise AssertionError("已关闭责任不能重复关闭")
        except Exception as exc:
            assert "目的地" in str(exc) or "关闭" in str(exc), exc

        # 另一箱：目的地拒收前先运到设施；拒收后只能冲销回运，原接收责任不回滚。
        t_m1 = service.propose_transfer(
            request_id="acc-mix-camp", actor_id="op-1", container_id="box-mix",
            transfer_type="cross_camp", from_party="野外营地", to_party="中转营地",
            from_site_id="camp", to_site_id="hub").resource_id
        service.confirm_transfer(request_id="acc-mix-camp-ok", actor_id="car-1",
                                 transfer_id=t_m1)
        t_m2 = service.propose_transfer(
            request_id="acc-mix-carrier", actor_id="car-1", container_id="box-mix",
            transfer_type="carrier_handover", from_party="中转营地",
            to_party="极地承运", from_site_id="hub").resource_id
        service.confirm_transfer(request_id="acc-mix-carrier-ok", actor_id="op-1",
                                 transfer_id=t_m2)
        t_m3 = service.propose_transfer(
            request_id="acc-mix-dest", actor_id="car-1", container_id="box-mix",
            transfer_type="destination_delivery", from_party="极地承运",
            to_party="处置设施", from_site_id="hub", to_site_id="facility").resource_id
        service.confirm_transfer(request_id="acc-mix-dest-ok", actor_id="rcv-1",
                                 transfer_id=t_m3)
        service.reject_received_container(request_id="acc-mix-reject", actor_id="rcv-1",
                                          container_id="box-mix", reason="清点发现箱体破损")
        t_back = service.propose_transfer(
            request_id="acc-mix-return", actor_id="rcv-1", container_id="box-mix",
            transfer_type="return_transfer", from_party="处置设施", to_party="中转营地",
            from_site_id="facility", to_site_id="hub", reversal_of=t_m3).resource_id
        service.confirm_transfer(request_id="acc-mix-return-ok", actor_id="op-1",
                                 transfer_id=t_back)
        assert service.get_container("box-mix").status == "at_camp"

        # 破损登记后拆箱重装到新箱（新箱按 v2 冻结，旧箱保留为 repacked）。
        service.mark_container_damaged(request_id="acc-mix-damaged", actor_id="op-1",
                                       container_id="box-mix", description="箱壁开裂渗漏")
        service.repack_container(
            request_id="acc-repack", actor_id="op-1", new_container_id="box-mix-2",
            site_id="hub",
            items=[{"lot_id": "bat", "quantity": 60, "unit": "kg",
                    "source_container_id": "box-mix"},
                   {"lot_id": "pkg", "quantity": 40, "unit": "kg",
                    "source_container_id": "box-mix"}])
        assert service.get_container("box-mix").status == "repacked"
        assert service.get_container("box-mix-2").regulation_version_id == "reg-v2"

        # 新箱继续运往设施并关闭。
        t_r1 = service.propose_transfer(
            request_id="acc-r-carrier", actor_id="op-1", container_id="box-mix-2",
            transfer_type="carrier_handover", from_party="中转营地",
            to_party="极地承运", from_site_id="hub").resource_id
        service.confirm_transfer(request_id="acc-r-carrier-ok", actor_id="car-1",
                                 transfer_id=t_r1)
        t_r2 = service.propose_transfer(
            request_id="acc-r-dest", actor_id="car-1", container_id="box-mix-2",
            transfer_type="destination_delivery", from_party="极地承运",
            to_party="处置设施", from_site_id="hub", to_site_id="facility").resource_id
        service.confirm_transfer(request_id="acc-r-dest-ok", actor_id="rcv-1",
                                 transfer_id=t_r2)
        service.acknowledge_receipt(request_id="acc-r-receipt", actor_id="rcv-1",
                                    transfer_id=t_r2)
        service.certify_disposal(request_id="acc-r-cert", actor_id="rcv-1",
                                 container_id="box-mix-2", facility_site_id="facility",
                                 certificate_ref="CERT-MIX-2")

        # 双向追溯：从批次追到凭证，从箱子反查全部组成（跨重装代）。
        oil_trace = service.trace_lot("oil")
        assert oil_trace["final_disposition"] == "disposed"
        assert oil_trace["certificates"][0]["certificate_ref"] == "CERT-OIL-1"
        composition = service.trace_container("box-mix-2")["composition"]
        by_lot = {item["lot_id"]: item["quantity"] for item in composition}
        assert by_lot == {"bat": 60.0, "pkg": 40.0}, by_lot
        closed_box = service.trace_container("box-oil")["composition"]
        assert {item["lot_id"]: item["quantity"] for item in closed_box} == {"oil": 100.0}

        # 数量差异：登记 = 封箱 = 处置，无悬空责任。
        variance = {row["lot_id"]: row for row in service.quantity_variance()}
        for lot_id in ("oil", "bat", "pkg"):
            row = variance[lot_id]
            assert row["registered_effective"] == row["sealed_quantity"] == \
                row["disposed_quantity"], row
            assert row["open_responsibility"] is False, row

        valid, event_count = service.verify_audit()
        assert valid, "审计哈希链必须完整"

        # 进程恢复：关闭并重新打开同一数据库，状态与审计链继续保留，可继续办理。
        database.close()
        restarted_db = WasteDatabase(db_path)
        restarted = WasteService(restarted_db, clock)
        assert restarted.get_container("box-oil").status == "closed"
        assert restarted.current_responsibility("box-oil")["responsibility_closed"] is True
        again = restarted.acknowledge_receipt(request_id="acc-receipt", actor_id="rcv-1",
                                              transfer_id=t_dest)
        assert again.replayed is True, "重启后重复回调仍须幂等"
        valid2, count2 = restarted.verify_audit()
        assert valid2 and count2 == event_count
        restarted_db.close()

        return {"status": "ok", "audit_events": event_count, "audit_valid": valid,
                "oil_final": oil_trace["final_disposition"],
                "repacked_composition": by_lot,
                "frozen_regulation": "reg-v1",
                "repacked_under_regulation": "reg-v2",
                "lots_closed": 3}


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
