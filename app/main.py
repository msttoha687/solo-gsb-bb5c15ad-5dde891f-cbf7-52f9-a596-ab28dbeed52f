"""FastAPI 入口：过敏原溯源 HTTP API。

启动即幂等建表；SEED_DEMO=1 时在空库写入演示数据。
所有写操作在一个事务内“先校验、后写入”，冲突整体回滚。
"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import config, seed, services
from .db import connect, get_conn, init_db, transaction
from .models import (
    CleaningIn,
    IssueLabelIn,
    ProcessingIn,
    RawBatchCorrectIn,
    RawBatchIn,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_conn = connect()
    try:
        init_db(init_conn)
        if config.SEED_DEMO:
            seed.load_demo_if_empty(init_conn)
    finally:
        init_conn.close()
    yield


app = FastAPI(
    title="共线生产过敏原溯源服务",
    version="1.0.0",
    description=(
        "登记原料批次过敏原声明/待确认、加工批次（设备、UTC 起止、投入/产出）、"
        "设备清洗；实时派生过敏原与待确认状态并给出每条来源路径；"
        "签发标签冻结依据与结论。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(services.DomainError)
async def domain_error_handler(request: Request, exc: services.DomainError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


@app.get("/")
def root():
    return {
        "service": "allergen-traceability",
        "docs": "/docs",
        "health": "/health",
        "db": str(config.DB_PATH),
    }


@app.get("/health")
def health(conn: sqlite3.Connection = Depends(get_conn)):
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 原料批次
# --------------------------------------------------------------------------- #


@app.post("/raw-batches", status_code=201, tags=["raw-batches"])
def create_raw_batch(body: RawBatchIn, conn: sqlite3.Connection = Depends(get_conn)):
    with transaction(conn):
        return services.register_raw_batch(conn, body.id, body.allergens, body.pending)


@app.get("/raw-batches", tags=["raw-batches"])
def raw_batches(conn: sqlite3.Connection = Depends(get_conn)):
    rows = conn.execute("SELECT * FROM batches WHERE kind='raw' ORDER BY id").fetchall()
    return [services._batch_basic(r) for r in rows]


@app.patch("/raw-batches/{batch_id}", tags=["raw-batches"])
def correct_raw_batch(
    batch_id: str, body: RawBatchCorrectIn, conn: sqlite3.Connection = Depends(get_conn)
):
    with transaction(conn):
        return services.correct_raw_batch(conn, batch_id, body.allergens, body.pending)


# --------------------------------------------------------------------------- #
# 加工
# --------------------------------------------------------------------------- #


@app.post("/processings", status_code=201, tags=["processings"])
def create_processing(body: ProcessingIn, conn: sqlite3.Connection = Depends(get_conn)):
    with transaction(conn):
        return services.register_processing(
            conn,
            body.id,
            body.equipment,
            body.start_utc,
            body.end_utc,
            body.inputs,
            body.outputs,
        )


@app.get("/processings", tags=["processings"])
def processings(conn: sqlite3.Connection = Depends(get_conn)):
    return services.list_processings(conn)


@app.get("/processings/{proc_id}", tags=["processings"])
def processing_detail(proc_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return services.get_processing(conn, proc_id)


# --------------------------------------------------------------------------- #
# 清洗
# --------------------------------------------------------------------------- #


@app.post("/cleanings", status_code=201, tags=["cleanings"])
def create_cleaning(body: CleaningIn, conn: sqlite3.Connection = Depends(get_conn)):
    with transaction(conn):
        return services.register_cleaning(
            conn, body.id, body.equipment, body.cleaned_utc, body.verified, body.note
        )


@app.post("/cleanings/{cleaning_id}/revoke", status_code=200, tags=["cleanings"])
def revoke_cleaning(cleaning_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    with transaction(conn):
        return services.revoke_cleaning(conn, cleaning_id)


@app.get("/cleanings", tags=["cleanings"])
def cleanings(conn: sqlite3.Connection = Depends(get_conn)):
    return services.list_cleanings(conn)


# --------------------------------------------------------------------------- #
# 批次查询（含每项过敏原/待确认的来源路径）与标签
# --------------------------------------------------------------------------- #


@app.get("/batches", tags=["batches"])
def batches(conn: sqlite3.Connection = Depends(get_conn)):
    return services.list_batches(conn)


@app.get("/batches/{batch_id}", tags=["batches"])
def batch_detail(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return services.get_batch(conn, batch_id)


@app.post("/labels", status_code=201, tags=["labels"])
def issue_label(body: IssueLabelIn, conn: sqlite3.Connection = Depends(get_conn)):
    with transaction(conn):
        return services.issue_label(conn, body.batch_id, body.supersedes_id)


@app.get("/labels", tags=["labels"])
def labels(batch_id: str | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    return services.list_labels(conn, batch_id)


@app.get("/labels/{label_id}", tags=["labels"])
def label_detail(label_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    return services.get_label(conn, label_id)
