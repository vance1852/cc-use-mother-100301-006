"""废弃物回运责任项目在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RegulationVersion:
    """封箱时可被冻结的一版法规要求。"""

    version_id: str
    payload: dict[str, Any]
    payload_hash: str
    effective_from: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WasteLot:
    """在产生地点登记的一批分类废弃物（许可、数量、初始封装）。"""

    lot_id: str
    site_id: str
    waste_category: str
    hazard_class: str
    quantity: float
    unit: str
    packaging: str
    permit_id: str
    generated_at: str
    generated_by: str
    created_at: str


@dataclass(frozen=True)
class ManifestEntry:
    """封箱清单中的一行：某个批次在本箱内的数量（封箱后不可改）。"""

    container_id: str
    lot_id: str
    quantity: float
    unit: str


@dataclass(frozen=True)
class WasteContainer:
    """一个回运箱及其封箱时冻结的法规快照。"""

    container_id: str
    origin_site_id: str
    status: str
    regulation_version_id: str
    regulation_snapshot: dict[str, Any]
    regulation_snapshot_hash: str
    gross_weight: float | None
    sealed_by: str
    sealed_at: str
    predecessor_container_ids: tuple[str, ...]
    closed_at: str | None
    closed_by: str | None


@dataclass(frozen=True)
class LineageEdge:
    """批次数量在箱与箱、箱与终点之间的一次定向流动。"""

    edge_id: int
    event_id: str
    lot_id: str
    from_container_id: str | None
    to_container_id: str | None
    quantity: float
    reason: str
    source_event_id: str | None
    acknowledged_at: str | None
    acknowledged_by: str | None


@dataclass(frozen=True)
class StorageStay:
    """一次暂存入库/出库责任记录。"""

    stay_id: str
    container_id: str
    site_id: str
    keeper: str
    handed_by: str
    checked_in_at: str
    due_at: str
    checked_out_at: str | None
    released_by: str | None


@dataclass(frozen=True)
class CustodyTransfer:
    """一次 custody 移交（跨营地、承运、目的地、重装、退回）。"""

    transfer_id: str
    transfer_type: str
    custody_kind: str
    container_id: str
    from_party: str
    to_party: str
    from_site_id: str
    to_site_id: str | None
    status: str
    reversal_of: str | None
    proposed_by: str
    proposed_at: str
    confirm_due_at: str
    confirmed_by: str | None
    confirmed_at: str | None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DisposalCertificate:
    """目的地处置凭证：按批次数量关闭责任。"""

    certificate_id: str
    container_id: str
    facility_site_id: str
    certificate_ref: str
    items: tuple[dict[str, Any], ...]
    certified_by: str
    certified_at: str
    event_id: str


@dataclass(frozen=True)
class LotCorrection:
    """对登记数量的一次冲销修正（不改写原始登记）。"""

    event_id: str
    lot_id: str
    delta: float
    reason: str
    corrected_by: str
    created_at: str
