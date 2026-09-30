"""核心领域服务：登记、校验、过敏原/待确认状态的实时派生与来源溯源。

设计要点
--------
1. 原料过敏原“当前有效声明”就地保存在 batches 行；更正直接覆盖。
2. 加工批次的过敏原永远不落地，查询时沿
       加工批次 → 生产加工 → 投入批次 ∪ 设备残留
   递归实时计算。原料更正、清洗撤销因此立即反映到“未签发结论”。
3. 设备残留：该设备在本次加工开始前最近一次已结束加工的产出内容；
   仅当两次加工之间存在“已验证且未撤销”的清洗时才清除。
4. 已签发标签把结论与全部依据快照为 JSON，永不重算。
5. 所有写操作在一个 BEGIN IMMEDIATE 事务里“先校验、后写入”，
   任何冲突都抛 DomainError 回滚，原状态不变。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import config

# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


class DomainError(Exception):
    """业务规则冲突。status_code 决定 HTTP 响应码。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def now_iso() -> str:
    return to_iso(datetime.now(timezone.utc))


def to_iso(dt: datetime) -> str:
    """统一存 UTC，Z 结尾，字典序与时间序一致。"""
    if dt.tzinfo is None:
        raise DomainError("时间必须带时区偏移", 422)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _jload(value: str | None, default: Any) -> Any:
    return json.loads(value) if value else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _check_allergens(allergens: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for a in allergens:
        if a not in seen:
            seen.add(a)
            result.append(a)
    if config.ALLOWED_ALLERGENS is not None:
        illegal = seen - config.ALLOWED_ALLERGENS
        if illegal:
            raise DomainError(
                f"过敏原不在允许清单内: {sorted(illegal)}; 允许: {sorted(config.ALLOWED_ALLERGENS)}"
            )
    return result


def _row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


# --------------------------------------------------------------------------- #
# 原料批次
# --------------------------------------------------------------------------- #


def register_raw_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    allergens: list[str],
    pending: bool,
) -> dict:
    allergens = _check_allergens(allergens)
    if pending and allergens:
        raise DomainError("待确认状态与明确过敏原集合互斥（待确认即过敏原未知）")

    # 事务由路由层 transaction() 统一包裹；全部语句一起提交或回滚
    if _row(conn, "SELECT 1 FROM batches WHERE id=?", (batch_id,)):
        raise DomainError(f"批次已存在: {batch_id}", 409)
    conn.execute(
        "INSERT INTO batches(id, kind, created_at, allergens, pending)"
        " VALUES(?,?,?,?,?)",
        (batch_id, "raw", now_iso(), _dumps(allergens), int(pending)),
    )
    return get_batch(conn, batch_id)


def correct_raw_batch(
    conn: sqlite3.Connection, batch_id: str, allergens: list[str], pending: bool
) -> dict:
    """原料声明更正：覆盖当前有效声明。

    已签发标签不受影响（其依据已快照）；未签发结论下次查询即按新声明重算。
    """
    allergens = _check_allergens(allergens)
    if pending and allergens:
        raise DomainError("待确认状态与明确过敏原集合互斥")
    b = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        raise DomainError(f"批次不存在: {batch_id}", 404)
    if b["kind"] != "raw":
        raise DomainError("只有原料批次允许直接更正过敏原声明", 422)
    conn.execute(
        "UPDATE batches SET allergens=?, pending=? WHERE id=?",
        (_dumps(allergens), int(pending), batch_id),
    )
    return get_batch(conn, batch_id)


# --------------------------------------------------------------------------- #
# 加工批次
# --------------------------------------------------------------------------- #


