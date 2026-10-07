"""废弃物回运责任项目的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Regulation:
    """一个法规版本；内容在封箱时被快照冻结。"""

    regulation_id: str
    version_label: str
    content: dict[str, Any]
    content_hash: str
    effective_from: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Custodian:
    """责任主体：营地、暂存场、承运方或处置设施。"""

    custodian_id: str
    organization_id: str
    site_id: str | None
    kind: str
    name: str
    qualifications: list[str]
    active: bool


@dataclass(frozen=True)
class WasteItem:
    """一批在产生地登记分类的废弃物（追踪的最小责任单元）。"""

    item_id: str
    shipment_id: str
    waste_type: str
    initial_kg: float
    current_kg: float
    origin_custodian_id: str
    status: str
    current_container_id: str | None
    generated_at: str
    generated_by: str


@dataclass(frozen=True)
class WasteEvent:
    """追加式责任台账上的一个不可变事件。"""

    sequence: int
    event_id: str
    shipment_id: str
    event_type: str
    payload: dict[str, Any]
    actor_id: str
    occurred_at: str


@dataclass(frozen=True)
class ContainerView:
    """一个回运箱的当前状态与冻结的封箱事实。"""

    container_id: str
    shipment_id: str
    origin_custodian_id: str
    current_custodian_id: str
    packaging: str
    gross_kg: float
    regulation_id: str
    regulation_hash: str
    regulation_snapshot: dict[str, Any]
    status: str
    sealed_at: str
    sealed_by: str
    items: list[str] = field(default_factory=list)
