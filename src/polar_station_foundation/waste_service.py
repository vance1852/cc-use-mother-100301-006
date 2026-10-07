"""废弃物回运责任项目的领域服务。

责任链：产生地登记分类 → 封箱（冻结法规快照）→ 暂存/跨营地/承运的双方
确认移交 → 目的地接收 → 处置凭证关闭责任。

所有业务事实以追加事件写入 ``waste_events``；破损、运输取消、数量修正和
接收拒绝都产生新的冲销、取消或重分配事件，已经确认的责任只能被新事件
接续，不能被回滚或改写。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable

from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .storage import Database
from .waste_models import ContainerView, Custodian, Regulation, WasteEvent, WasteItem
from .waste_rules import (
    CUSTODIAN_KINDS,
    HANDOVER_KINDS,
    JUMP_ACTIONS,
    STAGE_AFTER_CONFIRM,
    STAGE_BEFORE_HANDOVER,
    WASTE_TYPES,
    deadline_for,
    find_incompatible_pairs,
    max_total_kg,
    missing_qualifications,
    normalize_regulation,
    regulation_hash,
)

CUSTODIAN_KIND_FOR_HANDOVER = {
    "staging": "staging_yard",
    "cross_camp": "camp",
    "carrier": "carrier",
    "destination": "disposal_facility",
    "return": "camp",
}
NEXT_OBLIGATION = {
    "staging": "staging",
    "cross_camp": "staging",
    "return": "staging",
    "carrier": "transit",
    "destination": "disposal",
}
DAMAGE_SEVERITIES = frozenset({"minor", "breached", "destroyed"})


class WasteService:
    """实施废弃物回运责任的状态机、幂等、冻结与查询规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> datetime:
        return self.clock.now()

    def _now_text(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        if actor.role not in ("admin", "operator"):
            raise PermissionDenied("当前角色不能执行废弃物回运写操作")
        return actor

    def _custodian(self, connection, custodian_id: str):
        row = connection.execute(
            "SELECT * FROM waste_custodians WHERE custodian_id=?", (custodian_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("责任主体不存在")
        if not row["active"]:
            raise PermissionDenied("责任主体已停用")
        return row

    def _require_custodian_scope(self, actor: Actor, custodian_row) -> None:
        if actor.role != "admin" and actor.organization_id != custodian_row["organization_id"]:
            raise PermissionDenied("不能代表其他组织的责任主体操作")

    def _shipment(self, connection, shipment_id: str):
        row = connection.execute(
            "SELECT * FROM waste_shipments WHERE shipment_id=?", (shipment_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("废弃物回运项目不存在")
        if row["status"] != "open":
            raise ConflictError("项目责任已经关闭，只能查询，不能继续办理")
        return row

    def _regulation(self, connection, regulation_id: str):
        row = connection.execute(
            "SELECT * FROM waste_regulations WHERE regulation_id=?", (regulation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("法规版本不存在")
        return row

    def _regulation_snapshot(self, regulation_row) -> dict[str, Any]:
        snapshot = json.loads(regulation_row["content_json"])
        if regulation_hash(snapshot) != regulation_row["content_hash"]:
            raise ConflictError("法规版本内容与摘要不一致")
        return snapshot

    def _kg(self, value: Any, field: str, *, allow_zero: bool = False) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是数字千克")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValidationError(f"{field} 必须是正数千克")
        return float(value)

    def _append_event(self, connection, *, event_type: str, shipment_id: str,
                      payload: dict[str, Any], actor_id: str,
                      containers: list[str] | None = None,
                      items: list[str] | None = None) -> WasteEvent:
        """向追加式业务台账写事件，并登记容器/批次反查引用。"""

        event_id = uuid.uuid4().hex
        occurred_at = self._now_text()
        cursor = connection.execute(
            "INSERT INTO waste_events(event_id,shipment_id,event_type,payload_json,actor_id,occurred_at) "
            "VALUES(?,?,?,?,?,?)",
            (event_id, shipment_id, event_type, canonical_json(payload), actor_id, occurred_at),
        )
        sequence = cursor.lastrowid
        refs: set[tuple[str | None, str | None]] = set()
        for container_id in containers or []:
            refs.add((container_id, None))
        for item_id in items or []:
            refs.add((None, item_id))
        if not refs:
            refs.add((None, None))
        for container_id, item_id in refs:
            connection.execute(
                "INSERT INTO waste_event_refs(event_id,shipment_id,container_id,item_id) VALUES(?,?,?,?)",
                (event_id, shipment_id, container_id, item_id),
            )
        append_event(connection, actor_id=actor_id, action=f"waste.{event_type}",
                     resource_type="waste_shipment", resource_id=shipment_id,
                     detail={"event_id": event_id, **payload}, occurred_at=occurred_at)
        return WasteEvent(sequence, event_id, shipment_id, event_type, payload, actor_id, occurred_at)

    def _replay_receipt(self, connection, request_id: str, action: str,
                        payload: dict[str, Any]) -> WriteReceipt | None:
        """命中既有回执时直接回放，使进程恢复或重复回调保持幂等。"""

        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _store_receipt(self, connection, *, request_id: str, action: str,
                       payload: dict[str, Any], resource_type: str, resource_id: str,
                       response: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json(response), self._now_text()),
        )

    def _commit_idempotent(self, connection, *, request_id: str, action: str,
                           payload: dict[str, Any],
                           create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        replayed = self._replay_receipt(connection, request_id, action, payload)
        if replayed is not None:
            return replayed
        resource_type, resource_id, response = create()
        self._store_receipt(connection, request_id=request_id, action=action, payload=payload,
                            resource_type=resource_type, resource_id=resource_id, response=response)
        return WriteReceipt(request_id, resource_type, resource_id, False)

    # ---------------------------------------------------------- 法规与主体

    def register_regulation(self, *, request_id: str, actor_id: str, regulation_id: str,
                            version_label: str, content: dict[str, Any],
                            effective_from: str) -> WriteReceipt:
        """登记一个法规版本；内容不可修改，封箱时复制快照。"""

        payload = {"actor_id": actor_id, "regulation_id": regulation_id,
                   "version_label": version_label, "content": content,
                   "effective_from": effective_from}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            snapshot = normalize_regulation(content)
            content_hash = regulation_hash(snapshot)
            version_label = str(version_label).strip()
            if not version_label:
                raise ValidationError("version_label 不能为空")
            try:
                datetime.fromisoformat(str(effective_from).replace("Z", "+00:00"))
            except (AttributeError, ValueError) as exc:
                raise ValidationError("effective_from 必须是 ISO 时间") from exc

            def create():
                try:
                    connection.execute(
                        "INSERT INTO waste_regulations(regulation_id,version_label,content_json,"
                        "content_hash,effective_from,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (regulation_id, version_label, canonical_json(snapshot), content_hash,
                         effective_from, actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("法规编号或内容摘要已经存在") from exc
                self._append_event(connection, event_type="regulation.registered",
                                   shipment_id=f"regulation:{regulation_id}",
                                   payload={"regulation_id": regulation_id,
                                            "version_label": version_label,
                                            "content_hash": content_hash,
                                            "effective_from": effective_from},
                                   actor_id=actor_id)
                return ("waste_regulation", regulation_id,
                        {"regulation_id": regulation_id, "content_hash": content_hash})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_register_regulation",
                                           payload=payload, create=create)

    def register_custodian(self, *, request_id: str, actor_id: str, custodian_id: str,
                           organization_id: str, kind: str, name: str,
                           site_id: str | None = None,
                           qualifications: list[str] | None = None) -> WriteReceipt:
        """登记营地、暂存场、承运方或处置设施等责任主体。"""

        payload = {"actor_id": actor_id, "custodian_id": custodian_id,
                   "organization_id": organization_id, "kind": kind, "name": name,
                   "site_id": site_id, "qualifications": qualifications or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            if kind not in CUSTODIAN_KINDS:
                raise ValidationError("责任主体类别无效")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            if actor.role != "admin" and actor.organization_id != organization_id:
                raise PermissionDenied("不能为其他组织登记责任主体")
            if site_id is not None:
                site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
                if site is None:
                    raise NotFoundError("场所不存在")
                if site["organization_id"] != organization_id:
                    raise ValidationError("场所不属于责任主体所在组织")
            qualifications = sorted(qualifications or [])
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO waste_custodians(custodian_id,organization_id,site_id,kind,name,"
                        "qualifications_json,active,created_by,created_at) VALUES(?,?,?,?,?,?,1,?,?)",
                        (custodian_id, organization_id, site_id, kind, name,
                         canonical_json(qualifications), actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("责任主体编号已经存在") from exc
                self._append_event(connection, event_type="custodian.registered",
                                   shipment_id=f"custodian:{custodian_id}",
                                   payload={"custodian_id": custodian_id, "kind": kind,
                                            "organization_id": organization_id, "name": name,
                                            "qualifications": qualifications},
                                   actor_id=actor_id)
                return ("waste_custodian", custodian_id, {"custodian_id": custodian_id})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_register_custodian",
                                           payload=payload, create=create)

    def create_shipment(self, *, request_id: str, actor_id: str, shipment_id: str,
                        title: str, regulation_id: str) -> WriteReceipt:
        """建立年度撤站废弃物回运责任项目。"""

        payload = {"actor_id": actor_id, "shipment_id": shipment_id,
                   "title": title, "regulation_id": regulation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._regulation(connection, regulation_id)
            title = str(title).strip()
            if not title:
                raise ValidationError("title 不能为空")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO waste_shipments(shipment_id,title,default_regulation_id,status,"
                        "created_by,created_at) VALUES(?,?,?, 'open',?,?)",
                        (shipment_id, title, regulation_id, actor_id, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("项目编号已经存在") from exc
                self._append_event(connection, event_type="shipment.created",
                                   shipment_id=shipment_id,
                                   payload={"shipment_id": shipment_id, "title": title,
                                            "regulation_id": regulation_id},
                                   actor_id=actor_id)
                return ("waste_shipment", shipment_id, {"shipment_id": shipment_id})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_create_shipment",
                                           payload=payload, create=create)

    # ------------------------------------------------------------- 产生/封箱

    def generate_waste(self, *, request_id: str, actor_id: str, shipment_id: str,
                       waste_type: str, quantity_kg: float, origin_custodian_id: str,
                       item_id: str | None = None) -> WriteReceipt:
        """在产生地点登记一类废弃物的分类、数量与产生责任。"""

        payload = {"actor_id": actor_id, "shipment_id": shipment_id, "waste_type": waste_type,
                   "quantity_kg": quantity_kg, "origin_custodian_id": origin_custodian_id,
                   "item_id": item_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, "waste_generate", payload)
            if replayed is not None:
                return replayed
            self._shipment(connection, shipment_id)
            origin = self._custodian(connection, origin_custodian_id)
            self._require_custodian_scope(actor, origin)
            if waste_type not in WASTE_TYPES:
                raise ValidationError("废弃物类别无效")
            quantity_kg = self._kg(quantity_kg, "quantity_kg")
            item_id = item_id or uuid.uuid4().hex
            now = self._now_text()

            def create():
                if connection.execute("SELECT 1 FROM waste_items WHERE item_id=?",
                                      (item_id,)).fetchone():
                    raise ConflictError("废弃物批次编号已经存在")
                connection.execute(
                    "INSERT INTO waste_items(item_id,shipment_id,waste_type,initial_kg,current_kg,"
                    "origin_custodian_id,status,current_container_id,generated_at,generated_by) "
                    "VALUES(?,?,?,?,?,?, 'at_camp', NULL,?,?)",
                    (item_id, shipment_id, waste_type, quantity_kg, quantity_kg,
                     origin_custodian_id, now, actor_id),
                )
                self._append_event(connection, event_type="waste.generated",
                                   shipment_id=shipment_id,
                                   payload={"item_id": item_id, "waste_type": waste_type,
                                            "quantity_kg": quantity_kg,
                                            "origin_custodian_id": origin_custodian_id},
                                   actor_id=actor_id, items=[item_id])
                return ("waste_item", item_id, {"item_id": item_id})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_generate",
                                           payload=payload, create=create)

    def _load_seal_inputs(self, connection, shipment_id, origin_custodian_id, item_ids,
                          packaging, gross_kg, regulation_id):
        origin = self._custodian(connection, origin_custodian_id)
        regulation = self._regulation(connection, regulation_id)
        snapshot = self._regulation_snapshot(regulation)
        packaging = str(packaging).strip()
        if not packaging:
            raise ValidationError("packaging 不能为空")
        gross_kg = self._kg(gross_kg, "gross_kg")
        if not item_ids:
            raise ValidationError("封箱至少包含一个废弃物批次")
        items = []
        waste_types: list[str] = []
        net_kg = 0.0
        seen: set[str] = set()
        for item_id in item_ids:
            if item_id in seen:
                raise ValidationError(f"批次 {item_id} 在封箱清单中重复")
            seen.add(item_id)
            item = connection.execute("SELECT * FROM waste_items WHERE item_id=?",
                                      (item_id,)).fetchone()
            if item is None:
                raise NotFoundError(f"废弃物批次 {item_id} 不存在")
            if item["shipment_id"] != shipment_id:
                raise ValidationError(f"批次 {item_id} 不属于本项目")
            if item["status"] != "at_camp" or item["current_container_id"] is not None:
                raise ConflictError(f"批次 {item_id} 不在可封箱状态")
            if item["origin_custodian_id"] != origin_custodian_id:
                raise ValidationError(f"批次 {item_id} 产生地与封箱地不一致")
            items.append(item)
            waste_types.append(item["waste_type"])
            net_kg += item["current_kg"]
        violations = find_incompatible_pairs(waste_types, snapshot)
        if violations:
            raise ValidationError(f"混装限制被违反：{canonical_json(violations)}")
        cap = max_total_kg(snapshot)
        if cap is not None and net_kg > cap:
            raise ValidationError(f"箱内废弃物净重 {net_kg}kg 超过法规上限 {cap}kg")
        if gross_kg < net_kg:
            raise ValidationError("箱体毛重不能小于箱内废弃物净重")
        return origin, regulation, snapshot, packaging, gross_kg, items, round(net_kg, 6)

    def seal_container(self, *, request_id: str, actor_id: str, shipment_id: str,
                       container_id: str, origin_custodian_id: str, item_ids: list[str],
                       packaging: str, gross_kg: float,
                       regulation_id: str | None = None) -> WriteReceipt:
        """封箱并冻结适用法规版本、混装限制、期限与责任人资格要求。"""

        payload = {"actor_id": actor_id, "shipment_id": shipment_id,
                   "container_id": container_id, "origin_custodian_id": origin_custodian_id,
                   "item_ids": list(item_ids), "packaging": packaging, "gross_kg": gross_kg,
                   "regulation_id": regulation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, "waste_seal_container", payload)
            if replayed is not None:
                return replayed
            shipment = self._shipment(connection, shipment_id)
            regulation_id = regulation_id or shipment["default_regulation_id"]
            (origin, regulation, snapshot, packaging, gross_kg,
             items, net_kg) = self._load_seal_inputs(
                connection, shipment_id, origin_custodian_id, item_ids,
                packaging, gross_kg, regulation_id)
            self._require_custodian_scope(actor, origin)
            sealed_at = self._now_text()
            composition = [{"item_id": item["item_id"], "waste_type": item["waste_type"],
                            "kg": item["current_kg"]} for item in items]

            def create():
                if connection.execute("SELECT 1 FROM waste_containers WHERE container_id=?",
                                      (container_id,)).fetchone():
                    raise ConflictError("容器编号已经存在")
                connection.execute(
                    "INSERT INTO waste_containers(container_id,shipment_id,origin_custodian_id,"
                    "current_custodian_id,packaging,gross_kg,regulation_id,regulation_hash,"
                    "regulation_snapshot_json,status,sealed_at,sealed_by,sealed_event_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?, 'sealed',?,?,?)",
                    (container_id, shipment_id, origin_custodian_id, origin_custodian_id,
                     packaging, gross_kg, regulation_id, regulation["content_hash"],
                     canonical_json(snapshot), sealed_at, actor_id, "(pending)"),
                )
                event = self._append_event(
                    connection, event_type="container.sealed", shipment_id=shipment_id,
                    payload={"container_id": container_id,
                             "origin_custodian_id": origin_custodian_id,
                             "packaging": packaging, "gross_kg": gross_kg, "net_kg": net_kg,
                             "regulation_id": regulation_id,
                             "regulation_hash": regulation["content_hash"],
                             "regulation_version": regulation["version_label"],
                             "composition": composition},
                    actor_id=actor_id,
                    containers=[container_id],
                    items=[item["item_id"] for item in items],
                )
                connection.execute(
                    "UPDATE waste_containers SET sealed_event_id=? WHERE container_id=?",
                    (event.event_id, container_id),
                )
                for item in items:
                    connection.execute(
                        "UPDATE waste_items SET status='in_container',current_container_id=? "
                        "WHERE item_id=?",
                        (container_id, item["item_id"]),
                    )
                    connection.execute(
                        "INSERT INTO waste_container_items(container_id,item_id) VALUES(?,?)",
                        (container_id, item["item_id"]),
                    )
                return ("waste_container", container_id,
                        {"container_id": container_id,
                         "regulation_hash": regulation["content_hash"]})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_seal_container",
                                           payload=payload, create=create)

    # ------------------------------------------------------------------ 移交

    def _container_row(self, connection, container_id: str):
        row = connection.execute("SELECT * FROM waste_containers WHERE container_id=?",
                                 (container_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"容器 {container_id} 不存在")
        return row

    def _container_items(self, connection, container_id: str):
        return connection.execute(
            "SELECT * FROM waste_items WHERE current_container_id=? ORDER BY item_id",
            (container_id,),
        ).fetchall()

    def propose_handover(self, *, request_id: str, actor_id: str, shipment_id: str,
                         kind: str, from_custodian_id: str, to_custodian_id: str,
                         container_ids: list[str], callback_token: str | None = None,
                         move_id: str | None = None,
                         note: str | None = None) -> WriteReceipt:
        """提出暂存、跨营地、承运或目的地移交；接收方资格按各箱冻结快照校验。"""

        payload = {"actor_id": actor_id, "shipment_id": shipment_id, "kind": kind,
                   "from_custodian_id": from_custodian_id, "to_custodian_id": to_custodian_id,
                   "container_ids": list(container_ids), "callback_token": callback_token,
                   "move_id": move_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, "waste_propose_handover", payload)
            if replayed is not None:
                return replayed
            self._shipment(connection, shipment_id)
            if kind not in HANDOVER_KINDS:
                raise ValidationError("移交种类无效")
            sender = self._custodian(connection, from_custodian_id)
            receiver = self._custodian(connection, to_custodian_id)
            self._require_custodian_scope(actor, sender)
            if from_custodian_id == to_custodian_id:
                raise ValidationError("移交双方不能是同一责任主体")
            if receiver["kind"] != CUSTODIAN_KIND_FOR_HANDOVER[kind]:
                raise ValidationError(
                    f"{kind} 移交的接收方必须是 {CUSTODIAN_KIND_FOR_HANDOVER[kind]}")
            if not container_ids:
                raise ValidationError("移交至少包含一个容器")
            move_id = move_id or uuid.uuid4().hex
            now_dt = self._now()
            manifests: dict[str, dict[str, Any]] = {}
            seen: set[str] = set()
            for container_id in container_ids:
                if container_id in seen:
                    raise ValidationError(f"容器 {container_id} 重复出现")
                seen.add(container_id)
                container = self._container_row(connection, container_id)
                if container["shipment_id"] != shipment_id:
                    raise ValidationError(f"容器 {container_id} 不属于本项目")
                if container["status"] not in STAGE_BEFORE_HANDOVER[kind]:
                    raise ConflictError(
                        f"容器 {container_id} 当前状态 {container['status']} 不能办理 {kind} 移交")
                if container["current_custodian_id"] != from_custodian_id:
                    raise ConflictError(f"容器 {container_id} 当前不属于交出方")
                snapshot = json.loads(container["regulation_snapshot_json"])
                missing = missing_qualifications(kind, snapshot,
                                                 json.loads(receiver["qualifications_json"]))
                if missing:
                    raise PermissionDenied(
                        f"接收方缺少容器 {container_id} 冻结要求的资格：{canonical_json(missing)}")
                items = self._container_items(connection, container_id)
                if not items:
                    raise ConflictError(f"容器 {container_id} 中没有在管批次，不能移交")
                manifests[container_id] = {
                    "items": [{"item_id": item["item_id"], "kg": item["current_kg"]}
                              for item in items],
                    "gross_kg": container["gross_kg"],
                }

            def create():
                if connection.execute("SELECT 1 FROM waste_moves WHERE move_id=?",
                                      (move_id,)).fetchone():
                    raise ConflictError("移交编号已经存在")
                if callback_token is not None and connection.execute(
                        "SELECT 1 FROM waste_moves WHERE callback_token=?",
                        (callback_token,)).fetchone():
                    raise ConflictError("回调标识已经被其他移交使用")
                connection.execute(
                    "INSERT INTO waste_moves(move_id,shipment_id,kind,from_custodian_id,"
                    "to_custodian_id,callback_token,status,proposed_at,proposed_by,reason) "
                    "VALUES(?,?,?,?,?,?, 'proposed',?,?,?)",
                    (move_id, shipment_id, kind, from_custodian_id, to_custodian_id,
                     callback_token, self._now_text(), actor_id, note),
                )
                for container_id in container_ids:
                    connection.execute(
                        "INSERT INTO waste_move_items(move_id,container_id,manifest_json) VALUES(?,?,?)",
                        (move_id, container_id, canonical_json(manifests[container_id])),
                    )
                    container = self._container_row(connection, container_id)
                    snapshot = json.loads(container["regulation_snapshot_json"])
                    due = deadline_for("handover_confirm", snapshot, now_dt)
                    if due is not None:
                        connection.execute(
                            "INSERT INTO waste_obligations(obligation_id,shipment_id,container_id,"
                            "kind,responsible_custodian_id,due_at,status,created_at,created_move_id) "
                            "VALUES(?,?,?, 'handover_confirm',?,?, 'open',?,?)",
                            (uuid.uuid4().hex, shipment_id, container_id, to_custodian_id,
                             due.isoformat().replace("+00:00", "Z"), self._now_text(), move_id),
                        )
                self._append_event(
                    connection, event_type="handover.proposed", shipment_id=shipment_id,
                    payload={"move_id": move_id, "kind": kind,
                             "from_custodian_id": from_custodian_id,
                             "to_custodian_id": to_custodian_id,
                             "callback_token": callback_token, "note": note,
                             "manifests": manifests},
                    actor_id=actor_id, containers=list(container_ids),
                    items=[entry["item_id"] for manifest in manifests.values()
                           for entry in manifest["items"]])
                return ("waste_move", move_id, {"move_id": move_id, "status": "proposed"})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_propose_handover",
                                           payload=payload, create=create)

    def _load_proposed_move(self, connection, move_id: str):
        move = connection.execute("SELECT * FROM waste_moves WHERE move_id=?",
                                  (move_id,)).fetchone()
        if move is None:
            raise NotFoundError("移交不存在")
        return move

    def _settle_confirm_obligations(self, connection, move_id: str, now: str) -> None:
        connection.execute(
            "UPDATE waste_obligations SET status='fulfilled',fulfilled_at=?,fulfilled_move_id=? "
            "WHERE created_move_id=? AND kind='handover_confirm' AND status='open'",
            (now, move_id, move_id),
        )

    def _decide_handover(self, *, request_id: str, actor_id: str, move_id: str,
                         decision: str, reason: str | None,
                         callback_token: str | None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "move_id": move_id, "decision": decision,
                   "reason": reason, "callback_token": callback_token}
        action = f"waste_{decision}_handover"
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, action, payload)
            if replayed is not None:
                return replayed
            move = self._load_proposed_move(connection, move_id)
            shipment_id = move["shipment_id"]
            self._shipment(connection, shipment_id)
            receiver = self._custodian(connection, move["to_custodian_id"])
            if callback_token is not None and move["callback_token"] is not None \
                    and callback_token != move["callback_token"]:
                raise ValidationError("回调标识与移交单不匹配")
            already_decided = move["status"] != "proposed"
            if not already_decided:
                # 首次判定必须由接收方组织内、且不同于提出方的责任人完成。
                self._require_custodian_scope(actor, receiver)
                if actor.actor_id == move["proposed_by"]:
                    raise PermissionDenied("移交必须由不同于提出方的接收方责任人确认，不能自行确认")
            now = self._now_text()
            container_rows = connection.execute(
                "SELECT * FROM waste_move_items WHERE move_id=?", (move_id,)
            ).fetchall()
            container_ids = [row["container_id"] for row in container_rows]
            decision_items = [entry["item_id"] for link in container_rows
                              for entry in json.loads(link["manifest_json"])["items"]]

            def create():
                if already_decided:
                    # 重复回调：核销为同结果回放，不产生新事件、不改既有责任。
                    if move["status"] != decision:
                        raise ConflictError(
                            f"移交已结束为 {move['status']}，不能再记为 {decision}")
                    return ("waste_move", move_id,
                            {"move_id": move_id, "status": move["status"],
                             "replayed_existing": True})
                if decision == "rejected":
                    if not reason:
                        raise ValidationError("接收拒绝必须填写原因")
                    connection.execute(
                        "UPDATE waste_moves SET status='rejected',decided_at=?,decided_by=?,reason=? "
                        "WHERE move_id=?",
                        (now, actor_id, reason, move_id),
                    )
                    self._settle_confirm_obligations(connection, move_id, now)
                    self._append_event(
                        connection, event_type="handover.rejected", shipment_id=shipment_id,
                        payload={"move_id": move_id, "kind": move["kind"],
                                 "from_custodian_id": move["from_custodian_id"],
                                 "to_custodian_id": move["to_custodian_id"],
                                 "reason": reason},
                        actor_id=actor_id, containers=container_ids,
                        items=decision_items)
                    return ("waste_move", move_id,
                            {"move_id": move_id, "status": "rejected"})

                # 确认前再次按冻结快照校验资格与容器状态。
                now_dt = self._now()
                next_obligations: list[tuple[str, str]] = []
                for link in container_rows:
                    container = self._container_row(connection, link["container_id"])
                    if container["current_custodian_id"] != move["from_custodian_id"]:
                        raise ConflictError(
                            f"容器 {container['container_id']} 已不在交出方，不能确认移交")
                    if container["status"] not in STAGE_BEFORE_HANDOVER[move["kind"]]:
                        raise ConflictError(
                            f"容器 {container['container_id']} 状态已变化，不能确认移交")
                    snapshot = json.loads(container["regulation_snapshot_json"])
                    missing = missing_qualifications(
                        move["kind"], snapshot, json.loads(receiver["qualifications_json"]))
                    if missing:
                        raise PermissionDenied(f"接收方资格已不满足：{canonical_json(missing)}")
                    next_obligations.append((link["container_id"],
                                             container["regulation_snapshot_json"]))
                connection.execute(
                    "UPDATE waste_moves SET status='confirmed',decided_at=?,decided_by=? "
                    "WHERE move_id=?",
                    (now, actor_id, move_id),
                )
                self._settle_confirm_obligations(connection, move_id, now)
                new_stage = STAGE_AFTER_CONFIRM[move["kind"]]
                obligation_kind = NEXT_OBLIGATION[move["kind"]]
                for container_id, snapshot_json in next_obligations:
                    # 离开旧阶段即核销旧阶段义务：staged→staging、in_transit→transit。
                    prior_container = self._container_row(connection, container_id)
                    leaving_kind = {"staged": "staging",
                                    "in_transit": "transit"}.get(prior_container["status"])
                    if leaving_kind is not None:
                        connection.execute(
                            "UPDATE waste_obligations SET status='fulfilled',fulfilled_at=?,"
                            "fulfilled_move_id=? WHERE container_id=? AND kind=? AND status='open'",
                            (now, move_id, container_id, leaving_kind))
                    connection.execute(
                        "UPDATE waste_containers SET current_custodian_id=?,status=? "
                        "WHERE container_id=?",
                        (move["to_custodian_id"], new_stage, container_id),
                    )
                    # 同阶段跨营地（staged→staged）：未结义务改派给新持有方。
                    if prior_container["status"] == new_stage:
                        connection.execute(
                            "UPDATE waste_obligations SET responsible_custodian_id=? "
                            "WHERE container_id=? AND status='open' "
                            "AND responsible_custodian_id=?",
                            (move["to_custodian_id"], container_id,
                             move["from_custodian_id"]))
                    snapshot = json.loads(snapshot_json)
                    due = deadline_for(obligation_kind, snapshot, now_dt)
                    if due is not None:
                        connection.execute(
                            "INSERT INTO waste_obligations(obligation_id,shipment_id,container_id,"
                            "kind,responsible_custodian_id,due_at,status,created_at,created_move_id) "
                            "VALUES(?,?,?,?,?,?, 'open',?,?)",
                            (uuid.uuid4().hex, shipment_id, container_id, obligation_kind,
                             move["to_custodian_id"],
                             due.isoformat().replace("+00:00", "Z"), now, move_id),
                        )
                self._append_event(
                    connection, event_type=f"handover.{JUMP_ACTIONS[move['kind']]}",
                    shipment_id=shipment_id,
                    payload={"move_id": move_id, "kind": move["kind"],
                             "from_custodian_id": move["from_custodian_id"],
                             "to_custodian_id": move["to_custodian_id"],
                             "new_stage": new_stage},
                    actor_id=actor_id, containers=container_ids,
                    items=decision_items)
                return ("waste_move", move_id,
                        {"move_id": move_id, "status": "confirmed"})

            return self._commit_idempotent(connection, request_id=request_id, action=action,
                                           payload=payload, create=create)

    def confirm_handover(self, *, request_id: str, actor_id: str, move_id: str,
                         callback_token: str | None = None) -> WriteReceipt:
        """接收方确认移交，责任与期限义务同时转移。"""

        return self._decide_handover(request_id=request_id, actor_id=actor_id, move_id=move_id,
                                     decision="confirmed", reason=None,
                                     callback_token=callback_token)

    def reject_handover(self, *, request_id: str, actor_id: str, move_id: str,
                        reason: str, callback_token: str | None = None) -> WriteReceipt:
        """接收方拒绝接收；容器责任保留在交出方，拒绝原因留痕。"""

        return self._decide_handover(request_id=request_id, actor_id=actor_id, move_id=move_id,
                                     decision="rejected", reason=reason,
                                     callback_token=callback_token)

    def cancel_handover(self, *, request_id: str, actor_id: str, move_id: str,
                        reason: str) -> WriteReceipt:
        """取消尚未确认的运输（如航班取消）；已确认移交不能撤销，只能办回程。"""

        payload = {"actor_id": actor_id, "move_id": move_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, "waste_cancel_handover", payload)
            if replayed is not None:
                return replayed
            move = self._load_proposed_move(connection, move_id)
            shipment_id = move["shipment_id"]
            self._shipment(connection, shipment_id)
            sender = self._custodian(connection, move["from_custodian_id"])
            self._require_custodian_scope(actor, sender)
            now = self._now_text()
            link_rows = connection.execute(
                "SELECT container_id, manifest_json FROM waste_move_items WHERE move_id=?",
                (move_id,)).fetchall()
            container_ids = [row["container_id"] for row in link_rows]
            cancelled_items = [entry["item_id"] for link in link_rows
                               for entry in json.loads(link["manifest_json"])["items"]]

            def create():
                if move["status"] != "proposed":
                    raise ConflictError("只有待确认的移交可以取消；已完成责任请改用回程移交")
                connection.execute(
                    "UPDATE waste_moves SET status='cancelled',decided_at=?,decided_by=?,reason=? "
                    "WHERE move_id=?",
                    (now, actor_id, reason, move_id),
                )
                self._settle_confirm_obligations(connection, move_id, now)
                self._append_event(
                    connection, event_type="handover.cancelled", shipment_id=shipment_id,
                    payload={"move_id": move_id, "kind": move["kind"], "reason": reason,
                             "from_custodian_id": move["from_custodian_id"],
                             "to_custodian_id": move["to_custodian_id"]},
                    actor_id=actor_id, containers=container_ids,
                    items=cancelled_items)
                return ("waste_move", move_id,
                        {"move_id": move_id, "status": "cancelled"})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_cancel_handover",
                                           payload=payload, create=create)

    # ------------------------------------------------------- 破损/修正/重装

    def _apply_reversal(self, connection, *, shipment_id, item_id, delta_kg, reason_type,
                        detail, actor_id) -> dict[str, Any]:
        """对一个批次追加冲销量并更新当前投影；不改动任何历史事件。

        delta_kg 为正表示冲减（破损、短量、拒收），为负表示补记差异；无论正负
        都新增 ``item.quantity_reversed`` 事件，产生与封箱事实保持不变。
        """

        item = connection.execute("SELECT * FROM waste_items WHERE item_id=?",
                                  (item_id,)).fetchone()
        if item is None:
            raise NotFoundError(f"废弃物批次 {item_id} 不存在")
        if item["shipment_id"] != shipment_id:
            raise ValidationError("批次不属于本项目")
        if item["status"] == "disposed":
            raise ConflictError("批次已处置关闭，不能再冲销")
        before = item["current_kg"]
        after = round(before - delta_kg, 6)
        if after < 0:
            raise ValidationError(
                f"批次 {item_id} 冲销 {delta_kg}kg 超过在管量 {before}kg")
        connection.execute("UPDATE waste_items SET current_kg=? WHERE item_id=?",
                           (after, item_id))
        self._append_event(
            connection, event_type="item.quantity_reversed", shipment_id=shipment_id,
            payload={"item_id": item_id, "waste_type": item["waste_type"],
                     "before_kg": before, "after_kg": after, "delta_kg": delta_kg,
                     "reason_type": reason_type, **detail},
            actor_id=actor_id,
            containers=[item["current_container_id"]] if item["current_container_id"] else [],
            items=[item_id])
        return {"item_id": item_id, "before_kg": before, "after_kg": after, "delta_kg": delta_kg}

    def record_damage(self, *, request_id: str, actor_id: str, shipment_id: str,
                      container_id: str, severity: str, description: str,
                      losses: list[dict[str, float]] | None = None) -> WriteReceipt:
        """登记容器破损；损失通过冲销事件处理，破损箱须重装后才能继续移交。"""

        payload = {"actor_id": actor_id, "shipment_id": shipment_id,
                   "container_id": container_id, "severity": severity,
                   "description": description, "losses": losses or []}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, "waste_record_damage", payload)
            if replayed is not None:
                return replayed
            self._shipment(connection, shipment_id)
            container = self._container_row(connection, container_id)
            if container["status"] in ("repacked", "disposed"):
                raise ConflictError("该容器已封存或处置，不能再登记破损")
            holder = self._custodian(connection, container["current_custodian_id"])
            self._require_custodian_scope(actor, holder)
            if severity not in DAMAGE_SEVERITIES:
                raise ValidationError("severity 必须是 minor/breached/destroyed")
            description = str(description).strip()
            if not description:
                raise ValidationError("破损情况描述不能为空")
            normalized_losses: list[dict[str, Any]] = []
            held_item_ids = {item["item_id"] for item in
                             self._container_items(connection, container_id)}
            for entry in losses or []:
                item_id = entry.get("item_id")
                if not item_id:
                    raise ValidationError("损失明细必须包含 item_id")
                if item_id not in held_item_ids:
                    raise ValidationError(f"批次 {item_id} 不在容器 {container_id} 内")
                lost_kg = self._kg(entry.get("lost_kg"), "lost_kg")
                normalized_losses.append({"item_id": item_id, "lost_kg": lost_kg})

            def create():
                results = []
                for entry in normalized_losses:
                    results.append(self._apply_reversal(
                        connection, shipment_id=shipment_id, item_id=entry["item_id"],
                        delta_kg=entry["lost_kg"], reason_type="damage",
                        detail={"container_id": container_id, "severity": severity,
                                "description": description},
                        actor_id=actor_id))
                disabled = severity in ("breached", "destroyed")
                if disabled and container["status"] not in ("damaged", "repacked", "disposed"):
                    connection.execute(
                        "UPDATE waste_containers SET status='damaged',prev_status=? "
                        "WHERE container_id=?",
                        (container["status"], container_id),
                    )
                self._append_event(
                    connection, event_type="container.damaged", shipment_id=shipment_id,
                    payload={"container_id": container_id, "severity": severity,
                             "description": description, "losses": results,
                             "container_disabled": disabled},
                    actor_id=actor_id, containers=[container_id],
                    items=[entry["item_id"] for entry in normalized_losses])
                return ("waste_container", container_id,
                        {"container_id": container_id, "damaged": disabled,
                         "losses": results})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_record_damage",
                                           payload=payload, create=create)

    def correct_quantity(self, *, request_id: str, actor_id: str, shipment_id: str,
                         item_id: str, new_kg: float, reason: str) -> WriteReceipt:
        """盘点数量修正：以冲销事件记录差异，不重写产生和封箱事实。"""

        payload = {"actor_id": actor_id, "shipment_id": shipment_id, "item_id": item_id,
                   "new_kg": new_kg, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, "waste_correct_quantity", payload)
            if replayed is not None:
                return replayed
            self._shipment(connection, shipment_id)
            item = connection.execute("SELECT * FROM waste_items WHERE item_id=?",
                                      (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("废弃物批次不存在")
            if item["current_container_id"]:
                container = self._container_row(connection, item["current_container_id"])
                if container["status"] in ("repacked", "disposed"):
                    raise ConflictError("容器已封存或处置，不能修正其中批次")
                holder = self._custodian(connection, container["current_custodian_id"])
            else:
                holder = self._custodian(connection, item["origin_custodian_id"])
            self._require_custodian_scope(actor, holder)
            new_kg = self._kg(new_kg, "new_kg", allow_zero=True)
            reason = str(reason).strip()
            if not reason:
                raise ValidationError("修正原因不能为空")
            delta = round(item["current_kg"] - new_kg, 6)

            def create():
                if delta == 0:
                    raise ValidationError("修正数量与当前数量一致，无需冲销")
                result = self._apply_reversal(
                    connection, shipment_id=shipment_id, item_id=item_id,
                    delta_kg=delta, reason_type="count_correction",
                    detail={"reason": reason}, actor_id=actor_id)
                return ("waste_item", item_id, result)

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_correct_quantity",
                                           payload=payload, create=create)

    def repack_container(self, *, request_id: str, actor_id: str, shipment_id: str,
                         source_container_id: str, new_container_id: str,
                         packaging: str, gross_kg: float,
                         item_ids: list[str] | None = None,
                         regulation_id: str | None = None) -> WriteReceipt:
        """拆箱重装为新容器；旧箱封存为 repacked，责任与未结义务转移到新箱。"""

        payload = {"actor_id": actor_id, "shipment_id": shipment_id,
                   "source_container_id": source_container_id,
                   "new_container_id": new_container_id, "packaging": packaging,
                   "gross_kg": gross_kg, "item_ids": item_ids,
                   "regulation_id": regulation_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, "waste_repack_container", payload)
            if replayed is not None:
                return replayed
            self._shipment(connection, shipment_id)
            source = self._container_row(connection, source_container_id)
            holder = self._custodian(connection, source["current_custodian_id"])
            self._require_custodian_scope(actor, holder)
            if source["status"] not in ("sealed", "staged", "damaged"):
                raise ConflictError(f"容器状态 {source['status']} 不允许拆箱重装")
            current_items = self._container_items(connection, source_container_id)
            if not current_items:
                raise ConflictError("旧箱内没有可重装的批次")
            available = {item["item_id"] for item in current_items}
            chosen = item_ids or [item["item_id"] for item in current_items]
            if any(chosen_id not in available for chosen_id in chosen):
                raise ValidationError("重装清单必须全部来自旧箱内的批次")
            chosen_set = set(chosen)
            regulation_id = regulation_id or source["regulation_id"]
            regulation = self._regulation(connection, regulation_id)
            snapshot = self._regulation_snapshot(regulation)
            packaging = str(packaging).strip()
            if not packaging:
                raise ValidationError("packaging 不能为空")
            gross_kg = self._kg(gross_kg, "gross_kg")
            repack_items = [item for item in current_items if item["item_id"] in chosen_set]
            waste_types = [item["waste_type"] for item in repack_items]
            violations = find_incompatible_pairs(waste_types, snapshot)
            if violations:
                raise ValidationError(f"重装仍然违反混装限制：{canonical_json(violations)}")
            net_kg = round(sum(item["current_kg"] for item in repack_items), 6)
            cap = max_total_kg(snapshot)
            if cap is not None and net_kg > cap:
                raise ValidationError("重装净重超过法规上限")
            if gross_kg < net_kg:
                raise ValidationError("新箱毛重不能小于净重")
            now_text = self._now_text()
            # 新箱继承旧箱业务阶段；受损箱恢复受损前阶段，从中断环节继续办理。
            inherited_stage = source["prev_status"] if source["status"] == "damaged" \
                else source["status"]

            def create():
                if connection.execute("SELECT 1 FROM waste_containers WHERE container_id=?",
                                      (new_container_id,)).fetchone():
                    raise ConflictError("新容器编号已经存在")
                leftovers = [item["item_id"] for item in current_items
                             if item["item_id"] not in chosen_set]
                connection.execute(
                    "INSERT INTO waste_containers(container_id,shipment_id,origin_custodian_id,"
                    "current_custodian_id,packaging,gross_kg,regulation_id,regulation_hash,"
                    "regulation_snapshot_json,status,sealed_at,sealed_by,sealed_event_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?, ?,?,?,?)",
                    (new_container_id, shipment_id, source["origin_custodian_id"],
                     source["current_custodian_id"], packaging, gross_kg, regulation_id,
                     regulation["content_hash"], canonical_json(snapshot),
                     inherited_stage, now_text, actor_id, "(pending)"),
                )
                composition = [{"item_id": item["item_id"], "waste_type": item["waste_type"],
                                "kg": item["current_kg"]} for item in repack_items]
                event = self._append_event(
                    connection, event_type="container.repacked", shipment_id=shipment_id,
                    payload={"source_container_id": source_container_id,
                             "new_container_id": new_container_id,
                             "packaging": packaging, "gross_kg": gross_kg,
                             "net_kg": net_kg, "regulation_id": regulation_id,
                             "regulation_hash": regulation["content_hash"],
                             "frozen_regulation_changed": regulation_id != source["regulation_id"],
                             "composition": composition, "leftover_item_ids": leftovers},
                    actor_id=actor_id,
                    containers=[source_container_id, new_container_id],
                    items=[item["item_id"] for item in repack_items])
                connection.execute(
                    "UPDATE waste_containers SET sealed_event_id=? "
                    "WHERE container_id=?",
                    (event.event_id, new_container_id),
                )
                # 全部批次迁出才把旧箱封存；部分重装时旧箱保留原状态继续管理剩余批次。
                if not leftovers:
                    connection.execute(
                        "UPDATE waste_containers SET status='repacked' WHERE container_id=?",
                        (source_container_id,),
                    )
                for item in repack_items:
                    connection.execute(
                        "UPDATE waste_items SET current_container_id=? WHERE item_id=?",
                        (new_container_id, item["item_id"]),
                    )
                    connection.execute(
                        "DELETE FROM waste_container_items WHERE item_id=?",
                        (item["item_id"],),
                    )
                    connection.execute(
                        "INSERT INTO waste_container_items(container_id,item_id) VALUES(?,?)",
                        (new_container_id, item["item_id"]),
                    )
                # 全部批次迁出时未结义务跟随新箱；部分重装时旧箱仍有责任，
                # 义务在两个箱子上各自保留。
                if leftovers:
                    existing = connection.execute(
                        "SELECT DISTINCT kind,responsible_custodian_id,due_at,created_at "
                        "FROM waste_obligations WHERE container_id=? AND status='open'",
                        (source_container_id,)).fetchall()
                    for row in existing:
                        connection.execute(
                            "INSERT INTO waste_obligations(obligation_id,shipment_id,container_id,"
                            "kind,responsible_custodian_id,due_at,status,created_at) "
                            "VALUES(?,?,?,?,?, 'open',?)",
                            (uuid.uuid4().hex, shipment_id, new_container_id, row["kind"],
                             row["responsible_custodian_id"], row["due_at"], row["created_at"]),
                        )
                else:
                    connection.execute(
                        "UPDATE waste_obligations SET container_id=? "
                        "WHERE container_id=? AND status='open'",
                        (new_container_id, source_container_id),
                    )
                return ("waste_container", new_container_id,
                        {"new_container_id": new_container_id,
                         "source_container_id": source_container_id})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_repack_container",
                                           payload=payload, create=create)

    # ------------------------------------------------------------- 处置关闭

    def record_disposal(self, *, request_id: str, actor_id: str, shipment_id: str,
                        container_ids: list[str], certificate_no: str,
                        disposal_method: str,
                        disposed_kg: dict[str, float] | None = None,
                        evidence_ref: str | None = None) -> WriteReceipt:
        """登记目的地处置凭证；全部批次处置完毕才关闭项目责任。"""

        payload = {"actor_id": actor_id, "shipment_id": shipment_id,
                   "container_ids": list(container_ids), "certificate_no": certificate_no,
                   "disposal_method": disposal_method, "disposed_kg": disposed_kg,
                   "evidence_ref": evidence_ref}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            replayed = self._replay_receipt(connection, request_id, "waste_record_disposal", payload)
            if replayed is not None:
                return replayed
            self._shipment(connection, shipment_id)
            if not container_ids:
                raise ValidationError("处置凭证至少对应一个容器")
            certificate_no = str(certificate_no).strip()
            disposal_method = str(disposal_method).strip()
            if not certificate_no or not disposal_method:
                raise ValidationError("凭证编号和处置方式不能为空")
            facility = None
            resolved: list[dict[str, Any]] = []
            zero_items: list[str] = []
            for container_id in container_ids:
                container = self._container_row(connection, container_id)
                if container["shipment_id"] != shipment_id:
                    raise ValidationError(f"容器 {container_id} 不属于本项目")
                if container["status"] not in ("delivered", "disposed"):
                    raise ConflictError(
                        f"容器 {container_id} 尚未完成目的地接收，不能登记处置")
                holder = self._custodian(connection, container["current_custodian_id"])
                if holder["kind"] != "disposal_facility":
                    raise PermissionDenied("只有目的地处置设施可以登记处置凭证")
                if facility is None:
                    facility = holder
                elif holder["custodian_id"] != facility["custodian_id"]:
                    raise ValidationError("一张处置凭证不能同时覆盖不同设施持有的容器")
                self._require_custodian_scope(actor, holder)
                items = self._container_items(connection, container_id)
                declared = disposed_kg or {}
                for item in items:
                    if item["current_kg"] <= 1e-9:
                        zero_items.append(item["item_id"])
                        continue
                    amount = float(declared.get(item["item_id"], item["current_kg"]))
                    if amount <= 0 or amount > item["current_kg"] + 1e-9:
                        raise ValidationError(
                            f"批次 {item['item_id']} 处置量 {amount} 超过在管量")
                    resolved.append({"container_id": container_id, "item_id": item["item_id"],
                                     "waste_type": item["waste_type"], "kg": amount})
            now_text = self._now_text()

            def create():
                for entry in resolved:
                    connection.execute(
                        "UPDATE waste_items SET current_kg=ROUND(current_kg-?,6) WHERE item_id=?",
                        (entry["kg"], entry["item_id"]),
                    )
                # 此前已被冲销归零的批次随处置凭证一并核销关闭。
                for item_id in zero_items:
                    connection.execute(
                        "UPDATE waste_items SET status='disposed' WHERE item_id=? "
                        "AND status!='disposed'",
                        (item_id,))
                self._append_event(
                    connection, event_type="disposal.certified", shipment_id=shipment_id,
                    payload={"certificate_no": certificate_no,
                             "disposal_method": disposal_method, "evidence_ref": evidence_ref,
                             "facility_custodian_id": facility["custodian_id"],
                             "entries": resolved, "zeroed_item_ids": zero_items},
                    actor_id=actor_id, containers=list(container_ids),
                    items=[entry["item_id"] for entry in resolved] + zero_items)
                finished_containers: list[str] = []
                for container_id in container_ids:
                    remaining = connection.execute(
                        "SELECT COALESCE(SUM(current_kg),0) AS kg, COUNT(*) AS count "
                        "FROM waste_items WHERE current_container_id=?",
                        (container_id,)).fetchone()
                    if remaining["count"] > 0 and remaining["kg"] <= 1e-9:
                        connection.execute(
                            "UPDATE waste_items SET status='disposed',current_kg=0 "
                            "WHERE current_container_id=? AND status!='disposed'",
                            (container_id,))
                        connection.execute(
                            "UPDATE waste_containers SET status='disposed' WHERE container_id=?",
                            (container_id,))
                        connection.execute(
                            "UPDATE waste_obligations SET status='fulfilled',fulfilled_at=?,"
                            "fulfilled_move_id=? WHERE container_id=? AND status='open'",
                            (now_text, f"disposal:{certificate_no}", container_id))
                        finished_containers.append(container_id)
                totals = connection.execute(
                    "SELECT COUNT(*) AS total, SUM(CASE WHEN status='disposed' THEN 1 ELSE 0 END) "
                    "AS done FROM waste_items WHERE shipment_id=?", (shipment_id,)).fetchone()
                open_obligations = connection.execute(
                    "SELECT COUNT(*) AS count FROM waste_obligations WHERE shipment_id=? "
                    "AND status='open'", (shipment_id,)).fetchone()["count"]
                closed = False
                if totals["total"] > 0 and totals["total"] == totals["done"] \
                        and open_obligations == 0:
                    connection.execute(
                        "UPDATE waste_shipments SET status='closed',closed_at=?,closed_by=? "
                        "WHERE shipment_id=?",
                        (now_text, actor_id, shipment_id))
                    self._append_event(
                        connection, event_type="shipment.closed", shipment_id=shipment_id,
                        payload={"certificate_no": certificate_no}, actor_id=actor_id)
                    closed = True
                return ("waste_disposal", certificate_no,
                        {"certificate_no": certificate_no,
                         "finished_containers": finished_containers, "shipment_closed": closed})

            return self._commit_idempotent(connection, request_id=request_id,
                                           action="waste_record_disposal",
                                           payload=payload, create=create)

    # ------------------------------------------------------------------ 查询

    def get_shipment(self, shipment_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM waste_shipments WHERE shipment_id=?", (shipment_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return dict(row)

    def get_regulation(self, regulation_id: str) -> Regulation:
        row = self.database.connection.execute(
            "SELECT * FROM waste_regulations WHERE regulation_id=?", (regulation_id,)).fetchone()
        if row is None:
            raise NotFoundError("法规版本不存在")
        return Regulation(row["regulation_id"], row["version_label"],
                          json.loads(row["content_json"]), row["content_hash"],
                          row["effective_from"], row["created_by"], row["created_at"])

    def get_custodian(self, custodian_id: str) -> Custodian:
        row = self.database.connection.execute(
            "SELECT * FROM waste_custodians WHERE custodian_id=?", (custodian_id,)).fetchone()
        if row is None:
            raise NotFoundError("责任主体不存在")
        return Custodian(row["custodian_id"], row["organization_id"], row["site_id"],
                         row["kind"], row["name"],
                         json.loads(row["qualifications_json"]),
                         bool(row["active"]))

    def get_item(self, item_id: str) -> WasteItem:
        row = self.database.connection.execute(
            "SELECT * FROM waste_items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("废弃物批次不存在")
        return WasteItem(row["item_id"], row["shipment_id"], row["waste_type"],
                        row["initial_kg"], row["current_kg"], row["origin_custodian_id"],
                        row["status"], row["current_container_id"],
                        row["generated_at"], row["generated_by"])

    def get_container(self, container_id: str) -> ContainerView:
        row = self.database.connection.execute(
            "SELECT * FROM waste_containers WHERE container_id=?", (container_id,)).fetchone()
        if row is None:
            raise NotFoundError("容器不存在")
        items = [r["item_id"] for r in self.database.connection.execute(
            "SELECT item_id FROM waste_container_items WHERE container_id=? ORDER BY item_id",
            (container_id,))]
        return ContainerView(row["container_id"], row["shipment_id"], row["origin_custodian_id"],
                             row["current_custodian_id"], row["packaging"], row["gross_kg"],
                             row["regulation_id"], row["regulation_hash"],
                             json.loads(row["regulation_snapshot_json"]), row["status"],
                             row["sealed_at"], row["sealed_by"], items)

    def shipment_events(self, shipment_id: str) -> list[WasteEvent]:
        """正向溯源：项目从产生到关闭的完整事件链。"""

        rows = self.database.connection.execute(
            "SELECT * FROM waste_events WHERE shipment_id=? ORDER BY sequence", (shipment_id,))
        return [WasteEvent(r["sequence"], r["event_id"], r["shipment_id"], r["event_type"],
                           json.loads(r["payload_json"]), r["actor_id"], r["occurred_at"])
                for r in rows]

    def item_events(self, item_id: str) -> list[WasteEvent]:
        """反查一个批次经过的全部箱子、移交与处置。"""

        rows = self.database.connection.execute(
            "SELECT e.* FROM waste_events e JOIN waste_event_refs r ON e.event_id=r.event_id "
            "WHERE r.item_id=? ORDER BY e.sequence", (item_id,))
        return [WasteEvent(r["sequence"], r["event_id"], r["shipment_id"], r["event_type"],
                           json.loads(r["payload_json"]), r["actor_id"], r["occurred_at"])
                for r in rows]

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        """从一个箱子反查全部组成：当前批次、封箱净重与重装谱系。"""

        container = self.get_container(container_id)
        lineage: list[dict[str, Any]] = []
        # 向旧箱方向追溯。
        current = container_id
        visited = {current}
        while True:
            row = self.database.connection.execute(
                "SELECT payload_json FROM waste_events WHERE shipment_id=? AND "
                "event_type='container.repacked' AND "
                "json_extract(payload_json,'$.new_container_id')=? ORDER BY sequence LIMIT 1",
                (container.shipment_id, current)).fetchone()
            if row is None:
                break
            payload = json.loads(row["payload_json"])
            lineage.append(payload)
            current = payload["source_container_id"]
            if current in visited:
                break
            visited.add(current)
        lineage.reverse()
        # 向新箱方向追溯。
        current = container_id
        while True:
            row = self.database.connection.execute(
                "SELECT payload_json FROM waste_events WHERE shipment_id=? AND "
                "event_type='container.repacked' AND "
                "json_extract(payload_json,'$.source_container_id')=? ORDER BY sequence LIMIT 1",
                (container.shipment_id, current)).fetchone()
            if row is None:
                break
            payload = json.loads(row["payload_json"])
            if payload["new_container_id"] in visited:
                break
            visited.add(payload["new_container_id"])
            lineage.append(payload)
            current = payload["new_container_id"]
        current_items = [dict(r) for r in self.database.connection.execute(
            "SELECT item_id,waste_type,initial_kg,current_kg FROM waste_items "
            "WHERE current_container_id=? ORDER BY item_id", (container_id,))]
        seal = self.database.connection.execute(
            "SELECT payload_json FROM waste_events WHERE shipment_id=? AND event_type IN "
            "('container.sealed','container.repacked') AND "
            "(json_extract(payload_json,'$.container_id')=? "
            "OR json_extract(payload_json,'$.new_container_id')=?) ORDER BY sequence LIMIT 1",
            (container.shipment_id, container_id, container_id)).fetchone()
        sealed_composition = json.loads(seal["payload_json"]) if seal else {}
        events = [
            {"sequence": r["sequence"], "event_type": r["event_type"],
             "occurred_at": r["occurred_at"], "payload": json.loads(r["payload_json"])}
            for r in self.database.connection.execute(
                "SELECT e.* FROM waste_events e JOIN waste_event_refs r ON e.event_id=r.event_id "
                "WHERE r.container_id=? ORDER BY e.sequence", (container_id,))
        ]
        return {"container": container.__dict__, "current_items": current_items,
                "sealed_composition": sealed_composition, "lineage": lineage, "events": events}

    def current_custodianship(self, shipment_id: str) -> list[dict[str, Any]]:
        """协调员视图：每个容器的当前责任方与所处阶段。"""

        rows = self.database.connection.execute(
            "SELECT container_id,current_custodian_id,status,regulation_hash,gross_kg "
            "FROM waste_containers WHERE shipment_id=? ORDER BY container_id", (shipment_id,))
        result = []
        for row in rows:
            holder = self.get_custodian(row["current_custodian_id"])
            result.append({"container_id": row["container_id"], "status": row["status"],
                           "current_custodian_id": holder.custodian_id,
                           "current_custodian_name": holder.name, "kind": holder.kind,
                           "gross_kg": row["gross_kg"],
                           "regulation_hash": row["regulation_hash"][:12]})
        return result

    def open_obligations(self, shipment_id: str | None = None) -> list[dict[str, Any]]:
        query = ("SELECT o.*, c.name AS custodian_name FROM waste_obligations o "
                 "JOIN waste_custodians c ON c.custodian_id=o.responsible_custodian_id "
                 "WHERE o.status='open'")
        params: list[Any] = []
        if shipment_id:
            query += " AND o.shipment_id=?"
            params.append(shipment_id)
        query += " ORDER BY o.due_at"
        return [dict(row) for row in self.database.connection.execute(query, params)]

    def overdue(self, shipment_id: str | None = None, at: datetime | None = None) -> list[dict[str, Any]]:
        """逾期节点：截止时间已过但仍未履行的责任。"""

        moment = (at or self._now()).isoformat().replace("+00:00", "Z")
        query = ("SELECT o.*, c.name AS custodian_name FROM waste_obligations o "
                 "JOIN waste_custodians c ON c.custodian_id=o.responsible_custodian_id "
                 "WHERE o.status='open' AND o.due_at < ?")
        params: list[Any] = [moment]
        if shipment_id:
            query += " AND o.shipment_id=?"
            params.append(shipment_id)
        query += " ORDER BY o.due_at"
        return [dict(row) for row in self.database.connection.execute(query, params)]

    def quantity_variances(self, shipment_id: str) -> dict[str, Any]:
        """数量差异：批次冲销、箱净重差异，以及已确认移交清单相对在管量的短少。"""

        item_rows = self.database.connection.execute(
            "SELECT * FROM waste_items WHERE shipment_id=? ORDER BY item_id", (shipment_id,))
        items = []
        for row in item_rows:
            reversals = [
                {"event_type": r["event_type"], "occurred_at": r["occurred_at"],
                 "payload": json.loads(r["payload_json"])}
                for r in self.database.connection.execute(
                    "SELECT e.* FROM waste_events e JOIN waste_event_refs ref "
                    "ON e.event_id=ref.event_id WHERE ref.item_id=? "
                    "AND e.event_type='item.quantity_reversed' ORDER BY e.sequence",
                    (row["item_id"],))
            ]
            items.append({"item_id": row["item_id"], "waste_type": row["waste_type"],
                          "initial_kg": row["initial_kg"], "current_kg": row["current_kg"],
                          "variance_kg": round(row["initial_kg"] - row["current_kg"], 6),
                          "reversals": reversals})
        containers = []
        for row in self.database.connection.execute(
                "SELECT * FROM waste_containers WHERE shipment_id=? ORDER BY container_id",
                (shipment_id,)):
            current_net = self.database.connection.execute(
                "SELECT COALESCE(SUM(current_kg),0) AS kg FROM waste_items "
                "WHERE current_container_id=?", (row["container_id"],)).fetchone()["kg"]
            seal_event = self.database.connection.execute(
                "SELECT payload_json FROM waste_events WHERE shipment_id=? AND event_type IN "
                "('container.sealed','container.repacked') AND "
                "(json_extract(payload_json,'$.container_id')=? "
                "OR json_extract(payload_json,'$.new_container_id')=?) ORDER BY sequence LIMIT 1",
                (shipment_id, row["container_id"], row["container_id"])).fetchone()
            sealed_net = json.loads(seal_event["payload_json"]).get("net_kg", 0.0) \
                if seal_event else 0.0
            # 已重装旧箱的净重减少来自批次迁出（见 lineage），不是数量差异。
            moved_out = row["status"] == "repacked"
            containers.append({"container_id": row["container_id"], "status": row["status"],
                               "sealed_net_kg": sealed_net, "current_net_kg": round(current_net, 6),
                               "variance_kg": None if moved_out
                               else round(sealed_net - current_net, 6)})
        manifest_shortfalls = []
        for move in self.database.connection.execute(
                "SELECT * FROM waste_moves WHERE shipment_id=? AND status='confirmed' ORDER BY proposed_at",
                (shipment_id,)):
            for link in self.database.connection.execute(
                    "SELECT * FROM waste_move_items WHERE move_id=?", (move["move_id"],)):
                manifest = json.loads(link["manifest_json"])
                for entry in manifest["items"]:
                    item = self.database.connection.execute(
                        "SELECT * FROM waste_items WHERE item_id=?",
                        (entry["item_id"],)).fetchone()
                    if item is None or item["status"] == "disposed":
                        # 处置凭证是关闭证据，处置掉的量不是未解释短少。
                        continue
                    current_kg = item["current_kg"]
                    if abs(current_kg - entry["kg"]) <= 1e-9:
                        continue
                    explained_rows = self.database.connection.execute(
                        "SELECT payload_json FROM waste_events e JOIN waste_event_refs ref "
                        "ON e.event_id=ref.event_id WHERE ref.item_id=? "
                        "AND e.event_type='item.quantity_reversed' AND e.occurred_at>=?",
                        (entry["item_id"], move["proposed_at"])).fetchall()
                    explained_kg = round(sum(
                        json.loads(row["payload_json"]).get("delta_kg", 0.0)
                        for row in explained_rows), 6)
                    shortfall_kg = round(entry["kg"] - current_kg, 6)
                    manifest_shortfalls.append(
                        {"move_id": move["move_id"], "kind": move["kind"],
                         "container_id": link["container_id"],
                         "item_id": entry["item_id"],
                         "manifest_kg": entry["kg"], "current_kg": current_kg,
                         "shortfall_kg": shortfall_kg,
                         "explained_by_reversals_kg": explained_kg,
                         "unexplained_kg": round(shortfall_kg - explained_kg, 6)})
        return {"items": items, "containers": containers,
                "manifest_shortfalls": manifest_shortfalls}

    def coordinator_dashboard(self, shipment_id: str) -> dict[str, Any]:
        """协调员总览：状态、当前责任方、逾期节点与数量差异（不止总重量）。"""

        shipment = self.get_shipment(shipment_id)
        return {
            "shipment": {"shipment_id": shipment_id, "title": shipment["title"],
                         "status": shipment["status"]},
            "custodianship": self.current_custodianship(shipment_id),
            "open_obligations": self.open_obligations(shipment_id),
            "overdue": self.overdue(shipment_id),
            "variances": self.quantity_variances(shipment_id),
        }
