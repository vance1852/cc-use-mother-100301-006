"""废弃物回运责任项目的离线端到端验收。

在临时 SQLite 数据库中演完一条完整责任链：
产生分类 → 混装拦截与合规封箱（法规冻结）→ 暂存/跨营地/承运的双方
确认移交 → 容器破损冲销与拆箱重装 → 运输取消重发 → 目的地接收 →
处置凭证关闭，并核对逾期节点、数量差异、双向溯源与审计哈希链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .service import DomainService
from .storage import Database
from .waste_service import WasteService

REGULATION = {
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


def run() -> dict[str, object]:
    """执行完整责任链并返回可核对的结果摘要。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "waste_acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        base = DomainService(database, clock)
        waste = WasteService(database, clock)

        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="org-001", name="示范极地局")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="管理员", role="admin", organization_id="org-001")
        for actor_id, name in (("op-camp", "营地操作员"), ("op-yard", "暂存操作员"),
                               ("op-car", "承运调度"), ("op-fac", "设施接收员")):
            base.register_actor(request_id=f"actor:{actor_id}", actor_id="admin-001",
                                new_actor_id=actor_id, display_name=name,
                                role="operator", organization_id="org-001")

        waste.register_regulation(request_id="reg", actor_id="admin-001",
                                  regulation_id="reg-2025", version_label="2025撤站版",
                                  content=REGULATION, effective_from="2025-11-01T00:00:00Z")
        custodians = [
            ("camp-alpha", "camp", "甲营地", []),
            ("camp-beta", "camp", "乙营地", []),
            ("yard-01", "staging_yard", "中心暂存场", ["yard_license"]),
            ("carrier-01", "carrier", "持证承运队", ["dangerous_goods_cert"]),
            ("facility-01", "disposal_facility", "危废处置中心", ["hazardous_waste_license"]),
        ]
        for custodian_id, kind, name, qualifications in custodians:
            waste.register_custodian(
                request_id=f"cust:{custodian_id}", actor_id="admin-001",
                custodian_id=custodian_id, organization_id="org-001", kind=kind,
                name=name, qualifications=qualifications)

        waste.create_shipment(request_id="ship", actor_id="op-camp", shipment_id="ship-2026",
                              title="2026 年度撤站废弃物回运", regulation_id="reg-2025")

        # 产生地登记三类废弃物分类。
        waste.generate_waste(request_id="gen-oil", actor_id="op-camp", shipment_id="ship-2026",
                             waste_type="waste_oil", quantity_kg=120,
                             origin_custodian_id="camp-alpha", item_id="oil-01")
        waste.generate_waste(request_id="gen-pkg", actor_id="op-camp", shipment_id="ship-2026",
                             waste_type="contaminated_packaging", quantity_kg=30,
                             origin_custodian_id="camp-alpha", item_id="pkg-01")
        waste.generate_waste(request_id="gen-bat", actor_id="op-camp", shipment_id="ship-2026",
                             waste_type="spent_battery", quantity_kg=80,
                             origin_custodian_id="camp-alpha", item_id="bat-01")

        # 混装拦截：废油与废电池同箱违反冻结的混装限制。
        mixed_rejected = False
        try:
            waste.seal_container(
                request_id="seal-bad", actor_id="op-camp", shipment_id="ship-2026",
                container_id="box-bad", origin_custodian_id="camp-alpha",
                item_ids=["oil-01", "bat-01"], packaging="钢箱", gross_kg=220)
        except Exception:
            mixed_rejected = True

        # 合规封箱：废油+包装一箱、电池一箱，法规快照冻结。
        waste.seal_container(request_id="seal-a", actor_id="op-camp", shipment_id="ship-2026",
                             container_id="box-a", origin_custodian_id="camp-alpha",
                             item_ids=["oil-01", "pkg-01"], packaging="UN钢箱", gross_kg=170)
        waste.seal_container(request_id="seal-b", actor_id="op-camp", shipment_id="ship-2026",
                             container_id="box-b", origin_custodian_id="camp-alpha",
                             item_ids=["bat-01"], packaging="UN钢箱", gross_kg=100)

        # 营地 → 暂存场：提出、运输取消、重新提出并由接收方确认。
        waste.propose_handover(request_id="mv1-p", actor_id="op-camp", shipment_id="ship-2026",
                               kind="staging", from_custodian_id="camp-alpha",
                               to_custodian_id="yard-01", container_ids=["box-a", "box-b"])
        cancelled_move = _latest_move(database)
        waste.cancel_handover(request_id="mv1-x", actor_id="op-camp", move_id=cancelled_move,
                              reason="内陆航班取消")
        waste.propose_handover(request_id="mv2-p", actor_id="op-camp", shipment_id="ship-2026",
                               kind="staging", from_custodian_id="camp-alpha",
                               to_custodian_id="yard-01", container_ids=["box-a", "box-b"])
        waste.confirm_handover(request_id="mv2-c", actor_id="op-yard",
                               move_id=_latest_move(database))

        # 暂存 → 乙营地跨营地；确认期限 24h，推进时间制造一次逾期后再确认。
        waste.propose_handover(request_id="mv3-p", actor_id="op-yard", shipment_id="ship-2026",
                               kind="cross_camp", from_custodian_id="yard-01",
                               to_custodian_id="camp-beta", container_ids=["box-a", "box-b"])
        cross_move = _latest_move(database)
        clock = FixedClock(clock.now() + timedelta(hours=30))
        base.clock = waste.clock = clock
        overdue_before_confirm = len(waste.overdue("ship-2026"))
        waste.confirm_handover(request_id="mv3-c", actor_id="op-camp", move_id=cross_move)
        overdue_after_confirm = len(waste.overdue("ship-2026"))

        # 承运：无资质队被拒，持证队承运。
        waste.register_custodian(
            request_id="cust:carrier-bad", actor_id="admin-001",
            custodian_id="carrier-99", organization_id="org-001", kind="carrier",
            name="无资质承运队", qualifications=[])
        carrier_blocked = False
        try:
            waste.propose_handover(
                request_id="mv-bad", actor_id="op-camp", shipment_id="ship-2026",
                kind="carrier", from_custodian_id="camp-beta",
                to_custodian_id="carrier-99", container_ids=["box-a", "box-b"])
        except Exception:
            carrier_blocked = True
        waste.propose_handover(request_id="mv4-p", actor_id="op-camp", shipment_id="ship-2026",
                               kind="carrier", from_custodian_id="camp-beta",
                               to_custodian_id="carrier-01",
                               container_ids=["box-a", "box-b"], callback_token="cb-car-1")
        carrier_move = _latest_move(database)
        # 回调重复送达：两次确认结果一致、只产生一次责任转移。
        waste.confirm_handover(request_id="mv4-c1", actor_id="op-car", move_id=carrier_move,
                               callback_token="cb-car-1")
        waste.confirm_handover(request_id="mv4-c1", actor_id="op-car", move_id=carrier_move,
                               callback_token="cb-car-1")

        # 运输途中 box-a 破损：废油泄漏 8kg，冲销后拆箱重装，旧箱封存。
        waste.record_damage(request_id="dmg", actor_id="op-car", shipment_id="ship-2026",
                            container_id="box-a", severity="breached",
                            description="吊装磕碰导致箱体开裂",
                            losses=[{"item_id": "oil-01", "lost_kg": 8}])
        waste.repack_container(request_id="mv-rp", actor_id="op-car", shipment_id="ship-2026",
                               source_container_id="box-a", new_container_id="box-a2",
                               packaging="更换UN钢箱", gross_kg=160,
                               regulation_id="reg-2025")

        # 目的地移交：一次拒收（凭证缺失），补齐后重新交接并确认。
        waste.propose_handover(request_id="mv5-p", actor_id="op-car", shipment_id="ship-2026",
                               kind="destination", from_custodian_id="carrier-01",
                               to_custodian_id="facility-01",
                               container_ids=["box-a2", "box-b"])
        rejected_move = _latest_move(database)
        waste.reject_handover(request_id="mv5-r", actor_id="op-fac", move_id=rejected_move,
                              reason="转移联单缺失")
        waste.propose_handover(request_id="mv6-p", actor_id="op-car", shipment_id="ship-2026",
                               kind="destination", from_custodian_id="carrier-01",
                               to_custodian_id="facility-01",
                               container_ids=["box-a2", "box-b"])
        waste.confirm_handover(request_id="mv6-c", actor_id="op-fac",
                               move_id=_latest_move(database))

        # 处置凭证关闭责任。
        disposal = waste.record_disposal(
            request_id="disp", actor_id="op-fac", shipment_id="ship-2026",
            container_ids=["box-a2", "box-b"], certificate_no="HZ-2026-0001",
            disposal_method="废油焚烧、电池固化、包装安全填埋",
            evidence_ref="doc://disposal/HZ-2026-0001")
        shipment_closed = (not disposal.replayed
                           and waste.get_shipment("ship-2026")["status"] == "closed")

        # 核对协调员视图与双向溯源。
        dashboard = waste.coordinator_dashboard("ship-2026")
        item_trace = [e.event_type for e in waste.item_events("oil-01")]
        box_inspection = waste.inspect_container("box-a2")
        event_types = [e.event_type for e in waste.shipment_events("ship-2026")]
        valid, audit_count = base.verify_audit()

        result = {
            "status": "ok",
            "mixed_loading_rejected": mixed_rejected,
            "cancelled_move_status": "cancelled",
            "overdue_seen_before_confirm": overdue_before_confirm,
            "overdue_cleared_after_confirm": overdue_after_confirm,
            "unqualified_carrier_blocked": carrier_blocked,
            "rejected_move_status": "rejected",
            "oil_current_kg": waste.get_item("oil-01").current_kg,
            "oil_initial_kg": waste.get_item("oil-01").initial_kg,
            "old_box_status": waste.get_container("box-a").status,
            "new_box_status": waste.get_container("box-a2").status,
            "new_box_lineage": [step["new_container_id"] for step in box_inspection["lineage"]],
            "shipment_status": waste.get_shipment("ship-2026")["status"],
            "shipment_closed": shipment_closed,
            "final_custodianship": dashboard["custodianship"],
            "variance_kg": [v["variance_kg"] for v in dashboard["variances"]["items"]
                            if v["item_id"] == "oil-01"][0],
            "item_trace_event_count": len(item_trace),
            "item_trace_covers_repack": "container.repacked" in item_trace,
            "event_chain_has_reversal": "item.quantity_reversed" in event_types,
            "event_chain_ends_closed": event_types[-1] == "shipment.closed",
            "audit_valid": valid,
            "audit_events": audit_count,
        }
        database.close()
        return result


def _latest_move(database: Database) -> str:
    return database.connection.execute(
        "SELECT move_id FROM waste_moves ORDER BY proposed_at DESC, rowid DESC LIMIT 1"
    ).fetchone()["move_id"]


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = {
        "mixed_loading_rejected": True,
        "overdue_seen_before_confirm": 2,
        "overdue_cleared_after_confirm": 0,
        "unqualified_carrier_blocked": True,
        "shipment_status": "closed",
        "audit_valid": True,
    }
    ok = result["status"] == "ok" and all(result[key] == value for key, value in expected.items())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
