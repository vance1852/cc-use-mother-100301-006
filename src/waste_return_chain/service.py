"""废弃物回运责任服务。

在基础服务的权限、幂等、事务与哈希审计边界上，实现从产生地点到处置凭证的
责任链。核心原则：

- 封箱时冻结法规版本（混装限制、期限、责任人要求），旧事实不被新版本改写；
- 登记、封箱、暂存、移交、重装、接收、处置全部是只增事件，破损、取消、
  数量修正、接收拒绝通过新的冲销/重新分配事件处理，已完成责任不可回滚；
- 血缘边（wr_edges）记录批次数量在箱与箱、箱与终点之间的定向流动，
  支持从来源追到最终去向，也支持从箱子递归反查全部组成。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any

from polar_station_foundation.audit import append_event, canonical_json, digest
from polar_station_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from polar_station_foundation.models import WriteReceipt
from polar_station_foundation.service import DomainService

from .models import (
    CustodyTransfer,
    DisposalCertificate,
    LineageEdge,
    LotCorrection,
    ManifestEntry,
    RegulationVersion,
    StorageStay,
    WasteContainer,
    WasteLot,
)
from .rules import (
    UNITS,
    WASTE_CATEGORIES,
    assert_mixing_allowed,
    snapshot_regulation,
    validate_regulation_payload,
)


# 容器状态机。
STATUS_SEALED = "sealed"
STATUS_IN_STORAGE = "in_storage"
STATUS_IN_TRANSIT = "in_transit"
STATUS_AT_CAMP = "at_camp"
STATUS_AT_FACILITY = "at_facility"
STATUS_DAMAGED = "damaged"
STATUS_REJECTED_RETURN = "rejected_return"
STATUS_REPACKED = "repacked"
STATUS_CLOSED = "closed"

PHYSICAL_TRANSFERS = frozenset({"cross_camp", "carrier_handover", "destination_delivery", "return_transfer"})


class WasteService(DomainService):
    """协调废弃物责任链的权限、冻结快照、事件与查询。"""

    # ---- 法规版本 -------------------------------------------------------

    def publish_regulation(self, *, request_id: str, actor_id: str, version_id: str,
                           payload: dict[str, Any], effective_from: str | None = None):
        """发布一版不可变法规；旧容器继续引用旧版本。"""

        body = {"actor_id": actor_id, "version_id": version_id,
                "payload": payload, "effective_from": effective_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="publish_regulation", payload=body)
            if replay:
                return replay
            version_id = self._identifier(version_id, "version_id")
            validate_regulation_payload(payload)
            snapshot, payload_hash = snapshot_regulation(payload)
            effective_from = effective_from or self._now()

            def create():
                if connection.execute("SELECT 1 FROM wr_regulation_versions WHERE version_id=?",
                                      (version_id,)).fetchone():
                    raise ConflictError("法规版本编号已经存在")
                connection.execute(
                    "INSERT INTO wr_regulation_versions(version_id,payload_json,payload_hash,"
                    "effective_from,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (version_id, canonical_json(snapshot), payload_hash,
                     effective_from, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="waste_regulation.published",
                             resource_type="regulation_version", resource_id=version_id,
                             detail={"version_id": version_id, "payload_hash": payload_hash,
                                     "effective_from": effective_from},
                             occurred_at=self._now())
                return "regulation_version", version_id, {"version_id": version_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_regulation", payload=body, create=create)

    def _resolve_regulation(self, connection, version_id: str | None):
        if version_id:
            row = connection.execute(
                "SELECT * FROM wr_regulation_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("法规版本不存在")
            return row
        row = connection.execute(
            "SELECT * FROM wr_regulation_versions WHERE effective_from<=? "
            "ORDER BY effective_from DESC, version_id DESC LIMIT 1",
            (self._now(),),
        ).fetchone()
        if row is None:
            raise ValidationError("尚未发布任何生效法规版本，封箱无法冻结规则")
        return row

    def get_regulation_version(self, version_id: str) -> RegulationVersion:
        row = self.database.connection.execute(
            "SELECT * FROM wr_regulation_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("法规版本不存在")
        return RegulationVersion(row["version_id"], json.loads(row["payload_json"]),
                                 row["payload_hash"], row["effective_from"],
                                 row["created_by"], row["created_at"])

    # ---- 产生地点：批次登记与数量冲销 -----------------------------------

    def register_lot(self, *, request_id: str, actor_id: str, site_id: str, lot_id: str,
                     waste_category: str, hazard_class: str, quantity: float, unit: str,
                     packaging: str, permit_id: str, generated_at: str | None = None):
        """在产生地点登记一批已分类、已封装的废弃物及其许可。"""

        body = {"actor_id": actor_id, "site_id": site_id, "lot_id": lot_id,
                "waste_category": waste_category, "hazard_class": hazard_class,
                "quantity": quantity, "unit": unit, "packaging": packaging,
                "permit_id": permit_id, "generated_at": generated_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="register_lot", payload=body)
            if replay:
                return replay
            site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("产生地点不存在")
            if actor.organization_id != site["organization_id"] and actor.role != "admin":
                raise PermissionDenied("不能在其他组织的地点登记废弃物")
            lot_id = self._identifier(lot_id, "lot_id")
            if waste_category not in WASTE_CATEGORIES:
                raise ValidationError("waste_category 不在受监管范围内")
            if unit not in UNITS:
                raise ValidationError("unit 必须是 kg 或 L")
            quantity = self._positive(quantity, "quantity")
            hazard_class = self._text(hazard_class, "hazard_class", 80)
            packaging = self._text(packaging, "packaging", 200)
            permit_id = self._text(permit_id, "permit_id", 120)
            generated_at = generated_at or self._now()

            def create():
                if connection.execute("SELECT 1 FROM wr_lots WHERE lot_id=?", (lot_id,)).fetchone():
                    raise ConflictError("废弃物批次编号已经存在")
                connection.execute(
                    "INSERT INTO wr_lots(lot_id,site_id,waste_category,hazard_class,quantity,unit,"
                    "packaging,permit_id,generated_at,generated_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (lot_id, site_id, waste_category, hazard_class, quantity, unit, packaging,
                     permit_id, generated_at, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="waste_lot.registered",
                             resource_type="waste_lot", resource_id=lot_id,
                             detail={"site_id": site_id, "waste_category": waste_category,
                                     "quantity": quantity, "unit": unit, "permit_id": permit_id},
                             occurred_at=self._now())
                return "waste_lot", lot_id, {"lot_id": lot_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_lot", payload=body, create=create)

    def correct_lot_quantity(self, *, request_id: str, actor_id: str, lot_id: str,
                             delta: float, reason: str):
        """以追加的冲销事件修正登记数量，绝不改写原始登记。"""

        body = {"actor_id": actor_id, "lot_id": lot_id, "delta": delta, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="correct_lot_quantity", payload=body)
            if replay:
                return replay
            lot = connection.execute("SELECT * FROM wr_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if lot is None:
                raise NotFoundError("废弃物批次不存在")
            if not isinstance(delta, (int, float)) or delta == 0:
                raise ValidationError("delta 必须是非零数值")
            reason = self._text(reason, "reason", 500)
            effective = lot["quantity"] + self._correction_sum(connection, lot_id) + float(delta)
            if effective < 0:
                raise ValidationError("修正后的登记数量不能为负")

            def create():
                event_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO wr_lot_corrections(event_id,lot_id,delta,reason,corrected_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (event_id, lot_id, float(delta), reason, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="waste_lot.corrected",
                             resource_type="waste_lot", resource_id=lot_id,
                             detail={"event_id": event_id, "delta": float(delta),
                                     "reason": reason, "effective_quantity": effective},
                             occurred_at=self._now())
                return "lot_correction", event_id, {"event_id": event_id, "lot_id": lot_id,
                                                    "effective_quantity": effective}

            return self._idempotent(connection, request_id=request_id,
                                    action="correct_lot_quantity", payload=body, create=create)

    # ---- 封箱：冻结法规快照与清单 ---------------------------------------

    def seal_container(self, *, request_id: str, actor_id: str, container_id: str,
                       entries: list[dict[str, Any]], gross_weight: float | None = None,
                       regulation_version_id: str | None = None):
        """在产生地点封箱，冻结适用法规版本与不可变清单。"""

        body = {"actor_id": actor_id, "container_id": container_id, "entries": entries,
                "gross_weight": gross_weight, "regulation_version_id": regulation_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="seal_container", payload=body)
            if replay:
                return replay
            container_id = self._identifier(container_id, "container_id")
            entries = self._validated_entries(connection, entries)
            self._assert_within_registered(connection, entries)
            regulation = self._resolve_regulation(connection, regulation_version_id)
            snapshot = json.loads(regulation["payload_json"])
            categories = {self._lot_row(connection, item["lot_id"])["waste_category"] for item in entries}
            assert_mixing_allowed(categories, snapshot["mixing_rules"])
            origin_site_id = self._lot_row(connection, entries[0]["lot_id"])["site_id"]
            for item in entries:
                lot = self._lot_row(connection, item["lot_id"])
                if lot["site_id"] != origin_site_id:
                    raise ValidationError("同一箱只能封装同一产生地点的批次")
            if gross_weight is not None:
                gross_weight = self._positive(gross_weight, "gross_weight")

            def create():
                if connection.execute("SELECT 1 FROM wr_containers WHERE container_id=?",
                                      (container_id,)).fetchone():
                    raise ConflictError("回运箱编号已经存在")
                sealed_at = self._now()
                event_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO wr_containers(container_id,origin_site_id,status,regulation_version_id,"
                    "regulation_snapshot_json,regulation_snapshot_hash,gross_weight,sealed_by,sealed_at,"
                    "predecessor_container_ids_json,closed_at,closed_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (container_id, origin_site_id, STATUS_SEALED, regulation["version_id"],
                     regulation["payload_json"], regulation["payload_hash"], gross_weight,
                     actor_id, sealed_at, canonical_json([]), None, None),
                )
                for item in entries:
                    connection.execute(
                        "INSERT INTO wr_manifest(container_id,lot_id,quantity,unit) VALUES(?,?,?,?)",
                        (container_id, item["lot_id"], item["quantity"], item["unit"]),
                    )
                    connection.execute(
                        "INSERT INTO wr_edges(event_id,lot_id,from_container_id,to_container_id,"
                        "quantity,reason,source_event_id,acknowledged_at,acknowledged_by) "
                        "VALUES(?,?,NULL,?,?,?,?,NULL,NULL)",
                        (event_id, item["lot_id"], container_id, item["quantity"], "sealed", None),
                    )
                append_event(connection, actor_id=actor_id, action="waste_container.sealed",
                             resource_type="waste_container", resource_id=container_id,
                             detail={"event_id": event_id, "origin_site_id": origin_site_id,
                                     "regulation_version_id": regulation["version_id"],
                                     "regulation_snapshot_hash": regulation["payload_hash"],
                                     "entries": entries, "gross_weight": gross_weight},
                             occurred_at=sealed_at)
                return "waste_container", container_id, {"container_id": container_id,
                                                         "status": STATUS_SEALED, "event_id": event_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="seal_container", payload=body, create=create)

    # ---- 暂存 -----------------------------------------------------------

    def check_in_storage(self, *, request_id: str, actor_id: str, container_id: str,
                         site_id: str, keeper: str, handed_by: str):
        """入库暂存，按封箱快照中的 storage_due_hours 冻结暂存期限。"""

        body = {"actor_id": actor_id, "container_id": container_id, "site_id": site_id,
                "keeper": keeper, "handed_by": handed_by}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="check_in_storage", payload=body)
            if replay:
                return replay
            container = self._container_row(connection, container_id)
            self._assume_site(connection, site_id, actor)
            if container["status"] not in (STATUS_SEALED, STATUS_AT_CAMP, STATUS_DAMAGED):
                raise ConflictError(f"容器当前状态 {container['status']} 不能入库暂存")
            self._assert_no_open_stay(connection, container_id)
            keeper = self._text(keeper, "keeper", 120)
            handed_by = self._text(handed_by, "handed_by", 120)
            snapshot = json.loads(container["regulation_snapshot_json"])
            due_at = (self.clock.now() + timedelta(hours=snapshot["deadlines"]["storage_due_hours"]))
            due = due_at.isoformat().replace("+00:00", "Z")

            def create():
                stay_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO wr_storage_stays(stay_id,container_id,site_id,keeper,handed_by,"
                    "checked_in_at,due_at,checked_out_at,released_by) VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                    (stay_id, container_id, site_id, keeper, handed_by, now, due),
                )
                self._set_status(connection, container_id, STATUS_IN_STORAGE)
                append_event(connection, actor_id=actor_id, action="waste_storage.checked_in",
                             resource_type="storage_stay", resource_id=stay_id,
                             detail={"stay_id": stay_id, "container_id": container_id,
                                     "site_id": site_id, "keeper": keeper,
                                     "handed_by": handed_by, "due_at": due},
                             occurred_at=now)
                return "storage_stay", stay_id, {"stay_id": stay_id, "due_at": due}

            return self._idempotent(connection, request_id=request_id,
                                    action="check_in_storage", payload=body, create=create)

    def check_out_storage(self, *, request_id: str, actor_id: str, container_id: str,
                          released_by: str):
        """出库释放，闭合本次暂存责任。"""

        body = {"actor_id": actor_id, "container_id": container_id, "released_by": released_by}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="check_out_storage", payload=body)
            if replay:
                return replay
            container = self._container_row(connection, container_id)
            stay = connection.execute(
                "SELECT * FROM wr_storage_stays WHERE container_id=? AND checked_out_at IS NULL",
                (container_id,),
            ).fetchone()
            if stay is None:
                raise NotFoundError("容器没有进行中的暂存记录")
            released_by = self._text(released_by, "released_by", 120)

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE wr_storage_stays SET checked_out_at=?, released_by=? WHERE stay_id=?",
                    (now, released_by, stay["stay_id"]),
                )
                if container["status"] == STATUS_DAMAGED:
                    restored = STATUS_DAMAGED
                else:
                    last = connection.execute(
                        "SELECT transfer_type FROM wr_custody_transfers "
                        "WHERE container_id=? AND status='confirmed' ORDER BY confirmed_at DESC "
                        "LIMIT 1", (container_id,)).fetchone()
                    restored = {
                        "cross_camp": STATUS_AT_CAMP,
                        "return_transfer": STATUS_AT_CAMP,
                        "destination_delivery": STATUS_AT_FACILITY,
                    }.get(last["transfer_type"] if last else None, STATUS_SEALED)
                self._set_status(connection, container_id, restored)
                append_event(connection, actor_id=actor_id, action="waste_storage.checked_out",
                             resource_type="storage_stay", resource_id=stay["stay_id"],
                             detail={"stay_id": stay["stay_id"], "container_id": container_id,
                                     "released_by": released_by},
                             occurred_at=now)
                return "storage_stay", stay["stay_id"], {"stay_id": stay["stay_id"], "checked_out": True}

            return self._idempotent(connection, request_id=request_id,
                                    action="check_out_storage", payload=body, create=create)

    # ---- 跨营地移交 / 承运交接 / 目的地接收（双方确认） -------------------

    def propose_transfer(self, *, request_id: str, actor_id: str, container_id: str,
                         transfer_type: str, from_party: str, to_party: str,
                         from_site_id: str, to_site_id: str | None = None,
                         reversal_of: str | None = None, detail: dict[str, Any] | None = None):
        """发起一次移交并冻结确认期限；对方确认前责任不转移。"""

        body = {"actor_id": actor_id, "container_id": container_id, "transfer_type": transfer_type,
                "from_party": from_party, "to_party": to_party, "from_site_id": from_site_id,
                "to_site_id": to_site_id, "reversal_of": reversal_of, "detail": detail or {}}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            # 回运可由接收方（reviewer）在拒收后发起；无论谁发起都仍须对手方确认。
            if transfer_type == "return_transfer":
                self._require(actor, "admin", "operator", "reviewer")
            else:
                self._require(actor, "admin", "operator")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="propose_transfer", payload=body)
            if replay:
                return replay
            container = self._container_row(connection, container_id)
            if transfer_type not in PHYSICAL_TRANSFERS:
                raise ValidationError("transfer_type 不被支持")
            if container["status"] in (STATUS_CLOSED, STATUS_REPACKED):
                raise ConflictError("该容器已关闭或已完全重装，不能移交")
            if container["status"] == STATUS_IN_STORAGE:
                raise ConflictError("容器在暂存中，须先出库再移交")
            if transfer_type == "destination_delivery" and container["status"] != STATUS_IN_TRANSIT:
                raise ConflictError("只有在途容器可以向目的地交付")
            if transfer_type == "return_transfer" and \
                    container["status"] not in (STATUS_IN_TRANSIT, STATUS_REJECTED_RETURN):
                raise ConflictError("冲销回运只适用于承运取消（在途）或目的地接收拒绝")
            if transfer_type in ("cross_camp", "carrier_handover") and \
                    container["status"] not in (STATUS_SEALED, STATUS_AT_CAMP):
                raise ConflictError(f"容器当前状态 {container['status']} 不能发起{transfer_type}（破损箱须先重装）")
            self._assume_site(connection, from_site_id, actor)
            if to_site_id is not None:
                self._assume_site(connection, to_site_id, actor)
            from_party = self._text(from_party, "from_party", 120)
            to_party = self._text(to_party, "to_party", 120)
            if from_party == to_party:
                raise ValidationError("移交双方不能是同一责任方")
            snapshot = json.loads(container["regulation_snapshot_json"])
            due = (self.clock.now()
                   + timedelta(hours=snapshot["deadlines"]["transfer_confirm_due_hours"]))
            confirm_due = due.isoformat().replace("+00:00", "Z")
            if reversal_of:
                original = connection.execute(
                    "SELECT 1 FROM wr_custody_transfers WHERE transfer_id=?", (reversal_of,)
                ).fetchone()
                if original is None:
                    raise NotFoundError("被冲销的原移交不存在")

            def create():
                transfer_id = uuid.uuid4().hex
                now = self._now()
                connection.execute(
                    "INSERT INTO wr_custody_transfers(transfer_id,transfer_type,custody_kind,"
                    "container_id,from_party,to_party,from_site_id,to_site_id,status,reversal_of,"
                    "proposed_by,proposed_at,confirm_due_at,confirmed_by,confirmed_at,detail_json) "
                    "VALUES(?,?,'physical',?,?,?,?,?, 'proposed', ?,?,?,?,NULL,NULL,?)",
                    (transfer_id, transfer_type, container_id, from_party, to_party,
                     from_site_id, to_site_id, reversal_of, actor_id, now, confirm_due,
                     canonical_json(detail or {})),
                )
                append_event(connection, actor_id=actor_id, action="waste_transfer.proposed",
                             resource_type="custody_transfer", resource_id=transfer_id,
                             detail={"transfer_id": transfer_id, "container_id": container_id,
                                     "transfer_type": transfer_type, "from_party": from_party,
                                     "to_party": to_party, "from_site_id": from_site_id,
                                     "to_site_id": to_site_id, "reversal_of": reversal_of,
                                     "confirm_due_at": confirm_due},
                             occurred_at=now)
                return "custody_transfer", transfer_id, {"transfer_id": transfer_id,
                                                         "status": "proposed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="propose_transfer", payload=body, create=create)

    def confirm_transfer(self, *, request_id: str, actor_id: str, transfer_id: str):
        """接收方确认移交；发起人与确认人必须不同，责任此刻才转移。"""

        body = {"actor_id": actor_id, "transfer_id": transfer_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="confirm_transfer", payload=body)
            if replay:
                return replay
            transfer = self._transfer_row(connection, transfer_id)
            if transfer["status"] != "proposed":
                raise ConflictError(f"移交当前状态 {transfer['status']}，不能确认")
            if transfer["proposed_by"] == actor_id and actor.role != "admin":
                raise PermissionDenied("发起人不能同时作为接收确认人，必须双方确认")

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE wr_custody_transfers SET status='confirmed', confirmed_by=?, "
                    "confirmed_at=? WHERE transfer_id=?",
                    (actor_id, now, transfer_id),
                )
                new_status = {
                    "cross_camp": STATUS_AT_CAMP,
                    "carrier_handover": STATUS_IN_TRANSIT,
                    "destination_delivery": STATUS_AT_FACILITY,
                    "return_transfer": STATUS_AT_CAMP,
                }[transfer["transfer_type"]]
                self._set_status(connection, transfer["container_id"], new_status)
                append_event(connection, actor_id=actor_id, action="waste_transfer.confirmed",
                             resource_type="custody_transfer", resource_id=transfer_id,
                             detail={"transfer_id": transfer_id,
                                     "container_id": transfer["container_id"],
                                     "transfer_type": transfer["transfer_type"],
                                     "confirmed_by": actor_id, "reversal_of": transfer["reversal_of"]},
                             occurred_at=now)
                return "custody_transfer", transfer_id, {"transfer_id": transfer_id,
                                                         "status": "confirmed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="confirm_transfer", payload=body, create=create)

    def cancel_transfer(self, *, request_id: str, actor_id: str, transfer_id: str, reason: str):
        """取消尚未确认的移交；已确认责任不可取消，只能发起冲销回运。"""

        body = {"actor_id": actor_id, "transfer_id": transfer_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="cancel_transfer", payload=body)
            if replay:
                return replay
            transfer = self._transfer_row(connection, transfer_id)
            if transfer["status"] != "proposed":
                raise ConflictError("只有待确认的移交可以取消；已完成责任请用冲销回运事件")
            reason = self._text(reason, "reason", 500)

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE wr_custody_transfers SET status='cancelled' WHERE transfer_id=?",
                    (transfer_id,),
                )
                append_event(connection, actor_id=actor_id, action="waste_transfer.cancelled",
                             resource_type="custody_transfer", resource_id=transfer_id,
                             detail={"transfer_id": transfer_id, "reason": reason,
                                     "container_id": transfer["container_id"]},
                             occurred_at=now)
                return "custody_transfer", transfer_id, {"transfer_id": transfer_id,
                                                         "status": "cancelled"}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_transfer", payload=body, create=create)

    def reject_transfer(self, *, request_id: str, actor_id: str, transfer_id: str, reason: str):
        """接收方在确认前拒绝接收（例如清点不符或容器状态异常）。"""

        body = {"actor_id": actor_id, "transfer_id": transfer_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="reject_transfer", payload=body)
            if replay:
                return replay
            transfer = self._transfer_row(connection, transfer_id)
            if transfer["status"] != "proposed":
                raise ConflictError("只有待确认的移交可以拒绝")
            if transfer["proposed_by"] == actor_id and actor.role != "admin":
                raise PermissionDenied("发起人不能拒绝自己发起的移交")
            reason = self._text(reason, "reason", 500)

            def create():
                now = self._now()
                connection.execute(
                    "UPDATE wr_custody_transfers SET status='rejected' WHERE transfer_id=?",
                    (transfer_id,),
                )
                append_event(connection, actor_id=actor_id, action="waste_transfer.rejected",
                             resource_type="custody_transfer", resource_id=transfer_id,
                             detail={"transfer_id": transfer_id, "reason": reason,
                                     "container_id": transfer["container_id"],
                                     "transfer_type": transfer["transfer_type"]},
                             occurred_at=now)
                return "custody_transfer", transfer_id, {"transfer_id": transfer_id,
                                                         "status": "rejected"}

            return self._idempotent(connection, request_id=request_id,
                                    action="reject_transfer", payload=body, create=create)

    def acknowledge_receipt(self, *, request_id: str, actor_id: str, transfer_id: str,
                            items: list[dict[str, Any]] | None = None, note: str | None = None):
        """目的地/营地接收方逐项签收；数量必须与在箱血缘余额一致。"""

        body = {"actor_id": actor_id, "transfer_id": transfer_id, "items": items, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="acknowledge_receipt", payload=body)
            if replay:
                return replay
            transfer = self._transfer_row(connection, transfer_id)

            def create():
                if transfer["status"] != "confirmed":
                    raise ConflictError("只能对已确认的移交做接收签收")
                if connection.execute("SELECT 1 FROM wr_receipts WHERE transfer_id=?",
                                      (transfer_id,)).fetchone():
                    raise ConflictError("该移交已经签收，重复回调应复用原 request_id")
                balances = self._container_balances(connection, transfer["container_id"])
                expected = [{"lot_id": lot_id, "quantity": qty,
                             "unit": self._lot_row(connection, lot_id)["unit"]}
                            for lot_id, qty in sorted(balances.items())]
                received = expected if items is None else \
                    self._validated_entries(connection, items)
                expected_map = {row["lot_id"]: row["quantity"] for row in expected}
                if {i["lot_id"]: i["quantity"] for i in received} != expected_map:
                    raise ConflictError("签收数量与在箱组成不符；数量异议须发起冲销修正或拒绝接收")
                now = self._now()
                receipt_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO wr_receipts(receipt_id,transfer_id,container_id,items_json,"
                    "acknowledged_by,acknowledged_at) VALUES(?,?,?,?,?,?)",
                    (receipt_id, transfer_id, transfer["container_id"],
                     canonical_json(received), actor_id, now),
                )
                connection.execute(
                    "UPDATE wr_edges SET acknowledged_at=?, acknowledged_by=? "
                    "WHERE to_container_id=? AND acknowledged_at IS NULL",
                    (now, actor_id, transfer["container_id"]),
                )
                append_event(connection, actor_id=actor_id, action="waste_receipt.acknowledged",
                             resource_type="waste_receipt", resource_id=receipt_id,
                             detail={"receipt_id": receipt_id, "transfer_id": transfer_id,
                                     "container_id": transfer["container_id"],
                                     "items": received, "note": note},
                             occurred_at=now)
                return "waste_receipt", receipt_id, {"receipt_id": receipt_id,
                                                     "transfer_id": transfer_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="acknowledge_receipt", payload=body, create=create)

    def reject_received_container(self, *, request_id: str, actor_id: str, container_id: str,
                                  reason: str):
        """目的地接收后拒收：容器转入待退回状态，须经冲销回运移交离开。"""

        body = {"actor_id": actor_id, "container_id": container_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="reject_received_container", payload=body)
            if replay:
                return replay
            container = self._container_row(connection, container_id)
            if container["status"] != STATUS_AT_FACILITY:
                raise ConflictError("只有已到达目的地的容器可以被接收拒绝")
            reason = self._text(reason, "reason", 500)

            def create():
                now = self._now()
                self._set_status(connection, container_id, STATUS_REJECTED_RETURN)
                append_event(connection, actor_id=actor_id, action="waste_container.receive_rejected",
                             resource_type="waste_container", resource_id=container_id,
                             detail={"container_id": container_id, "reason": reason},
                             occurred_at=now)
                return "waste_container", container_id, {"container_id": container_id,
                                                         "status": STATUS_REJECTED_RETURN}

            return self._idempotent(connection, request_id=request_id,
                                    action="reject_received_container", payload=body, create=create)

    # ---- 容器破损与拆箱重装（冲销 + 重新分配） ---------------------------

    def mark_container_damaged(self, *, request_id: str, actor_id: str, container_id: str,
                               description: str):
        """登记容器破损；破损箱不得继续移交/处置，必须重装到新箱。"""

        body = {"actor_id": actor_id, "container_id": container_id, "description": description}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="mark_container_damaged", payload=body)
            if replay:
                return replay
            container = self._container_row(connection, container_id)
            if container["status"] in (STATUS_CLOSED, STATUS_REPACKED):
                raise ConflictError("已关闭容器不能登记破损")
            if container["status"] == STATUS_DAMAGED:
                raise ConflictError("容器已处于破损状态")
            description = self._text(description, "description", 500)

            def create():
                now = self._now()
                self._set_status(connection, container_id, STATUS_DAMAGED)
                append_event(connection, actor_id=actor_id, action="waste_container.damaged",
                             resource_type="waste_container", resource_id=container_id,
                             detail={"container_id": container_id, "description": description},
                             occurred_at=now)
                return "waste_container", container_id, {"container_id": container_id,
                                                         "status": STATUS_DAMAGED}

            return self._idempotent(connection, request_id=request_id,
                                    action="mark_container_damaged", payload=body, create=create)

    def repack_container(self, *, request_id: str, actor_id: str, new_container_id: str,
                         site_id: str, items: list[dict[str, Any]],
                         gross_weight: float | None = None,
                         regulation_version_id: str | None = None):
        """拆箱重装：把一个或多个旧箱中的批次数量重新分配到新箱。

        旧箱不被删除或改写：为每条数量建立 旧箱→新箱 的血缘边，余额清零的
        旧箱转入 repacked；新箱重新封箱并冻结当前适用法规。
        """

        body = {"actor_id": actor_id, "new_container_id": new_container_id, "site_id": site_id,
                "items": items, "gross_weight": gross_weight,
                "regulation_version_id": regulation_version_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="repack_container", payload=body)
            if replay:
                return replay
            self._assume_site(connection, site_id, actor)
            new_container_id = self._identifier(new_container_id, "new_container_id")
            if not isinstance(items, list) or not items:
                raise ValidationError("items 必须是非空数组")
            parsed: list[dict[str, Any]] = []
            claimed: dict[tuple[str, str], float] = {}
            for raw in items:
                lot_id = self._identifier(raw["lot_id"], "lot_id")
                source_id = self._identifier(raw["source_container_id"], "source_container_id")
                qty = self._positive(raw["quantity"], "quantity")
                lot = self._lot_row(connection, lot_id)
                if lot["unit"] != raw.get("unit", lot["unit"]):
                    raise ValidationError("重装数量单位必须与批次登记单位一致")
                source = self._container_row(connection, source_id)
                if source["status"] not in (STATUS_SEALED, STATUS_AT_CAMP, STATUS_AT_FACILITY,
                                            STATUS_DAMAGED, STATUS_REJECTED_RETURN,
                                            STATUS_REPACKED):
                    raise ConflictError(f"来源箱 {source_id} 当前状态不能重装")
                balances = self._container_balances(connection, source_id)
                key = (source_id, lot_id)
                claimed_total = claimed.get(key, 0.0) + qty
                if claimed_total > balances.get(lot_id, 0) + 1e-9:
                    raise ConflictError(
                        f"来源箱 {source_id} 中批次 {lot_id} 本次重装合计 "
                        f"{claimed_total} 超过可重装数量 {balances.get(lot_id, 0)}")
                claimed[key] = claimed_total
                parsed.append({"lot_id": lot_id, "source_container_id": source_id,
                               "quantity": qty, "unit": lot["unit"]})
            regulation = self._resolve_regulation(connection, regulation_version_id)
            snapshot = json.loads(regulation["payload_json"])
            categories = {self._lot_row(connection, item["lot_id"])["waste_category"]
                          for item in parsed}
            assert_mixing_allowed(categories, snapshot["mixing_rules"])
            if gross_weight is not None:
                gross_weight = self._positive(gross_weight, "gross_weight")
            predecessors = sorted({item["source_container_id"] for item in parsed})

            def create():
                if connection.execute("SELECT 1 FROM wr_containers WHERE container_id=?",
                                      (new_container_id,)).fetchone():
                    raise ConflictError("新回运箱编号已经存在")
                now = self._now()
                event_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO wr_containers(container_id,origin_site_id,status,"
                    "regulation_version_id,regulation_snapshot_json,regulation_snapshot_hash,"
                    "gross_weight,sealed_by,sealed_at,predecessor_container_ids_json,closed_at,closed_by) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (new_container_id, site_id, STATUS_SEALED, regulation["version_id"],
                     regulation["payload_json"], regulation["payload_hash"], gross_weight,
                     actor_id, now, canonical_json(predecessors), None, None),
                )
                for item in parsed:
                    source_edge = connection.execute(
                        "SELECT event_id FROM wr_edges WHERE to_container_id=? AND lot_id=? "
                        "ORDER BY edge_id DESC LIMIT 1",
                        (item["source_container_id"], item["lot_id"]),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO wr_edges(event_id,lot_id,from_container_id,to_container_id,"
                        "quantity,reason,source_event_id,acknowledged_at,acknowledged_by) "
                        "VALUES(?,?,?,?,?, 'repack', ?,NULL,NULL)",
                        (event_id, item["lot_id"], item["source_container_id"], new_container_id,
                         item["quantity"], source_edge["event_id"] if source_edge else None),
                    )
                for source_id in predecessors:
                    remaining = self._container_balances(connection, source_id)
                    # _container_balances 在边插入后重算，故清零的箱标记为 repacked。
                    if not remaining:
                        self._set_status(connection, source_id, STATUS_REPACKED)
                append_event(connection, actor_id=actor_id, action="waste_container.repacked",
                             resource_type="waste_container", resource_id=new_container_id,
                             detail={"event_id": event_id, "new_container_id": new_container_id,
                                     "site_id": site_id, "predecessors": predecessors,
                                     "items": parsed,
                                     "regulation_version_id": regulation["version_id"]},
                             occurred_at=now)
                return "waste_container", new_container_id, {"container_id": new_container_id,
                                                             "status": STATUS_SEALED,
                                                             "event_id": event_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="repack_container", payload=body, create=create)

    # ---- 处置凭证：责任关闭 ---------------------------------------------

    def certify_disposal(self, *, request_id: str, actor_id: str, container_id: str,
                         facility_site_id: str, certificate_ref: str,
                         items: list[dict[str, Any]] | None = None):
        """目的地按批次数量开具处置凭证并关闭该箱责任。"""

        body = {"actor_id": actor_id, "container_id": container_id,
                "facility_site_id": facility_site_id, "certificate_ref": certificate_ref,
                "items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "reviewer")
            replay = self._replay_if_seen(connection, request_id=request_id,
                                          action="certify_disposal", payload=body)
            if replay:
                return replay
            container = self._container_row(connection, container_id)
            self._assume_site(connection, facility_site_id, actor)
            certificate_ref = self._text(certificate_ref, "certificate_ref", 160)

            def create():
                # 状态相关校验全部放在 create 内：相同 request_id 的重放由幂等层
                # 直接短路，不会因容器已关闭、余额已清零而失败。
                if container["status"] != STATUS_AT_FACILITY:
                    raise ConflictError("只有目的地完成接收的容器可以关闭处置责任")
                balances = self._container_balances(connection, container_id)
                if not balances:
                    raise ConflictError("容器没有可处置的在箱数量（可能已重装转出）")
                expected = [{"lot_id": lot_id, "quantity": qty,
                             "unit": self._lot_row(connection, lot_id)["unit"]}
                            for lot_id, qty in sorted(balances.items())]
                certified_items = expected if items is None else \
                    self._validated_entries(connection, items)
                if {i["lot_id"]: i["quantity"] for i in certified_items} != \
                        {row["lot_id"]: row["quantity"] for row in expected}:
                    raise ConflictError("凭证数量必须与容器全部在箱组成一致")
                now = self._now()
                certificate_id = uuid.uuid4().hex
                event_id = uuid.uuid4().hex
                for item in certified_items:
                    source_edge = connection.execute(
                        "SELECT event_id FROM wr_edges WHERE to_container_id=? AND lot_id=? "
                        "ORDER BY edge_id DESC LIMIT 1",
                        (container_id, item["lot_id"]),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO wr_edges(event_id,lot_id,from_container_id,to_container_id,"
                        "quantity,reason,source_event_id,acknowledged_at,acknowledged_by) "
                        "VALUES(?,?,?,NULL,?, 'disposal', ?,?,?)",
                        (event_id, item["lot_id"], container_id, item["quantity"],
                         source_edge["event_id"] if source_edge else None, now, actor_id),
                    )
                connection.execute(
                    "INSERT INTO wr_disposal_certificates(certificate_id,container_id,"
                    "facility_site_id,certificate_ref,items_json,certified_by,certified_at,event_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (certificate_id, container_id, facility_site_id, certificate_ref,
                     canonical_json(certified_items), actor_id, now, event_id),
                )
                connection.execute(
                    "UPDATE wr_containers SET status=?, closed_at=?, closed_by=? "
                    "WHERE container_id=?",
                    (STATUS_CLOSED, now, actor_id, container_id),
                )
                append_event(connection, actor_id=actor_id, action="waste_disposal.certified",
                             resource_type="disposal_certificate", resource_id=certificate_id,
                             detail={"certificate_id": certificate_id, "event_id": event_id,
                                     "container_id": container_id,
                                     "facility_site_id": facility_site_id,
                                     "certificate_ref": certificate_ref,
                                     "items": certified_items},
                             occurred_at=now)
                return "disposal_certificate", certificate_id, {
                    "certificate_id": certificate_id, "container_id": container_id,
                    "status": STATUS_CLOSED}

            return self._idempotent(connection, request_id=request_id,
                                    action="certify_disposal", payload=body, create=create)

    # ---- 查询：当前责任方、逾期、数量差异、双向追溯 -----------------------

    def get_container(self, container_id: str) -> WasteContainer:
        row = self._container_row(self.database.connection, container_id)
        return WasteContainer(
            row["container_id"], row["origin_site_id"], row["status"],
            row["regulation_version_id"], json.loads(row["regulation_snapshot_json"]),
            row["regulation_snapshot_hash"], row["gross_weight"], row["sealed_by"],
            row["sealed_at"], tuple(json.loads(row["predecessor_container_ids_json"])),
            row["closed_at"], row["closed_by"])

    def get_lot(self, lot_id: str) -> WasteLot:
        row = self.database.connection.execute(
            "SELECT * FROM wr_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFoundError("废弃物批次不存在")
        return WasteLot(row["lot_id"], row["site_id"], row["waste_category"],
                        row["hazard_class"], row["quantity"], row["unit"], row["packaging"],
                        row["permit_id"], row["generated_at"], row["generated_by"],
                        row["created_at"])

    def manifest(self, container_id: str) -> list[ManifestEntry]:
        self._container_row(self.database.connection, container_id)
        return [ManifestEntry(row["container_id"], row["lot_id"], row["quantity"], row["unit"])
                for row in self.database.connection.execute(
                    "SELECT * FROM wr_manifest WHERE container_id=? ORDER BY lot_id",
                    (container_id,))]

    def list_transfers(self, container_id: str) -> list[CustodyTransfer]:
        return [self._transfer_dataclass(row) for row in self.database.connection.execute(
            "SELECT * FROM wr_custody_transfers WHERE container_id=? ORDER BY proposed_at, transfer_id",
            (container_id,))]

    def current_responsibility(self, container_id: str) -> dict[str, Any]:
        """推导当前责任方与所在位置（暂存优先，其次最近一次已确认移交）。"""

        connection = self.database.connection
        container = self._container_row(connection, container_id)
        if container["status"] == STATUS_CLOSED:
            return {"container_id": container_id, "responsible_party": container["closed_by"],
                    "site_id": None, "in_storage": False, "in_transit": False,
                    "responsibility_closed": True, "basis": "disposal_certificate",
                    "since": container["closed_at"]}
        stay = connection.execute(
            "SELECT * FROM wr_storage_stays WHERE container_id=? AND checked_out_at IS NULL "
            "ORDER BY checked_in_at DESC LIMIT 1",
            (container_id,),
        ).fetchone()
        if stay:
            return {"container_id": container_id, "responsible_party": stay["keeper"],
                    "site_id": stay["site_id"], "in_storage": True, "in_transit": False,
                    "basis": "open_storage_stay", "since": stay["checked_in_at"]}
        transfer = connection.execute(
            "SELECT * FROM wr_custody_transfers WHERE container_id=? AND status='confirmed' "
            "ORDER BY confirmed_at DESC LIMIT 1",
            (container_id,),
        ).fetchone()
        if transfer:
            return {"container_id": container_id, "responsible_party": transfer["to_party"],
                    "site_id": transfer["to_site_id"],
                    "in_storage": False, "in_transit": transfer["to_site_id"] is None,
                    "basis": transfer["transfer_type"], "since": transfer["confirmed_at"],
                    "transfer_id": transfer["transfer_id"]}
        container = self._container_row(connection, container_id)
        return {"container_id": container_id, "responsible_party": container["sealed_by"],
                "site_id": container["origin_site_id"], "in_storage": False,
                "in_transit": False, "basis": "sealed_origin", "since": container["sealed_at"]}

    def overdue_nodes(self) -> dict[str, list[dict[str, Any]]]:
        """找出逾期未确认移交、逾期未出库暂存、目的地逾期未签收和破损未重装箱。"""

        connection = self.database.connection
        now = self._now()
        result: dict[str, list[dict[str, Any]]] = {
            "pending_transfers": [], "overdue_storage": [], "pending_receipts": [],
            "damaged_open": [],
        }
        for row in connection.execute(
                "SELECT * FROM wr_custody_transfers WHERE status='proposed' AND confirm_due_at<?",
                (now,)):
            result["pending_transfers"].append(
                {"transfer_id": row["transfer_id"], "container_id": row["container_id"],
                 "transfer_type": row["transfer_type"], "from_party": row["from_party"],
                 "to_party": row["to_party"], "confirm_due_at": row["confirm_due_at"]})
        for row in connection.execute(
                "SELECT * FROM wr_storage_stays WHERE checked_out_at IS NULL AND due_at<?",
                (now,)):
            result["overdue_storage"].append(
                {"stay_id": row["stay_id"], "container_id": row["container_id"],
                 "site_id": row["site_id"], "keeper": row["keeper"], "due_at": row["due_at"]})
        for row in connection.execute(
                "SELECT t.* FROM wr_custody_transfers t "
                "JOIN wr_containers c ON c.container_id=t.container_id "
                "WHERE t.status='confirmed' AND t.transfer_type='destination_delivery' "
                "AND c.status='at_facility' AND NOT EXISTS ("
                "SELECT 1 FROM wr_receipts r WHERE r.transfer_id=t.transfer_id)"):
            container = self._container_row(connection, row["container_id"])
            snapshot = json.loads(container["regulation_snapshot_json"])
            confirmed = datetime.fromisoformat(row["confirmed_at"].replace("Z", "+00:00"))
            receipt_due = (confirmed + timedelta(
                hours=snapshot["deadlines"]["receipt_due_hours"])).isoformat().replace("+00:00", "Z")
            if receipt_due < now:
                result["pending_receipts"].append(
                    {"transfer_id": row["transfer_id"], "container_id": row["container_id"],
                     "to_party": row["to_party"], "receipt_due_at": receipt_due})
        for row in connection.execute("SELECT * FROM wr_containers WHERE status=?",
                                      (STATUS_DAMAGED,)):
            if self._container_balances(connection, row["container_id"]):
                result["damaged_open"].append(
                    {"container_id": row["container_id"], "origin_site_id": row["origin_site_id"],
                     "sealed_by": row["sealed_by"]})
        return result

    def quantity_variance(self) -> list[dict[str, Any]]:
        """按批次核对登记（含冲销）、封箱、在途与已处置数量的差异。"""

        connection = self.database.connection
        rows = []
        for lot in connection.execute("SELECT * FROM wr_lots ORDER BY lot_id"):
            lot_id = lot["lot_id"]
            corrected = self._correction_sum(connection, lot_id)
            sealed = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM wr_manifest WHERE lot_id=?",
                (lot_id,)).fetchone()["total"]
            disposed = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM wr_edges "
                "WHERE lot_id=? AND reason='disposal'",
                (lot_id,)).fetchone()["total"]
            registered_effective = lot["quantity"] + corrected
            rows.append({
                "lot_id": lot_id, "unit": lot["unit"],
                "registered_quantity": lot["quantity"],
                "correction_total": round(corrected, 9),
                "registered_effective": round(registered_effective, 9),
                "sealed_quantity": round(sealed, 9),
                "disposed_quantity": round(disposed, 9),
                "in_circulation": round(sealed - disposed, 9),
                "unaccounted_vs_registered": round(registered_effective - sealed, 9),
                "open_responsibility": round(sealed - disposed, 9) != 0,
            })
        return rows

    def trace_lot(self, lot_id: str) -> dict[str, Any]:
        """从来源批次正向追踪到最终去向（含修正、全部血缘边与凭证）。"""

        connection = self.database.connection
        lot = connection.execute("SELECT * FROM wr_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFoundError("废弃物批次不存在")
        edges = [self._edge_dataclass(row) for row in connection.execute(
            "SELECT * FROM wr_edges WHERE lot_id=? ORDER BY edge_id", (lot_id,))]
        corrections = [LotCorrection(row["event_id"], row["lot_id"], row["delta"],
                                     row["reason"], row["corrected_by"], row["created_at"])
                       for row in connection.execute(
                           "SELECT * FROM wr_lot_corrections WHERE lot_id=? ORDER BY created_at",
                           (lot_id,))]
        certificates = []
        disposed_total = 0.0
        for edge in edges:
            if edge.reason == "disposal":
                disposed_total += edge.quantity
                cert = connection.execute(
                    "SELECT * FROM wr_disposal_certificates WHERE event_id=?",
                    (edge.event_id,)).fetchone()
                if cert:
                    certificates.append(self._certificate_dataclass(cert))
        return {
            "lot": self.get_lot(lot_id).__dict__,
            "corrections": [item.__dict__ for item in corrections],
            "registered_effective": round(
                lot["quantity"] + sum(item.delta for item in corrections), 9),
            "edges": [edge.__dict__ for edge in edges],
            "disposed_quantity": round(disposed_total, 9),
            "final_disposition": "disposed" if disposed_total > 0 and self._lot_fully_disposed(
                connection, lot_id) else ("partial" if disposed_total > 0 else "in_circulation"),
            "certificates": [item.__dict__ for item in certificates],
        }

    def trace_container(self, container_id: str) -> dict[str, Any]:
        """从箱子反查全部组成：递归血缘直到产生批次，并附责任时间线。"""

        connection = self.database.connection
        self._container_row(connection, container_id)
        composition = self._composition(connection, container_id)
        stays = [StorageStay(row["stay_id"], row["container_id"], row["site_id"],
                             row["keeper"], row["handed_by"], row["checked_in_at"], row["due_at"],
                             row["checked_out_at"], row["released_by"]).__dict__
                 for row in connection.execute(
                     "SELECT * FROM wr_storage_stays WHERE container_id=? ORDER BY checked_in_at",
                     (container_id,))]
        transfers = [self._transfer_dataclass(row).__dict__ for row in connection.execute(
            "SELECT * FROM wr_custody_transfers WHERE container_id=? ORDER BY proposed_at",
            (container_id,))]
        receipts = [dict(row) for row in connection.execute(
            "SELECT receipt_id,transfer_id,acknowledged_by,acknowledged_at,items_json "
            "FROM wr_receipts WHERE container_id=? ORDER BY acknowledged_at", (container_id,))]
        for receipt in receipts:
            receipt["items"] = json.loads(receipt.pop("items_json"))
        cert_rows = connection.execute(
            "SELECT * FROM wr_disposal_certificates WHERE container_id=?", (container_id,)).fetchall()
        certificates = [self._certificate_dataclass(row).__dict__ for row in cert_rows]
        return {
            "container": self.get_container(container_id).__dict__,
            "composition": composition,
            "storage_stays": stays,
            "transfers": transfers,
            "receipts": receipts,
            "certificates": certificates,
            "current_responsibility": self.current_responsibility(container_id),
        }

    # ---- 内部辅助 -------------------------------------------------------

    def _replay_if_seen(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> WriteReceipt | None:
        """在状态机校验之前短路相同 request_id 的重复回调。

        进程恢复或网络重试会在状态已推进（容器已关闭、已出库、已确认）后
        重放同一请求；若不先核销，后续状态校验会把合法重放误判为冲突。
        因此每个写操作在权限校验后、状态校验前调用本方法。
        """

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _assert_within_registered(self, connection, entries: list[dict[str, Any]]) -> None:
        for item in entries:
            lot = self._lot_row(connection, item["lot_id"])
            effective = lot["quantity"] + self._correction_sum(connection, lot["lot_id"])
            already = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM wr_manifest WHERE lot_id=?",
                (item["lot_id"],)).fetchone()["total"]
            if already + item["quantity"] > effective + 1e-9:
                raise ConflictError(
                    f"批次 {item['lot_id']} 封箱总量 {already + item['quantity']} "
                    f"超过登记（含冲销）数量 {effective}")

    def _positive(self, value: Any, field: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValidationError(f"{field} 必须是正数")
        return float(value)

    def _assume_site(self, connection, site_id: str, actor) -> None:
        site = connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")

    def _validated_entries(self, connection, entries: Any) -> list[dict[str, Any]]:
        if not isinstance(entries, list) or not entries:
            raise ValidationError("entries 必须是非空数组")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValidationError("entries 每一项必须是对象")
            lot_id = self._identifier(raw["lot_id"], "lot_id")
            if lot_id in seen:
                raise ValidationError(f"批次 {lot_id} 在同一清单中重复")
            seen.add(lot_id)
            lot = self._lot_row(connection, lot_id)
            qty = self._positive(raw["quantity"], "quantity")
            unit = raw.get("unit", lot["unit"])
            if unit != lot["unit"]:
                raise ValidationError(f"批次 {lot_id} 的单位必须是 {lot['unit']}")
            result.append({"lot_id": lot_id, "quantity": qty, "unit": unit})
        return result

    def _lot_row(self, connection, lot_id: str):
        row = connection.execute("SELECT * FROM wr_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"废弃物批次 {lot_id} 不存在")
        return row

    def _container_row(self, connection, container_id: str):
        row = connection.execute(
            "SELECT * FROM wr_containers WHERE container_id=?", (container_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"回运箱 {container_id} 不存在")
        return row

    def _transfer_row(self, connection, transfer_id: str):
        row = connection.execute(
            "SELECT * FROM wr_custody_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFoundError("移交记录不存在")
        return row

    def _assert_no_open_stay(self, connection, container_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM wr_storage_stays WHERE container_id=? AND checked_out_at IS NULL",
            (container_id,)).fetchone()
        if row:
            raise ConflictError("容器已有未出库的暂存记录")

    def _set_status(self, connection, container_id: str, status: str) -> None:
        connection.execute("UPDATE wr_containers SET status=? WHERE container_id=?",
                           (status, container_id))

    def _correction_sum(self, connection, lot_id: str) -> float:
        return connection.execute(
            "SELECT COALESCE(SUM(delta),0) AS total FROM wr_lot_corrections WHERE lot_id=?",
            (lot_id,)).fetchone()["total"]

    def _container_balances(self, connection, container_id: str) -> dict[str, float]:
        """按血缘边重算某箱当前各批次的在箱数量。"""

        balances: dict[str, float] = {}
        for row in connection.execute(
                "SELECT lot_id, "
                "COALESCE(SUM(CASE WHEN to_container_id=? THEN quantity END),0) AS inbound, "
                "COALESCE(SUM(CASE WHEN from_container_id=? THEN quantity END),0) AS outbound "
                "FROM wr_edges WHERE to_container_id=? OR from_container_id=? GROUP BY lot_id",
                (container_id, container_id, container_id, container_id)):
            remaining = row["inbound"] - row["outbound"]
            if remaining > 1e-9:
                balances[row["lot_id"]] = round(remaining, 9)
        return balances

    def _lot_fully_disposed(self, connection, lot_id: str) -> bool:
        sealed = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS total FROM wr_manifest WHERE lot_id=?",
            (lot_id,)).fetchone()["total"]
        disposed = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) AS total FROM wr_edges "
            "WHERE lot_id=? AND reason='disposal'",
            (lot_id,)).fetchone()["total"]
        return sealed > 0 and abs(sealed - disposed) < 1e-9

    def _composition(self, connection, container_id: str) -> list[dict[str, Any]]:
        """递归把箱内数量回溯到产生批次（支持多代重装与已处置箱）。

        以全部入箱边数量（而非净余额）播种，因此处置凭证关闭后仍可从箱子
        反查它曾包含的全部组成。封箱容器的入箱边来自产生（from 为空），
        重装容器的入箱边指向前箱；沿 repack 边按入边顺序逐条分摊上溯，
        数量在每一层守恒。
        """

        lot_ids = {row["lot_id"] for row in connection.execute(
            "SELECT DISTINCT lot_id FROM wr_edges WHERE to_container_id=? OR from_container_id=?",
            (container_id, container_id))}
        closed = self._container_row(connection, container_id)["status"] == STATUS_CLOSED
        resolved: dict[tuple[str, str], float] = {}
        for lot_id in lot_ids:
            inbound = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM wr_edges "
                "WHERE to_container_id=? AND lot_id=?", (container_id, lot_id)).fetchone()["total"]
            outbound_repack = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM wr_edges "
                "WHERE from_container_id=? AND lot_id=? AND reason='repack'",
                (container_id, lot_id)).fetchone()["total"]
            outbound_disposal = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) AS total FROM wr_edges "
                "WHERE from_container_id=? AND lot_id=? AND reason='disposal'",
                (container_id, lot_id)).fetchone()["total"]
            net = inbound - outbound_repack - outbound_disposal
            if net > 1e-9:
                seed = net
            elif closed and inbound - outbound_repack > 1e-9:
                seed = inbound - outbound_repack       # 关闭箱：处置时持有量
            else:
                seed = inbound                          # 完全重装箱：原始组成
            if seed > 1e-9:
                self._resolve_origin(connection, container_id, lot_id, seed, resolved, set())
        items = []
        for (source, lot_id), amount in sorted(resolved.items()):
            lot = self._lot_row(connection, lot_id)
            items.append({"lot_id": lot_id, "waste_category": lot["waste_category"],
                          "quantity": round(amount, 9), "unit": lot["unit"],
                          "origin_site_id": lot["site_id"], "permit_id": lot["permit_id"],
                          "sealed_into_container": source})
        return items

    def _resolve_origin(self, connection, container_id: str, lot_id: str, amount: float,
                        resolved: dict[tuple[str, str], float], path: set[str]) -> None:
        """把 amount 数量的 lot_id 经 container_id 递归回溯到产生封箱箱。"""

        if amount <= 1e-9:
            return
        key = (container_id, lot_id)
        if container_id in path:
            # 防御性：血缘不应成环；成环则停在当前容器以免无限递归。
            resolved[key] = resolved.get(key, 0.0) + amount
            return
        inbound = connection.execute(
            "SELECT * FROM wr_edges WHERE to_container_id=? AND lot_id=? ORDER BY edge_id",
            (container_id, lot_id)).fetchall()
        from_edges = [edge for edge in inbound if edge["from_container_id"] is not None]
        if not from_edges:
            resolved[key] = resolved.get(key, 0.0) + amount
            return
        next_path = path | {container_id}
        remaining = amount
        for edge in from_edges:
            if remaining <= 1e-9:
                break
            take = min(remaining, edge["quantity"])
            self._resolve_origin(connection, edge["from_container_id"], lot_id, take,
                                 resolved, next_path)
            remaining -= take
        if remaining > 1e-9:
            # 请求数量超过重装入边总量（理论上不发生），余额归到当前箱来源。
            resolved[key] = resolved.get(key, 0.0) + remaining

    def _edge_dataclass(self, row) -> LineageEdge:
        return LineageEdge(row["edge_id"], row["event_id"], row["lot_id"],
                           row["from_container_id"], row["to_container_id"], row["quantity"],
                           row["reason"], row["source_event_id"], row["acknowledged_at"],
                           row["acknowledged_by"])

    def _transfer_dataclass(self, row) -> CustodyTransfer:
        return CustodyTransfer(row["transfer_id"], row["transfer_type"], row["custody_kind"],
                               row["container_id"], row["from_party"], row["to_party"],
                               row["from_site_id"], row["to_site_id"], row["status"],
                               row["reversal_of"], row["proposed_by"], row["proposed_at"],
                               row["confirm_due_at"], row["confirmed_by"], row["confirmed_at"],
                               json.loads(row["detail_json"]))

    def _certificate_dataclass(self, row) -> DisposalCertificate:
        return DisposalCertificate(row["certificate_id"], row["container_id"],
                                   row["facility_site_id"], row["certificate_ref"],
                                   tuple(json.loads(row["items_json"])), row["certified_by"],
                                   row["certified_at"], row["event_id"])
