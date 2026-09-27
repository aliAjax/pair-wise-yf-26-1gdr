"""副本退役交接 —— 数据表层。

新增 ``copy_retirements`` 交接单表，并把 ``copies.state`` 的允许值从
``healthy/corrupt/degraded`` 扩展出 ``retired``（退役留档）。SQLite 无法直接
修改 CHECK 约束，旧库通过重建 copies 表完成迁移；迁移按 sqlite_master 中记录的
建表语句是否已包含 retired 做幂等判断，新库首次初始化会走一次空表重建。
"""
from __future__ import annotations

import sqlite3

RETIREMENTS_DDL = """
CREATE TABLE IF NOT EXISTS copy_retirements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_id INTEGER NOT NULL REFERENCES copies(id),
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    archive_id INTEGER NOT NULL REFERENCES archives(id),
    retiring_location TEXT NOT NULL,
    new_location TEXT NOT NULL,
    custodian TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','rejected')),
    blockers TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES users(id),
    reviewed_at TEXT
)
"""

RETIREMENTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_copy_retirements_version ON copy_retirements(version_id)",
    # 同一副本同一时间只允许存在一张待复核交接单
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_copy_retirements_pending "
    "ON copy_retirements(copy_id) WHERE status='pending'",
)

# 与 app.py 中 copies 定义保持同构，仅 CHECK 增加 'retired'
_COPIES_REBUILD_SQL = """
CREATE TABLE copies__new(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES archive_versions(id),
    location TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'healthy' CHECK(state IN ('healthy','corrupt','degraded','retired')),
    created_at TEXT NOT NULL,
    last_verified_at TEXT,
    UNIQUE(version_id,location)
);
INSERT INTO copies__new(id,version_id,location,state,created_at,last_verified_at)
    SELECT id,version_id,location,state,created_at,last_verified_at FROM copies;
DROP TABLE copies;
ALTER TABLE copies__new RENAME TO copies;
"""


def _copies_supports_retired(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='copies'"
    ).fetchone()
    return bool(row) and "retired" in (row[0] or "").lower()


def apply_schema(conn: sqlite3.Connection) -> None:
    """在已执行基础建表脚本的连接上追加退役交接所需结构。幂等。"""
    if not _copies_supports_retired(conn):
        # PRAGMA foreign_keys 不能在事务内切换；先确保无挂起事务，
        # 关闭外键后重建父表，copy_files 仍按表名 copies 关联。
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(_COPIES_REBUILD_SQL)
        conn.commit()
        conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(RETIREMENTS_DDL)
    for ddl in RETIREMENTS_INDEXES:
        conn.execute(ddl)
    conn.commit()
