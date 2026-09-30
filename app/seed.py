"""演示数据（SEED_DEMO=1 且库为空时自动写入）。

场景：
  RM-PEANUT 含花生；RM-MILK 含奶；RM-UNKNOWN 待确认。
  10:00-11:00 LINE-A 生产 P1（花生）；
  12:00-13:00 LINE-A 未清洗直接生产 P2（继承投入的奶 + 设备残留花生）。
可自行登记 11:00~12:00 之间的已验证清洗观察残留被清除。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import services
from .db import transaction


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def load_demo_if_empty(conn: sqlite3.Connection) -> bool:
    has = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    if has:
        return False
    with transaction(conn):
        services.register_raw_batch(conn, "RM-PEANUT", ["PEANUT"], False)
        services.register_raw_batch(conn, "RM-MILK", ["MILK"], False)
        services.register_raw_batch(conn, "RM-UNKNOWN", [], True)
        services.register_processing(
            conn,
            "PR-1",
            "LINE-A",
            _dt("2026-09-30T10:00:00"),
            _dt("2026-09-30T11:00:00"),
            ["RM-PEANUT"],
            ["P1"],
        )
        services.register_processing(
            conn,
            "PR-2",
            "LINE-A",
            _dt("2026-09-30T12:00:00"),
            _dt("2026-09-30T13:00:00"),
            ["RM-MILK"],
            ["P2"],
        )
    return True
