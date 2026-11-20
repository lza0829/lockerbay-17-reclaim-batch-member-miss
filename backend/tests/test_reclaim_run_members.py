"""趟次成员投影硬约束测试（夹具：到期 OCC-CAB-A-02-E2 + 未到期 OCC-CAB-B-01-E4）。"""

import pytest

from app.db import connect
from app.modules import overdue_auto_reclaim as svc
from app.seed import overdue_hours

SCAN_NOW = "2026-11-20T12:00:00Z"
OVERDUE_OCC = "OCC-CAB-A-02-E2"
FRESH_OCC = "OCC-CAB-B-01-E4"


def _open_occupancy_ids(conn):
    return {
        r["id"]
        for r in conn.execute("SELECT id FROM occupancies WHERE status='occupied'").fetchall()
    }


def _overdue_member_count(conn):
    return conn.execute(
        "SELECT COUNT(*) AS c FROM occupancies WHERE closed_reason='overdue'"
    ).fetchone()["c"]


# ---------- 基线夹具：到期关、未到期不动，成员与实关全等 ----------

def test_scan_closes_only_overdue_and_projects_members(isolated_db):
    conn = connect()
    try:
        result = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()

        assert result["reclaimed_count"] == 1
        assert result["member_count"] == 1
        assert result["closed_n"] == 1
        closed = [r["occupancy_id"] for r in result["reclaimed"]]
        assert closed == [OVERDUE_OCC]
        member_ids = [m["occupancy_id"] for m in result["members"]]
        assert member_ids == closed  # 投影集合与实关集合全等

        # 占用账：到期已关、未到期仍 open
        assert dict(conn.execute(
            "SELECT status, closed_reason, closed_at FROM occupancies WHERE id=?",
            (OVERDUE_OCC,),
        ).fetchone())["status"] == "reclaimed"
        assert FRESH_OCC in _open_occupancy_ids(conn)
        # 格口回 free；未到期格口仍 occupied
        assert dict(conn.execute(
            "SELECT status FROM cells WHERE id='CAB-A-02'").fetchone())["status"] == "free"
        assert dict(conn.execute(
            "SELECT status FROM cells WHERE id='CAB-B-01'").fetchone())["status"] == "occupied"

        # 趟次回执与成员投影落库
        run = dict(conn.execute(
            "SELECT * FROM reclaim_runs WHERE run_id=?", (result["run_id"],)).fetchone())
        assert run["closed_n"] == 1
        members = conn.execute(
            "SELECT * FROM reclaim_run_members WHERE run_id=?", (result["run_id"],)
        ).fetchall()
        assert len(members) == 1
        m = dict(members[0])
        assert m["occupancy_id"] == OVERDUE_OCC
        assert m["closed_at"] == result["scanned_at"]
        assert m["run_id"] == result["run_id"]
    finally:
        conn.close()


def test_run_member_read_back_equals_closed_set(isolated_db):
    """只读成员接口：成员集合与实关集合全等，closed_n 不分叉。"""
    conn = connect()
    try:
        result = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()
        run_id = result["run_id"]

        view = svc.list_run_members(conn, run_id)
        assert view["run_id"] == run_id
        assert view["closed_n"] == 1
        assert view["member_count"] == 1
        assert view["valid"] is True
        assert [m["occupancy_id"] for m in view["members"]] == [OVERDUE_OCC]

        # 缺省取最新一趟
        latest = svc.list_run_members(conn)
        assert latest["run_id"] == run_id and latest["member_count"] == 1

        # 指定不存在的 run_id → LookupError（路由转 404）
        with pytest.raises(LookupError):
            svc.list_run_members(conn, "RUN-ghost")
    finally:
        conn.close()


def test_attest_run_valid_on_aligned_projection(isolated_db):
    """新增只读核证接口：全等时四项检查全过。"""
    conn = connect()
    try:
        result = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()
        report = svc.attest_run(conn, result["run_id"])
        assert report["valid"] is True
        assert report["checks"] == {
            "no_missing_members": True,
            "no_unclosed_members": True,
            "counts_aligned": True,
            "set_equal": True,
        }
        assert report["missing_member_ids"] == []
        assert report["extra_member_ids"] == []
        assert report["closed_n"] == report["member_count"] == report["closed_count"] == 1
        with pytest.raises(LookupError):
            svc.attest_run(conn, "RUN-ghost")
    finally:
        conn.close()


# ---------- 第二趟：空趟也固化，closed_n=0 不分叉 ----------

