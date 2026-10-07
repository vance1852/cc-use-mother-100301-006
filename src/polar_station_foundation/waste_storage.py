"""废弃物回运责任项目的 SQLite 表结构。

设计分两层：

- ``waste_events`` / ``waste_event_refs`` 是只增不改的业务事件台账，容器和
  批次“发生过什么”只能追加；冲销、修正、拒收都产生新事件，不删除旧事实。
- 其余表是台账的当前投影（容器现状、批次位置、未结义务等），进程重启后
  依靠数据库中的既有状态继续办理，回调与重试通过 request_id 幂等核销。
"""

from __future__ import annotations

import sqlite3


WASTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS waste_shipments (
    shipment_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    default_regulation_id TEXT NOT NULL REFERENCES waste_regulations(regulation_id),
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT
);
CREATE TABLE IF NOT EXISTS waste_regulations (
    regulation_id TEXT PRIMARY KEY,
    version_label TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waste_custodians (
    custodian_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    site_id TEXT REFERENCES sites(site_id),
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    qualifications_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waste_items (
    item_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL,
    waste_type TEXT NOT NULL,
    initial_kg REAL NOT NULL CHECK(initial_kg > 0),
    current_kg REAL NOT NULL CHECK(current_kg >= 0),
    origin_custodian_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('at_camp', 'in_container', 'disposed')),
    current_container_id TEXT,
    generated_at TEXT NOT NULL,
    generated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waste_containers (
    container_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL,
    origin_custodian_id TEXT NOT NULL,
    current_custodian_id TEXT NOT NULL,
    packaging TEXT NOT NULL,
    gross_kg REAL NOT NULL CHECK(gross_kg > 0),
    regulation_id TEXT NOT NULL,
    regulation_hash TEXT NOT NULL,
    regulation_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('sealed', 'staged', 'in_transit', 'delivered', 'damaged', 'repacked', 'disposed')),
    prev_status TEXT,
    sealed_at TEXT NOT NULL,
    sealed_by TEXT NOT NULL,
    sealed_event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waste_container_items (
    container_id TEXT NOT NULL,
    item_id TEXT NOT NULL PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS idx_waste_container_items_container
    ON waste_container_items(container_id);
CREATE TABLE IF NOT EXISTS waste_moves (
    move_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_custodian_id TEXT NOT NULL,
    to_custodian_id TEXT NOT NULL,
    callback_token TEXT,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed', 'rejected', 'cancelled')),
    proposed_at TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS waste_move_items (
    move_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    PRIMARY KEY(move_id, container_id)
);
CREATE TABLE IF NOT EXISTS waste_obligations (
    obligation_id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL,
    container_id TEXT,
    kind TEXT NOT NULL,
    responsible_custodian_id TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'fulfilled')),
    created_at TEXT NOT NULL,
    fulfilled_at TEXT,
    created_move_id TEXT,
    fulfilled_move_id TEXT
);
CREATE TABLE IF NOT EXISTS waste_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    shipment_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waste_events_shipment ON waste_events(shipment_id, sequence);
CREATE TABLE IF NOT EXISTS waste_event_refs (
    event_id TEXT NOT NULL,
    shipment_id TEXT NOT NULL,
    container_id TEXT,
    item_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_waste_refs_event ON waste_event_refs(event_id);
CREATE INDEX IF NOT EXISTS idx_waste_refs_container ON waste_event_refs(container_id);
CREATE INDEX IF NOT EXISTS idx_waste_refs_item ON waste_event_refs(item_id);
CREATE INDEX IF NOT EXISTS idx_waste_refs_shipment ON waste_event_refs(shipment_id);
"""


def ensure_waste_schema(connection: sqlite3.Connection) -> None:
    """在既有基础库上幂等建立废弃物项目表。"""

    connection.executescript(WASTE_SCHEMA)
