"""废弃物回运责任项目的 SQLite 表结构与连接。

与基础服务共用同一个 SQLite 文件：业务表使用 ``wr_`` 前缀，安装时只做
CREATE TABLE IF NOT EXISTS，不改动基础表。除派生状态字段外，登记表、
事件、移交和血缘边均为只增记录，修正与冲销以新行体现，旧事实不被改写。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterator
from contextlib import contextmanager

from polar_station_foundation.storage import SCHEMA as FOUNDATION_SCHEMA


WASTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS wr_regulation_versions (
    version_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wr_lots (
    lot_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    waste_category TEXT NOT NULL,
    hazard_class TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    packaging TEXT NOT NULL,
    permit_id TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    generated_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wr_lot_corrections (
    event_id TEXT PRIMARY KEY,
    lot_id TEXT NOT NULL REFERENCES wr_lots(lot_id),
    delta REAL NOT NULL,
    reason TEXT NOT NULL,
    corrected_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wr_containers (
    container_id TEXT PRIMARY KEY,
    origin_site_id TEXT NOT NULL REFERENCES sites(site_id),
    status TEXT NOT NULL,
    regulation_version_id TEXT NOT NULL REFERENCES wr_regulation_versions(version_id),
    regulation_snapshot_json TEXT NOT NULL,
    regulation_snapshot_hash TEXT NOT NULL,
    gross_weight REAL,
    sealed_by TEXT NOT NULL,
    sealed_at TEXT NOT NULL,
    predecessor_container_ids_json TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT
);
CREATE TABLE IF NOT EXISTS wr_manifest (
    container_id TEXT NOT NULL REFERENCES wr_containers(container_id),
    lot_id TEXT NOT NULL REFERENCES wr_lots(lot_id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    PRIMARY KEY(container_id, lot_id)
);
CREATE TABLE IF NOT EXISTS wr_storage_stays (
    stay_id TEXT PRIMARY KEY,
    container_id TEXT NOT NULL REFERENCES wr_containers(container_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    keeper TEXT NOT NULL,
    handed_by TEXT NOT NULL,
    checked_in_at TEXT NOT NULL,
    due_at TEXT NOT NULL,
    checked_out_at TEXT,
    released_by TEXT
);
CREATE TABLE IF NOT EXISTS wr_custody_transfers (
    transfer_id TEXT PRIMARY KEY,
    transfer_type TEXT NOT NULL,
    custody_kind TEXT NOT NULL,
    container_id TEXT NOT NULL REFERENCES wr_containers(container_id),
    from_party TEXT NOT NULL,
    to_party TEXT NOT NULL,
    from_site_id TEXT NOT NULL REFERENCES sites(site_id),
    to_site_id TEXT,
    status TEXT NOT NULL,
    reversal_of TEXT,
    proposed_by TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    confirm_due_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wr_receipts (
    receipt_id TEXT PRIMARY KEY,
    transfer_id TEXT NOT NULL UNIQUE REFERENCES wr_custody_transfers(transfer_id),
    container_id TEXT NOT NULL REFERENCES wr_containers(container_id),
    items_json TEXT NOT NULL,
    acknowledged_by TEXT NOT NULL,
    acknowledged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wr_disposal_certificates (
    certificate_id TEXT PRIMARY KEY,
    container_id TEXT NOT NULL REFERENCES wr_containers(container_id),
    facility_site_id TEXT NOT NULL REFERENCES sites(site_id),
    certificate_ref TEXT NOT NULL,
    items_json TEXT NOT NULL,
    certified_by TEXT NOT NULL,
    certified_at TEXT NOT NULL,
    event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wr_edges (
    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    from_container_id TEXT,
    to_container_id TEXT,
    quantity REAL NOT NULL,
    reason TEXT NOT NULL,
    source_event_id TEXT,
    acknowledged_at TEXT,
    acknowledged_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_wr_edges_lot ON wr_edges(lot_id);
CREATE INDEX IF NOT EXISTS idx_wr_edges_container_from ON wr_edges(from_container_id);
CREATE INDEX IF NOT EXISTS idx_wr_edges_container_to ON wr_edges(to_container_id);
CREATE INDEX IF NOT EXISTS idx_wr_transfers_status ON wr_custody_transfers(status);
"""


class WasteDatabase:
    """打开（必要时创建）同时包含基础表和废弃物表的数据库。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(FOUNDATION_SCHEMA)
        self.connection.executescript(WASTE_SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()


def install_waste_schema(connection: sqlite3.Connection) -> None:
    """在既有基础库连接上追加废弃物表（供已有 Database 复用）。"""

    connection.executescript(WASTE_SCHEMA)
