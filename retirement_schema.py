"""副本退役交接数据表：退役申请单与副本退役标记。

规则见 retirement_rules.py，页面见 web/retirement.html。
"""
from __future__ import annotations

import sqlite3

RETIREMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS retirement_requests(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_id INTEGER NOT NULL REFERENCES copies(id),
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    successor_location TEXT NOT NULL,
    custodian TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','confirmed','returned')),
    requested_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES users(id),
    reviewed_at TEXT,
    blockers TEXT NOT NULL DEFAULT '[]'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_retirement_requests_pending
    ON retirement_requests(copy_id) WHERE state='pending';
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建退役申请单，并为 copies 补充 retired_at 退役标记列（可重复执行）。"""
    conn.executescript(RETIREMENT_SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(copies)")}
    if "retired_at" not in columns:
        conn.execute("ALTER TABLE copies ADD COLUMN retired_at TEXT")