def _batch_available_at(conn: sqlite3.Connection, batch_id: str, start_iso: str) -> str | None:
    """返回不可用原因；None 表示可用。原料批次登记即可用。"""
    b = _row(conn, "SELECT kind FROM batches WHERE id=?", (batch_id,))
    if not b:
        return f"批次不存在: {batch_id}"
    if b["kind"] == "raw":
        return None
    p = _producer(conn, batch_id)
    if not p:
        return f"加工批次 {batch_id} 没有生产记录"
    if p["end_utc"] > start_iso:
        return (
            f"投入批次 {batch_id} 在加工开始时不可用：其产出加工结束于 {p['end_utc']}，"
            f"晚于本次开始 {start_iso}"
        )
    return None


def _producer(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.* FROM processings p"
        " JOIN processing_io io ON io.processing_id=p.id"
        " WHERE io.role='output' AND io.batch_id=?",
        (batch_id,),
    ).fetchone()


def _io_batches(conn: sqlite3.Connection, proc_id: str, role: str) -> list[str]:
    rows = conn.execute(
        "SELECT batch_id FROM processing_io WHERE processing_id=? AND role=?"
        " ORDER BY position",
        (proc_id, role),
    ).fetchall()
    return [r["batch_id"] for r in rows]


def _overlaps(
    conn: sqlite3.Connection, equipment: str, start_iso: str, end_utc: str, exclude_id: str | None = None
) -> sqlite3.Row | None:
    """半开区间重叠判定：start < other.end AND end > other.start。"""
    sql = (
        "SELECT * FROM processings WHERE equipment=? AND start_utc < ? AND end_utc > ?"
    )
    params: list[Any] = [equipment, end_utc, start_iso]
    if exclude_id:
        sql += " AND id<>?"
        params.append(exclude_id)
    sql += " ORDER BY start_utc LIMIT 1"
    return conn.execute(sql, params).fetchone()


def _upstream_batches(conn: sqlite3.Connection, batch_ids: list[str]) -> set[str]:
    """沿 批次→生产加工→投入批次 向上求闭包（用于成环检测）。"""
    seen: set[str] = set()
    stack = list(batch_ids)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        p = _producer(conn, cur)
        if p:
            stack.extend(_io_batches(conn, p["id"], "input"))
    return seen


def register_processing(
    conn: sqlite3.Connection,
    proc_id: str,
    equipment: str,
    start_utc: datetime,
    end_utc: datetime,
    inputs: list[str],
    outputs: list[str],
) -> dict:
    start_iso, end_iso = to_iso(start_utc), to_iso(end_utc)

    # ---- 纯参数校验 -----------------------------------------------------
    if end_iso <= start_iso:
        raise DomainError(f"加工结束 {end_iso} 必须晚于开始 {start_iso}", 422)
    if len(set(inputs)) != len(inputs):
        raise DomainError("投入批次存在重复")
    if len(set(outputs)) != len(outputs):
        raise DomainError("产出批次存在重复")
    if set(inputs) & set(outputs):
        raise DomainError("同一批次不能既是投入又是产出（自环）")
    if _row(conn, "SELECT 1 FROM processings WHERE id=?", (proc_id,)):
        raise DomainError(f"加工记录已存在: {proc_id}", 409)

    # ---- 依赖批次存在且按时可用 ----------------------------------------
    for bid in inputs:
        reason = _batch_available_at(conn, bid, start_iso)
        if reason:
            code = 404 if "不存在" in reason else 409
            raise DomainError(reason, code)

    for bid in outputs:
        if _row(conn, "SELECT 1 FROM batches WHERE id=?", (bid,)):
            raise DomainError(f"批次已存在，不能再次作为产出: {bid}", 409)

    # ---- 成环：产出不得是任意投入的上游 -------------------------------
    reachable = _upstream_batches(conn, inputs)
    cyclic = [o for o in outputs if o in reachable]
    if cyclic:
        raise DomainError(f"登记后批次来源将成环，涉及产出: {cyclic}", 409)

    # ---- 同设备加工时间区间不得重叠 ------------------------------------
    hit = _overlaps(conn, equipment, start_iso, end_iso)
    if hit:
        raise DomainError(
            f"设备 {equipment} 在 {start_iso}~{end_iso} 与加工 {hit['id']}"
            f"({hit['start_utc']}~{hit['end_utc']}) 时间重叠",
            409,
        )

    # ---- 全部通过，写入（先建产出批次行，再写 IO 边以满足外键） --------
    conn.execute(
        "INSERT INTO processings(id, equipment, start_utc, end_utc, created_at)"
        " VALUES(?,?,?,?,?)",
        (proc_id, equipment, start_iso, end_iso, now_iso()),
    )
    for pos, bid in enumerate(outputs):
        conn.execute(
            "INSERT INTO batches(id, kind, created_at, allergens, pending)"
            " VALUES(?,?,?,?,?)",
            (bid, "processed", now_iso(), "[]", 0),
        )
    for pos, bid in enumerate(inputs):
        conn.execute(
            "INSERT INTO processing_io(processing_id, role, batch_id, position)"
            " VALUES(?,?,?,?)",
            (proc_id, "input", bid, pos),
        )
    for pos, bid in enumerate(outputs):
        conn.execute(
            "INSERT INTO processing_io(processing_id, role, batch_id, position)"
            " VALUES(?,?,?,?)",
            (proc_id, "output", bid, pos),
        )
    return get_processing(conn, proc_id)