def test_second_empty_run_persists_zero_members(isolated_db):
    conn = connect()
    try:
        first = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()
        second = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()

        assert first["run_id"] != second["run_id"]
        assert second["reclaimed_count"] == 0
        assert second["member_count"] == 0 and second["closed_n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM reclaim_runs").fetchone()["c"] == 2
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM reclaim_run_members WHERE run_id=?",
            (second["run_id"],),
        ).fetchone()["c"] == 0

        view = svc.list_run_members(conn, second["run_id"])
        assert view["closed_n"] == 0 and view["members"] == [] and view["valid"] is True
        # 最新趟即空趟
        assert svc.list_run_members(conn)["run_id"] == second["run_id"]
        # 第一趟成员仍可下钻，互不串
        assert svc.list_run_members(conn, first["run_id"])["member_count"] == 1
    finally:
        conn.close()


# ---------- 故障注入：半截写成员失败 → 整趟回滚，无孤儿成员/半关账 ----------

def test_member_write_failure_rolls_back_whole_run(isolated_db):
    conn = connect()
    try:
        before_runs = conn.execute("SELECT COUNT(*) AS c FROM reclaim_runs").fetchone()["c"]
        before_members = conn.execute(
            "SELECT COUNT(*) AS c FROM reclaim_run_members").fetchone()["c"]
        before_open = _open_occupancy_ids(conn)
        before_cells = {
            r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM cells").fetchall()
        }

        # 故障注入：下一条成员投影写入抛错（模拟半截），函数内自动复位
        svc._FAULT_INJECTION["fail_next"] = True
        with pytest.raises(RuntimeError, match="注入故障"):
            svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.rollback()
        assert svc._FAULT_INJECTION["fail_next"] is False

        # 整趟回滚：趟次回执、成员、关账、格口全部回到趟前
        assert conn.execute("SELECT COUNT(*) AS c FROM reclaim_runs").fetchone()["c"] == before_runs
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM reclaim_run_members").fetchone()["c"] == before_members
        assert _open_occupancy_ids(conn) == before_open
        after_cells = {
            r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM cells").fetchall()
        }
        assert after_cells == before_cells
        # 到期占用仍是 open，没有半关账
        assert OVERDUE_OCC in _open_occupancy_ids(conn)
        row = dict(conn.execute(
            "SELECT closed_at, closed_reason, status FROM occupancies WHERE id=?",
            (OVERDUE_OCC,),
        ).fetchone())
        assert row["closed_at"] is None and row["closed_reason"] is None
        assert row["status"] == "occupied"
    finally:
        conn.close()


def test_member_write_failure_then_retry_succeeds_cleanly(isolated_db):
    """回滚后重跑须干净成功，且不产生重复成员（UNIQUE 兜底）。"""
    conn = connect()
    try:
        svc._FAULT_INJECTION["fail_next"] = True
        with pytest.raises(RuntimeError):
            svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.rollback()

        result = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()
        assert result["reclaimed_count"] == 1 and result["member_count"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM reclaim_run_members").fetchone()["c"] == 1
        assert _overdue_member_count(conn) == 1
    finally:
        conn.close()


# ---------- 占用唯一归属：同一占用不可能进两个趟次 ----------

def test_occupancy_can_belong_to_only_one_run(isolated_db):
    conn = connect()
    try:
        svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()
        # 手工再塞一条同占用成员 → UNIQUE 撞键
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO reclaim_run_members"
                "(run_id, occupancy_id, cell_id, tracking_no, reclaim_deadline_at, closed_at) "
                "SELECT ?, id, cell_id, tracking_no, reclaim_deadline_at, closed_at "
                "FROM occupancies WHERE id=?",
                ("RUN-duplicate", OVERDUE_OCC),
            )
    finally:
        conn.close()


# ---------- 只读核证对脏数据的检出（四硬约束被违反时 valid=false） ----------

def _commit_one_run(conn):
    result = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
    conn.commit()
    return result["run_id"]


def test_attest_detects_missing_member(isolated_db):
    """禁止回收成功而成员缺行：删掉成员行 → no_missing_members/set_equal 失败。"""
    conn = connect()
    try:
        run_id = _commit_one_run(conn)
        conn.execute("DELETE FROM reclaim_run_members WHERE run_id=?", (run_id,))
        conn.execute("UPDATE reclaim_runs SET closed_n=1 WHERE run_id=?", (run_id,))
        conn.commit()
        report = svc.attest_run(conn, run_id)
        assert report["valid"] is False
        assert report["checks"]["no_missing_members"] is False
        assert report["checks"]["set_equal"] is False
        assert report["missing_member_ids"] == [OVERDUE_OCC]
    finally:
        conn.close()


def test_attest_detects_unclosed_member(isolated_db):
    """禁止成员含未关闭占用：把占用账改回 open → no_unclosed_members 失败。"""
    conn = connect()
    try:
        run_id = _commit_one_run(conn)
        conn.execute(
            "UPDATE occupancies SET status='occupied', closed_at=NULL, closed_reason=NULL "
            "WHERE id=?",
            (OVERDUE_OCC,),
        )
        conn.commit()
        report = svc.attest_run(conn, run_id)
        assert report["valid"] is False
        assert report["checks"]["no_unclosed_members"] is False
        assert report["unclosed_member_ids"] == [OVERDUE_OCC]
    finally:
        conn.close()


