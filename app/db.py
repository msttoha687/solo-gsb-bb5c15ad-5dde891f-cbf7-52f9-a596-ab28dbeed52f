"""SQLite 连接管理与建表初始化。

时序字段统一存成 UTC 的 ISO-8601 字符串（带时区偏移 Z），
字典序即时间序，可直接用字符串做区间比较。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS batches (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('raw', 'processed')),
    created_at   TEXT NOT NULL,
    -- 原料批次当前有效声明（已更正也只保留最新值；旧值的快照由 labels 承载）
    allergens    TEXT NOT NULL DEFAULT '[]',  -- JSON 数组，大写
    pending      INTEGER NOT NULL DEFAULT 0   -- 1 = 待确认
);

CREATE TABLE IF NOT EXISTS processings (
    id            TEXT PRIMARY KEY,
    equipment     TEXT NOT NULL,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proc_equip_time
    ON processings(equipment, start_utc, end_utc);

-- 加工的投入/产出边。加工批次产出：一个加工 -> 多个产出批次
CREATE TABLE IF NOT EXISTS processing_io (
    processing_id TEXT NOT NULL REFERENCES processings(id),
    role          TEXT NOT NULL CHECK (role IN ('input', 'output')),
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    position      INTEGER NOT NULL,          -- 登记顺序，用于稳定输出
    PRIMARY KEY (processing_id, role, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_io_batch ON processing_io(batch_id);
CREATE INDEX IF NOT EXISTS idx_io_role_proc ON processing_io(processing_id, role);

-- 设备清洗。verified=1 表示已验证；revoked=1 表示事后撤销
CREATE TABLE IF NOT EXISTS cleanings (
    id           TEXT PRIMARY KEY,
    equipment    TEXT NOT NULL,
    cleaned_utc  TEXT NOT NULL,
    verified     INTEGER NOT NULL DEFAULT 1,
    revoked      INTEGER NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clean_equip_time
    ON cleanings(equipment, cleaned_utc);

-- 已签发标签：结论与依据在签发时整体冻结为 JSON，后续登记/更正/撤销不影响它
CREATE TABLE IF NOT EXISTS labels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL REFERENCES batches(id),
    issued_utc    TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES labels(id),   -- 关联旧版（可为空）
    conclusion    TEXT NOT NULL,                     -- finalized | pending
    allergens     TEXT NOT NULL,                     -- JSON: 签发时过敏原集合
    snapshot      TEXT NOT NULL                      -- JSON: 完整依据与来源路径
);
CREATE INDEX IF NOT EXISTS idx_labels_batch ON labels(batch_id);
"""


def connect(db_path=None) -> sqlite3.Connection:
    path = config.DB_PATH if db_path is None else Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """幂等建表。"""
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 立刻拿写锁；任何异常回滚，保证“拒绝并保持原状态”。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()