# --------------------------------------------------------------------------- #
# 清洗
# --------------------------------------------------------------------------- #


def register_cleaning(
    conn: sqlite3.Connection,
    cleaning_id: str,
    equipment: str,
    cleaned_utc: datetime,
    verified: bool,
    note: str,
) -> dict:
    c_iso = to_iso(cleaned_utc)
    if _row(conn, "SELECT 1 FROM cleanings WHERE id=?", (cleaning_id,)):
        raise DomainError(f"清洗记录已存在: {cleaning_id}", 409)

    # 清洗时刻落在同设备某次加工区间内部属于无效时序（端点允许）
    inside = conn.execute(
        "SELECT id FROM processings WHERE equipment=? AND start_utc < ? AND end_utc > ? LIMIT 1",
        (equipment, c_iso, c_iso),
    ).fetchone()
    if inside:
        raise DomainError(
            f"清洗时刻 {c_iso} 落在设备 {equipment} 的加工 {inside['id']} 进行期间",
            422,
        )

    conn.execute(
        "INSERT INTO cleanings(id, equipment, cleaned_utc, verified, revoked, note, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (cleaning_id, equipment, c_iso, int(verified), 0, note, now_iso()),
    )
    return get_cleaning(conn, cleaning_id)


def revoke_cleaning(conn: sqlite3.Connection, cleaning_id: str) -> dict:
    """撤销清洗（幂等）：残留按“没有有效清洗”重算。"""
    if not _row(conn, "SELECT 1 FROM cleanings WHERE id=?", (cleaning_id,)):
        raise DomainError(f"清洗记录不存在: {cleaning_id}", 404)
    conn.execute("UPDATE cleanings SET revoked=1 WHERE id=?", (cleaning_id,))
    return get_cleaning(conn, cleaning_id)


# --------------------------------------------------------------------------- #
# 实时派生计算 + 来源溯源
# --------------------------------------------------------------------------- #