def test_attest_detects_closed_n_divergence(isolated_db):
    """禁止 closed_n 与成员数分叉：篡改回执计数 → counts_aligned 失败。"""
    conn = connect()
    try:
        run_id = _commit_one_run(conn)
        conn.execute("UPDATE reclaim_runs SET closed_n=9 WHERE run_id=?", (run_id,))
        conn.commit()
        report = svc.attest_run(conn, run_id)
        assert report["valid"] is False
        assert report["checks"]["counts_aligned"] is False
        # 集合本身仍全等
        assert report["checks"]["set_equal"] is True
    finally:
        conn.close()


def test_attest_detects_extra_member(isolated_db):
    """成员越界（含未被本趟关闭的占用）→ set_equal 失败。"""
    conn = connect()
    try:
        run_id = _commit_one_run(conn)
        conn.execute(
            "INSERT INTO reclaim_run_members"
            "(run_id, occupancy_id, cell_id, tracking_no, reclaim_deadline_at, closed_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, FRESH_OCC, "CAB-B-01", "TN2001", "2026-11-22T06:00:00Z", SCAN_NOW),
        )
        conn.commit()
        report = svc.attest_run(conn, run_id)
        assert report["valid"] is False
        assert report["checks"]["set_equal"] is False
        assert report["extra_member_ids"] == [FRESH_OCC]
    finally:
        conn.close()


def test_list_runs_returns_all_runs_newest_first(isolated_db):
    conn = connect()
    try:
        first = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()
        second = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=SCAN_NOW)
        conn.commit()
        runs = svc.list_runs(conn)
        assert [r["run_id"] for r in runs] == [second["run_id"], first["run_id"]]
        assert runs[0]["closed_n"] == 0 and runs[1]["closed_n"] == 1
    finally:
        conn.close()


def test_no_runs_returns_empty_projection(isolated_db):
    conn = connect()
    try:
        view = svc.list_run_members(conn)
        assert view == {
            "run_id": None, "closed_n": 0, "member_count": 0, "members": [], "valid": True,
        }
        assert svc.list_runs(conn) == []
    finally:
        conn.close()


# ---------- 协同：装载（ingest 开占用物化 deadline）→ 回收扫描闸 → 多趟成员各自全等 ----------

def test_ingest_then_staged_reclaim_each_run_aligned(isolated_db, monkeypatch):
    """装载 TN1003 后，两次扫描各关各的：成员投影始终与本趟实关集合全等。"""
    from app.services import ingest as ingest_svc

    # 让 ingest 服务的 connect 命中隔离库
    monkeypatch.setattr("app.services.ingest.connect", connect)
    batch = ingest_svc.run_batch()
    assert batch["accepted"] == 1
    new_occ = "OCC-CAB-A-03-E5"  # drop 2026-11-19T02:00 → deadline 2026-11-21T02:00

    conn = connect()
    try:
        # 第一趟 2026-11-20T12:00：E2 到期；E5 未到期不动
        r1 = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now="2026-11-20T12:00:00Z")
        conn.commit()
        assert [m["occupancy_id"] for m in r1["members"]] == [OVERDUE_OCC]
        assert new_occ in _open_occupancy_ids(conn)

        # 第二趟 2026-11-21T12:00：E5 到期，独立成趟，成员只含 E5
        r2 = svc.run_scan(conn, overdue_hours=overdue_hours(conn), now="2026-11-21T12:00:00Z")
        conn.commit()
        assert r2["reclaimed_count"] == 1
        assert [m["occupancy_id"] for m in r2["members"]] == [new_occ]

        # 两趟成员互不串、各自全等，同一占用不会重复进趟
        assert svc.attest_run(conn, r1["run_id"])["valid"] is True
        assert svc.attest_run(conn, r2["run_id"])["valid"] is True
        all_members = {
            r["occupancy_id"]
            for r in conn.execute("SELECT occupancy_id FROM reclaim_run_members").fetchall()
        }
        assert all_members == {OVERDUE_OCC, new_occ}
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM reclaim_run_members").fetchone()["c"] == 2
        # 两个已关格口均回 free，未到期的 CAB-B-01 仍 occupied
        free_cells = {
            r["id"] for r in conn.execute(
                "SELECT id FROM cells WHERE status='free'").fetchall()
        }
        assert {"CAB-A-02", "CAB-A-03"} <= free_cells
        assert dict(conn.execute(
            "SELECT status FROM cells WHERE id='CAB-B-01'").fetchone())["status"] == "occupied"
    finally:
        conn.close()
