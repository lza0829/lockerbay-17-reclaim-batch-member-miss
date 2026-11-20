"""路由级测试：回收页/核证页所用接口的协同与失败整趟回滚。"""

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod
from app import seed
from app.main import app
from app.modules import overdue_auto_reclaim as svc

SCAN_NOW = "2026-11-20T12:00:00Z"
OVERDUE_OCC = "OCC-CAB-A-02-E2"
FRESH_OCC = "OCC-CAB-B-01-E4"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "lockerbay_api.db")
    seed.init_db()
    # raise_server_exceptions=False：让服务端异常以真实 500 响应返回，
    # 才能断言路由的整趟 rollback 行为而非直接抛栈
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_run_endpoint_persists_members_and_readback(client):
    r = client.post("/api/reclaim/run", json={"now": SCAN_NOW})
    assert r.status_code == 200
    body = r.json()
    assert body["reclaimed_count"] == 1
    assert body["closed_n"] == body["member_count"] == 1
    assert body["members"][0]["occupancy_id"] == OVERDUE_OCC
    run_id = body["run_id"]

    # 趟次列表
    runs = client.get("/api/reclaim/runs").json()
    assert [x["run_id"] for x in runs] == [run_id]

    # 成员投影只读接口（显式 run_id 与缺省最新趟）
    view = client.get(f"/api/reclaim/run-members?run_id={run_id}").json()
    assert view["closed_n"] == 1 and view["member_count"] == 1 and view["valid"] is True
    assert view["members"][0]["closed_at"] == body["scanned_at"]
    assert client.get("/api/reclaim/run-members").json()["run_id"] == run_id

    # 趟次成员核证只读接口
    attest = client.get(f"/api/reclaim/attest?run_id={run_id}").json()
    assert attest["valid"] is True
    assert all(attest["checks"].values())

    # 回收核证页仍可查到关闭占用
    reclaims = client.get("/api/reclaims").json()
    assert [x["occupancy_id"] for x in reclaims] == [OVERDUE_OCC]

    # 未到期占用未被触碰
    occs = {x["id"]: x for x in client.get("/api/occupancies").json()}
    assert occs[FRESH_OCC]["status"] == "occupied"
    assert occs[OVERDUE_OCC]["status"] == "reclaimed"


def test_run_endpoint_rolls_back_on_member_failure(client):
    """半截写成员失败经路由 → 500，整趟回滚：无趟次、无成员、占用仍 open。"""
    # 模块级故障开关：TestClient 工作线程与主线程共享同一 svc 模块对象
    svc._FAULT_INJECTION["fail_next"] = True
    r = client.post("/api/reclaim/run", json={"now": SCAN_NOW})
    assert r.status_code == 500

    with db_mod.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM reclaim_runs").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM reclaim_run_members").fetchone()["c"] == 0
        row = dict(conn.execute(
            "SELECT status, closed_at FROM occupancies WHERE id=?", (OVERDUE_OCC,)
        ).fetchone())
        assert row["status"] == "occupied" and row["closed_at"] is None
        cell = dict(conn.execute(
            "SELECT status FROM cells WHERE id='CAB-A-02'").fetchone())
        assert cell["status"] == "occupied"

    # 故障开关已在抛错时自动复位，重跑成功且无重复成员
    assert svc._FAULT_INJECTION["fail_next"] is False
    r2 = client.post("/api/reclaim/run", json={"now": SCAN_NOW})
    assert r2.status_code == 200
    assert r2.json()["member_count"] == 1


def test_unknown_run_id_returns_404(client):
    assert client.get("/api/reclaim/run-members?run_id=RUN-nope").status_code == 404
    assert client.get("/api/reclaim/attest?run_id=RUN-nope").status_code == 404


def test_bad_now_returns_400(client):
    r = client.post("/api/reclaim/run", json={"now": "not-a-timestamp"})
    assert r.status_code == 400


def test_empty_run_still_persists_receipt(client):
    first = client.post("/api/reclaim/run", json={"now": SCAN_NOW}).json()
    second = client.post("/api/reclaim/run", json={"now": SCAN_NOW}).json()
    assert second["closed_n"] == 0 and second["members"] == []
    view = client.get(f"/api/reclaim/run-members?run_id={second['run_id']}").json()
    assert view["valid"] is True and view["members"] == []
    # 第一趟成员仍可下钻
    old = client.get(f"/api/reclaim/run-members?run_id={first['run_id']}").json()
    assert old["member_count"] == 1