class Evaluator:
    """沿来源图递归计算任意加工/加工批次的当前有效结论与来源路径。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.evidence: dict[str, Any] = {
            "raw_declarations": {},
            "processings": {},
            "cleanings": {},
            "residue_decisions": {},
        }
        self._content_cache: dict[str, dict[str, Any]] = {}

    # ---- 基础查询 ----------------------------------------------------------

    def _batch(self, batch_id: str) -> sqlite3.Row:
        b = _row(self.conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
        if not b:
            raise DomainError(f"批次不存在: {batch_id}", 404)
        return b

    def _processing(self, proc_id: str) -> sqlite3.Row:
        p = _row(self.conn, "SELECT * FROM processings WHERE id=?", (proc_id,))
        if not p:
            raise DomainError(f"加工记录不存在: {proc_id}", 404)
        return p

    def _record_processing(self, p: sqlite3.Row) -> None:
        if p["id"] in self.evidence["processings"]:
            return
        self.evidence["processings"][p["id"]] = {
            "processing_id": p["id"],
            "equipment": p["equipment"],
            "start_utc": p["start_utc"],
            "end_utc": p["end_utc"],
            "inputs": _io_batches(self.conn, p["id"], "input"),
            "outputs": _io_batches(self.conn, p["id"], "output"),
        }

    def _record_raw(self, b: sqlite3.Row) -> dict:
        decl = {
            "batch_id": b["id"],
            "allergens": _jload(b["allergens"], []),
            "pending": bool(b["pending"]),
        }
        self.evidence["raw_declarations"][b["id"]] = decl
        return decl

    # ---- 残留判定 ----------------------------------------------------------

    def _previous_processing(self, p: sqlite3.Row) -> sqlite3.Row | None:
        """同设备、在本次开始前已结束的最近一次加工（不含自身）。"""
        return self.conn.execute(
            "SELECT * FROM processings WHERE equipment=? AND id<>? AND end_utc<=?"
            " ORDER BY end_utc DESC, start_utc DESC, id DESC LIMIT 1",
            (p["equipment"], p["id"], p["start_utc"]),
        ).fetchone()

    def residue_decision(self, p: sqlite3.Row) -> dict:
        """判定加工 p 是否继承设备残留，并收集窗口内全部清洗作为证据。"""
        cached = self.evidence["residue_decisions"].get(p["id"])
        if cached:
            return cached

        prev = self._previous_processing(p)
        window: list[dict] = []
        effective: list[str] = []
        if prev:
            rows = self.conn.execute(
                "SELECT * FROM cleanings WHERE equipment=? AND cleaned_utc>=? AND cleaned_utc<=?"
                " ORDER BY cleaned_utc, id",
                (p["equipment"], prev["end_utc"], p["start_utc"]),
            ).fetchall()
            for r in rows:
                item = {
                    "cleaning_id": r["id"],
                    "cleaned_utc": r["cleaned_utc"],
                    "verified": bool(r["verified"]),
                    "revoked": bool(r["revoked"]),
                }
                window.append(item)
                self.evidence["cleanings"][r["id"]] = {
                    **item,
                    "equipment": r["equipment"],
                    "note": r["note"],
                }
                if r["verified"] and not r["revoked"]:
                    effective.append(r["id"])

        if not prev:
            carried, reason = False, "no_previous_processing"
        elif effective:
            carried, reason = False, "verified_cleaning_between"
        else:
            carried, reason = True, "no_verified_cleaning_between"

        decision = {
            "processing_id": p["id"],
            "equipment": p["equipment"],
            "previous_processing_id": prev["id"] if prev else None,
            "window_start_utc": prev["end_utc"] if prev else None,
            "window_end_utc": p["start_utc"],
            "cleanings_in_window": window,
            "effective_cleaning_ids": effective,
            "residue_carried": carried,
            "reason": reason,
        }
        self.evidence["residue_decisions"][p["id"]] = decision
        return decision

    # ---- 内容计算（过敏原集合 + 待确认） ----------------------------------

    def _proc_content(self, proc_id: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if proc_id in self._content_cache:
            return self._content_cache[proc_id]
        if proc_id in stack:  # 防御性成环保护（正常登记已拒绝成环）
            return {"allergens": set(), "pending": False}
        p = self._processing(proc_id)
        self._record_processing(p)

        allergens: set[str] = set()
        pending = False
        for bid in _io_batches(self.conn, proc_id, "input"):
            c = self._batch_content(bid, stack + (proc_id,))
            allergens |= c["allergens"]
            pending = pending or c["pending"]

        decision = self.residue_decision(p)
        if decision["residue_carried"] and decision["previous_processing_id"]:
            rc = self._proc_content(decision["previous_processing_id"], stack + (proc_id,))
            allergens |= rc["allergens"]
            pending = pending or rc["pending"]

        result = {"allergens": allergens, "pending": pending}
        self._content_cache[proc_id] = result
        return result

    def _batch_content(self, batch_id: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        b = self._batch(batch_id)
        if b["kind"] == "raw":
            self._record_raw(b)
            return {"allergens": set(_jload(b["allergens"], [])), "pending": bool(b["pending"])}
        producer = _producer(self.conn, batch_id)
        if not producer:
            return {"allergens": set(), "pending": False}
        return self._proc_content(producer["id"], stack)

    # ---- 来源路径 ----------------------------------------------------------

    @staticmethod
    def _raw_decl_node(decl: dict) -> dict:
        return {
            "type": "raw_declaration",
            "batch_id": decl["batch_id"],
            "allergens": decl["allergens"],
            "pending": decl["pending"],
        }

    @staticmethod
    def _batch_node(b: sqlite3.Row) -> dict:
        return {"type": "batch", "batch_id": b["id"], "kind": b["kind"]}

    def _proc_node(self, p: sqlite3.Row, role: str, extra: dict | None = None) -> dict:
        node = {
            "type": "processing",
            "processing_id": p["id"],
            "equipment": p["equipment"],
            "start_utc": p["start_utc"],
            "end_utc": p["end_utc"],
            "role": role,
        }
        if extra:
            node.update(extra)
        return node

    def _proc_sources(
        self, proc_id: str, stack: tuple[str, ...]
    ) -> dict[str, list[list[dict]]]:
        """返回终止于本加工节点的全部来源路径：{allergen: [paths...], '__pending__': [...]}。"""
        if proc_id in stack:
            return {}
        p = self._processing(proc_id)
        self._record_processing(p)
        out: dict[str, list[list[dict]]] = {}

        def add(allergen: str, path: list[dict]) -> None:
            out.setdefault(allergen, []).append(path)

        # 投入批次分支
        for bid in _io_batches(self.conn, proc_id, "input"):
            for key, paths in self._batch_sources(bid, stack + (proc_id,)).items():
                node = self._proc_node(p, "input", {"input_batch_id": bid})
                for path in paths:
                    add(key, path + [node])

        # 设备残留分支
        decision = self.residue_decision(p)
        if decision["residue_carried"] and decision["previous_processing_id"]:
            prev_id = decision["previous_processing_id"]
            prev = self._processing(prev_id)
            residue_extra = {
                "residue": {
                    "equipment": p["equipment"],
                    "previous_processing_id": prev_id,
                    "reason": decision["reason"],
                    "effective_cleaning_ids": decision["effective_cleaning_ids"],
                    "cleanings_in_window": decision["cleanings_in_window"],
                }
            }
            for key, paths in self._proc_sources(prev_id, stack + (proc_id,)).items():
                node = self._proc_node(p, "residue", residue_extra)
                for path in paths:
                    add(key, path + [node])
        return out

    def _batch_sources(
        self, batch_id: str, stack: tuple[str, ...] = ()
    ) -> dict[str, list[list[dict]]]:
        """返回终止于该批次节点的来源路径。"""
        b = self._batch(batch_id)
        if b["kind"] == "raw":
            decl = self._record_raw(b)
            base = [self._raw_decl_node(decl), self._batch_node(b)]
            result: dict[str, list[list[dict]]] = {}
            for a in decl["allergens"]:
                result.setdefault(a, []).append(list(base))
            if decl["pending"]:
                result.setdefault("__pending__", []).append(list(base))
            return result

        producer = _producer(self.conn, batch_id)
        if not producer:
            return {}
        result = {}
        for key, paths in self._proc_sources(producer["id"], stack).items():
            result.setdefault(key, [])
            for path in paths:
                result[key].append(path + [self._batch_node(b)])
        return result

    @staticmethod
    def _dedupe(paths: list[list[dict]]) -> list[list[dict]]:
        uniq: dict[str, list[dict]] = {}
        for path in paths:
            key = _dumps(path)
            uniq.setdefault(key, path)
        return [uniq[k] for k in sorted(uniq)]

    # ---- 对外入口 ----------------------------------------------------------

    def evaluate_batch(self, batch_id: str) -> dict:
        b = self._batch(batch_id)
        content = self._batch_content(batch_id)
        raw_sources = self._batch_sources(batch_id)

        allergen_paths: dict[str, list[list[dict]]] = {}
        for a, paths in raw_sources.items():
            if a == "__pending__":
                continue
            allergen_paths[a] = self._dedupe(paths)

        result = {
            "allergens": sorted(content["allergens"]),
            "pending": content["pending"],
            "sources": {
                "allergens": {a: allergen_paths.get(a, []) for a in sorted(content["allergens"])},
                "pending": self._dedupe(raw_sources.get("__pending__", []))
                if content["pending"]
                else [],
            },
            "evidence": self.evidence,
        }
        return result

    def evaluate_processing(self, proc_id: str) -> dict:
        p = self._processing(proc_id)
        content = self._proc_content(proc_id)
        sources = self._proc_sources(proc_id, ())
        allergen_paths = {
            a: self._dedupe(paths)
            for a, paths in sources.items()
            if a != "__pending__"
        }
        return {
            "allergens": sorted(content["allergens"]),
            "pending": content["pending"],
            "sources": {
                "allergens": {a: allergen_paths.get(a, []) for a in sorted(content["allergens"])},
                "pending": self._dedupe(sources.get("__pending__", []))
                if content["pending"]
                else [],
            },
            "evidence": self.evidence,
            "residue": self.residue_decision(p),
        }


def evaluate_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    return Evaluator(conn).evaluate_batch(batch_id)


# --------------------------------------------------------------------------- #
# 标签签发（冻结）
# --------------------------------------------------------------------------- #


def issue_label(
    conn: sqlite3.Connection, batch_id: str, supersedes_id: int | None
) -> dict:
    if not _row(conn, "SELECT 1 FROM batches WHERE id=?", (batch_id,)):
        raise DomainError(f"批次不存在: {batch_id}", 404)

    old_label = None
    if supersedes_id is not None:
        old_label = _row(conn, "SELECT * FROM labels WHERE id=?", (supersedes_id,))
        if not old_label:
            raise DomainError(f"被关联的旧标签不存在: {supersedes_id}", 404)
        if old_label["batch_id"] != batch_id:
            raise DomainError(
                f"标签 {supersedes_id} 属于批次 {old_label['batch_id']}，"
                f"与当前批次 {batch_id} 不一致",
                422,
            )

    evaluator = Evaluator(conn)
    current = evaluator.evaluate_batch(batch_id)
    issued = now_iso()
    snapshot = {
        "batch_id": batch_id,
        "issued_utc": issued,
        "supersedes_id": supersedes_id,
        "conclusion_basis": {
            "allergens": current["allergens"],
            "pending": current["pending"],
            "sources": current["sources"],
            "evidence": current["evidence"],
        },
    }
    conclusion = "pending" if current["pending"] else "finalized"
    cur = conn.execute(
        "INSERT INTO labels(batch_id, issued_utc, supersedes_id, conclusion, allergens, snapshot)"
        " VALUES(?,?,?,?,?,?)",
        (batch_id, issued, supersedes_id, conclusion, _dumps(current["allergens"]), _dumps(snapshot)),
    )
    return get_label(conn, cur.lastrowid)


# --------------------------------------------------------------------------- #
# 查询视图
# --------------------------------------------------------------------------- #


def _batch_basic(b: sqlite3.Row) -> dict:
    return {
        "id": b["id"],
        "kind": b["kind"],
        "created_at": b["created_at"],
        **(
            {"declared_allergens": _jload(b["allergens"], []), "pending": bool(b["pending"])}
            if b["kind"] == "raw"
            else {}
        ),
    }


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict:
    b = _row(conn, "SELECT * FROM batches WHERE id=?", (batch_id,))
    if not b:
        raise DomainError(f"批次不存在: {batch_id}", 404)
    info = _batch_basic(b)
    producer = _producer(conn, batch_id) if b["kind"] == "processed" else None
    if producer:
        info["produced_by"] = {
            "processing_id": producer["id"],
            "equipment": producer["equipment"],
            "start_utc": producer["start_utc"],
            "end_utc": producer["end_utc"],
            "available_from_utc": producer["end_utc"],
        }
    info["current"] = _current_view(conn, batch_id)
    return info


def _current_view(conn: sqlite3.Connection, batch_id: str) -> dict:
    ev = Evaluator(conn).evaluate_batch(batch_id)
    return {
        "allergens": ev["allergens"],
        "pending": ev["pending"],
        "sources": ev["sources"],
    }


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    result = []
    for b in rows:
        item = _batch_basic(b)
        item["current_allergens"] = Evaluator(conn).evaluate_batch(b["id"])["allergens"]
        result.append(item)
    return result


def get_processing(conn: sqlite3.Connection, proc_id: str) -> dict:
    p = _row(conn, "SELECT * FROM processings WHERE id=?", (proc_id,))
    if not p:
        raise DomainError(f"加工记录不存在: {proc_id}", 404)
    ev = Evaluator(conn).evaluate_processing(proc_id)
    return {
        "id": p["id"],
        "equipment": p["equipment"],
        "start_utc": p["start_utc"],
        "end_utc": p["end_utc"],
        "created_at": p["created_at"],
        "inputs": _io_batches(conn, proc_id, "input"),
        "outputs": _io_batches(conn, proc_id, "output"),
        "current": {
            "allergens": ev["allergens"],
            "pending": ev["pending"],
            "sources": ev["sources"],
        },
        "residue": ev["residue"],
    }


def list_processings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM processings ORDER BY start_utc, id").fetchall()
    return [
        {
            "id": r["id"],
            "equipment": r["equipment"],
            "start_utc": r["start_utc"],
            "end_utc": r["end_utc"],
            "inputs": _io_batches(conn, r["id"], "input"),
            "outputs": _io_batches(conn, r["id"], "output"),
        }
        for r in rows
    ]


def get_cleaning(conn: sqlite3.Connection, cleaning_id: str) -> dict:
    r = _row(conn, "SELECT * FROM cleanings WHERE id=?", (cleaning_id,))
    if not r:
        raise DomainError(f"清洗记录不存在: {cleaning_id}", 404)
    return _cleaning_view(r)


def _cleaning_view(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "equipment": r["equipment"],
        "cleaned_utc": r["cleaned_utc"],
        "verified": bool(r["verified"]),
        "revoked": bool(r["revoked"]),
        "note": r["note"],
        "created_at": r["created_at"],
    }


def list_cleanings(conn: sqlite3.Connection) -> list[dict]:
    return [_cleaning_view(r) for r in conn.execute(
        "SELECT * FROM cleanings ORDER BY cleaned_utc, id"
    ).fetchall()]


def get_label(conn: sqlite3.Connection, label_id: int) -> dict:
    r = _row(conn, "SELECT * FROM labels WHERE id=?", (label_id,))
    if not r:
        raise DomainError(f"标签不存在: {label_id}", 404)
    return _label_view(r)


def _label_view(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "batch_id": r["batch_id"],
        "issued_utc": r["issued_utc"],
        "supersedes_id": r["supersedes_id"],
        "conclusion": r["conclusion"],
        "allergens": _jload(r["allergens"], []),
        "frozen": True,
        "snapshot": _jload(r["snapshot"], {}),
    }


def list_labels(conn: sqlite3.Connection, batch_id: str | None = None) -> list[dict]:
    if batch_id:
        rows = conn.execute(
            "SELECT * FROM labels WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM labels ORDER BY id").fetchall()
    return [_label_view(r) for r in rows]
