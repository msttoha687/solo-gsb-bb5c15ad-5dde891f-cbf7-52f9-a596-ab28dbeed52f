"""端到端 API 测试：用临时 SQLite 文件 + TestClient。"""
from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch) -> Iterator[TestClient]:
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_DEMO", "false")

    from app import config
    from app.db import connect, init_db
    from app.main import app

    monkeypatch.setattr(config, "DB_PATH", db_path)
    init_db(connect(db_path))
    with TestClient(app) as c:
        yield c
    tmp.cleanup()


def raw(client, bid, allergens=None, pending=False):
    return client.post(
        "/raw-batches",
        json={"id": bid, "allergens": allergens or [], "pending": pending},
    )


def proc(client, pid, equip, start, end, inputs, outputs):
    return client.post(
        "/processings",
        json={
            "id": pid,
            "equipment": equip,
            "start_utc": start,
            "end_utc": end,
            "inputs": inputs,
            "outputs": outputs,
        },
    )


# --------------------------------------------------------------------------- #


def test_health_and_demo_flow(client):
    assert client.get("/health").json()["status"] == "ok"

    assert raw(client, "RM-P", ["peanut", "PEANUT"]).status_code == 201  # 大小写归一+去重
    r = proc(
        client, "PR1", "L1",
        "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z",
        ["RM-P"], ["FG1"],
    )
    assert r.status_code == 201, r.text

    cur = client.get("/batches/FG1").json()["current"]
    assert cur["allergens"] == ["PEANUT"]
    assert cur["pending"] is False
    # 来源路径：原料声明 -> 原料批次 -> 加工 -> 产出批次
    path = cur["sources"]["allergens"]["PEANUT"][0]
    assert [n["type"] for n in path] == [
        "raw_declaration", "batch", "processing", "batch"
    ]


def test_residue_carried_without_cleaning(client):
    raw(client, "RM-P", ["PEANUT"])
    raw(client, "RM-M", ["MILK"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-P"], ["FG1"])
    proc(client, "PR2", "L1", "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z", ["RM-M"], ["FG2"])

    cur = client.get("/batches/FG2").json()["current"]
    assert set(cur["allergens"]) == {"MILK", "PEANUT"}  # MILK 来自投入，PEANUT 来自残留
    p2 = client.get("/processings/PR2").json()
    assert p2["residue"]["residue_carried"] is True
    assert p2["residue"]["reason"] == "no_verified_cleaning_between"
    assert p2["residue"]["previous_processing_id"] == "PR1"


def test_verified_cleaning_clears_residue(client):
    raw(client, "RM-P", ["PEANUT"])
    raw(client, "RM-M", ["MILK"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-P"], ["FG1"])

    r = client.post(
        "/cleanings",
        json={
            "id": "CL1", "equipment": "L1",
            "cleaned_utc": "2026-09-30T11:30:00Z",
            "verified": True,
        },
    )
    assert r.status_code == 201, r.text

    proc(client, "PR2", "L1", "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z", ["RM-M"], ["FG2"])
    cur = client.get("/batches/FG2").json()["current"]
    assert cur["allergens"] == ["MILK"]
    p2 = client.get("/processings/PR2").json()
    assert p2["residue"]["residue_carried"] is False
    assert p2["residue"]["effective_cleaning_ids"] == ["CL1"]


def test_unverified_cleaning_keeps_residue(client):
    raw(client, "RM-P", ["PEANUT"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-P"], ["FG1"])
    client.post(
        "/cleanings",
        json={"id": "CL1", "equipment": "L1", "cleaned_utc": "2026-09-30T11:30:00Z",
              "verified": False},
    )
    proc(client, "PR2", "L1", "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z", [], ["FG2"])
    assert client.get("/batches/FG2").json()["current"]["allergens"] == ["PEANUT"]


def test_revoke_cleaning_reinstates_residue(client):
    raw(client, "RM-P", ["PEANUT"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-P"], ["FG1"])
    client.post(
        "/cleanings",
        json={"id": "CL1", "equipment": "L1", "cleaned_utc": "2026-09-30T11:30:00Z"},
    )
    proc(client, "PR2", "L1", "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z", [], ["FG2"])
    assert client.get("/batches/FG2").json()["current"]["allergens"] == []

    assert client.post("/cleanings/CL1/revoke").status_code == 200
    # 撤销后未签发结论按当前有效记录重算：残留恢复
    assert client.get("/batches/FG2").json()["current"]["allergens"] == ["PEANUT"]
    assert client.post("/cleanings/CL1/revoke").status_code == 200  # 幂等


def test_pending_propagation_and_source_paths(client):
    raw(client, "RM-U", [], pending=True)
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-U"], ["FG1"])
    proc(client, "PR2", "L9", "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z", ["FG1"], ["FG2"])

    cur = client.get("/batches/FG2").json()["current"]
    assert cur["pending"] is True
    assert cur["allergens"] == []
    path = cur["sources"]["pending"][0]
    assert [n["type"] for n in path] == [
        "raw_declaration", "batch", "processing", "batch", "processing", "batch"
    ]
    assert all(n.get("pending") for n in path if n["type"] == "raw_declaration")


def test_raw_correction_recomputes_open_conclusions_but_frozen_label_kept(client):
    raw(client, "RM-1", ["WHEAT"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-1"], ["FG1"])

    # 签发旧标签：结论 WHEAT
    r = client.post("/labels", json={"batch_id": "FG1"})
    assert r.status_code == 201
    label1 = r.json()
    assert label1["allergens"] == ["WHEAT"]
    assert label1["conclusion"] == "finalized"

    # 原料更正：WHEAT -> EGG
    assert client.patch("/raw-batches/RM-1", json={"allergens": ["EGG"]}).status_code == 200
    assert client.get("/batches/FG1").json()["current"]["allergens"] == ["EGG"]

    # 旧标签不变
    frozen = client.get(f"/labels/{label1['id']}").json()
    assert frozen["allergens"] == ["WHEAT"]
    assert frozen["snapshot"]["conclusion_basis"]["allergens"] == ["WHEAT"]

    # 新标签关联旧版
    r = client.post("/labels", json={"batch_id": "FG1", "supersedes_id": label1["id"]})
    assert r.status_code == 201
    label2 = r.json()
    assert label2["allergens"] == ["EGG"]
    assert label2["supersedes_id"] == label1["id"]
    assert client.get("/labels", params={"batch_id": "FG1"}).json() == [
        client.get(f"/labels/{label1['id']}").json(),
        label2,
    ]


def test_overlap_rejected_and_state_preserved(client):
    raw(client, "RM-1", ["EGG"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-1"], ["FG1"])

    # 区间重叠（端点相接不重叠）
    r = proc(
        client, "PR2", "L1",
        "2026-09-30T10:30:00Z", "2026-09-30T11:30:00Z",
        ["RM-1"], ["FG2"],
    )
    assert r.status_code == 409
    assert "重叠" in r.json()["error"]
    # 状态保持：没有 FG2、没有 PR2
    assert client.get("/batches/FG2").status_code == 404
    assert client.get("/processings/PR2").status_code == 404

    # 结束<=开始：无效时序
    r = proc(
        client, "PR3", "L1",
        "2026-09-30T12:00:00Z", "2026-09-30T12:00:00Z",
        ["RM-1"], ["FG3"],
    )
    assert r.status_code == 422


def test_input_not_available_rejected(client):
    raw(client, "RM-1", ["EGG"])
    # 投入在 12:00 才生产结束，却要 09:00 就使用
    proc(client, "PR-LATE", "L1",
         "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z", ["RM-1"], ["LATE"])
    r = proc(
        client, "PR-EARLY", "L2",
        "2026-09-30T09:00:00Z", "2026-09-30T10:00:00Z",
        ["LATE"], ["EARLY"],
    )
    assert r.status_code == 409
    assert "不可用" in r.json()["error"]


def test_cycle_rejected(client):
    raw(client, "RM-1", ["EGG"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-1"], ["B1"])
    proc(client, "PR2", "L2", "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z", ["B1"], ["B2"])
    # B2 已存在不能再做产出；构造真实成环需要把已有批次重新当产出——
    # 而产出必须是新批次，故环只能通过“新产出 ∈ 投入批次上游”形成：
    r = proc(
        client, "PR3", "L3",
        "2026-09-30T14:00:00Z", "2026-09-30T15:00:00Z",
        ["B2"], ["B3"],
    )
    assert r.status_code == 201
    r = proc(
        client, "PR4", "L4",
        "2026-09-30T16:00:00Z", "2026-09-30T17:00:00Z",
        ["B3"], ["B1"],   # B1 已存在 -> 409
    )
    assert r.status_code == 409

    # 直接自环
    r = proc(
        client, "PR5", "L5",
        "2026-09-30T16:00:00Z", "2026-09-30T17:00:00Z",
        ["B2"], ["B2"],
    )
    assert r.status_code == 400


def test_duplicate_and_404s(client):
    raw(client, "RM-1", ["EGG"])
    assert raw(client, "RM-1", ["EGG"]).status_code == 409
    assert client.get("/batches/NOPE").status_code == 404
    assert client.post("/labels", json={"batch_id": "NOPE"}).status_code == 404
    assert client.post("/cleanings/NOPE/revoke").status_code == 404


def test_cleaning_during_processing_rejected(client):
    raw(client, "RM-1", ["EGG"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-1"], ["FG1"])
    r = client.post(
        "/cleanings",
        json={"id": "CL1", "equipment": "L1", "cleaned_utc": "2026-09-30T10:30:00Z"},
    )
    assert r.status_code == 422
    # 端点时刻允许
    assert client.post(
        "/cleanings",
        json={"id": "CL2", "equipment": "L1", "cleaned_utc": "2026-09-30T11:00:00Z"},
    ).status_code == 201


def test_residue_path_shows_residue_role(client):
    raw(client, "RM-P", ["PEANUT"])
    proc(client, "PR1", "L1", "2026-09-30T10:00:00Z", "2026-09-30T11:00:00Z", ["RM-P"], ["FG1"])
    proc(client, "PR2", "L1", "2026-09-30T12:00:00Z", "2026-09-30T13:00:00Z", [], ["FG2"])

    cur = client.get("/batches/FG2").json()["current"]
    peanut_paths = cur["sources"]["allergens"]["PEANUT"]
    # 路径中本加工节点必须带 residue 角色与判定依据
    own_proc_nodes = [n for p in peanut_paths for n in p if n.get("processing_id") == "PR2"]
    assert own_proc_nodes and all(n["role"] == "residue" for n in own_proc_nodes)
    assert own_proc_nodes[0]["residue"]["previous_processing_id"] == "PR1"
