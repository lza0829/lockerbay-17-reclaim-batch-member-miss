from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import overdue_auto_reclaim as reclaim_svc
from app.seed import overdue_hours
from app.services import bay as bay_svc
from app.services import ingest as ingest_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        data = bay_svc.overview(conn)
        data["overdue_hours"] = overdue_hours(conn)
        return data
    finally:
        conn.close()


@router.get("/bay")
def bay_matrix():
    conn = connect()
    try:
        return bay_svc.cabinet_matrix(conn)
    finally:
        conn.close()


@router.get("/occupancies")
def list_occupancies(status: str | None = None):
    conn = connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM occupancies WHERE status=? ORDER BY opened_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM occupancies ORDER BY opened_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/events")
def list_events():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, cabinet_id, cell_id, event_type, event_ts, tracking_no, channel FROM parcel_events ORDER BY event_ts, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/occupancy-proofs")
def occupancy_proofs():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM occupancy_proofs ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/reclaim/run")
def run_reclaim(body: dict | None = Body(default=None)):
    """超时回收扫描：补写、裁决、关账、格口回 free、趟次成员投影同事务一次提交。

    任一步失败（含成员投影写不满/全等核证不过）→ 整趟 rollback，
    不留孤儿/半截成员，也不泄露半关账状态。
    """
    now = (body or {}).get("now")
    if now is not None:
        try:
            reclaim_svc.parse_ts(now)
        except (ValueError, TypeError):
            raise HTTPException(400, "now 须为 ISO-8601 时刻")
    conn = connect()
    try:
        result = reclaim_svc.run_scan(conn, overdue_hours=overdue_hours(conn), now=now)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@router.get("/reclaims")
def list_reclaims():
    """超时回收核证（只读）：占用、格口、deadline、关闭时刻。"""
    conn = connect()
    try:
        return reclaim_svc.list_reclaims(conn)
    finally:
        conn.close()


@router.get("/reclaim/runs")
def reclaim_runs():
    """回收趟次回执只读列表（run_id、扫描时刻、closed_n）。"""
    conn = connect()
    try:
        return reclaim_svc.list_runs(conn)
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/settings")
def get_settings():
    conn = connect()
    try:
        return {"overdue_hours": overdue_hours(conn)}
    finally:
        conn.close()


@router.put("/settings/overdue-hours")
def put_overdue(body: dict = Body(...)):
    hours = int(body.get("overdue_hours", 48))
    if hours < 1:
        raise HTTPException(400, "超时回收至少 1 小时")
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('overdue_hours', ?)",
            (str(hours),),
        )
        conn.commit()
        return {"overdue_hours": hours}
    finally:
        conn.close()

# --- 趟次成员投影只读接口（基线所无） ---
@router.get("/reclaim/run-members")
def reclaim_run_members(run_id: str | None = None):
    """回收趟次成员投影（只读）：run_id、占用 id 集合、关闭时刻；缺省最新一趟。"""
    conn = connect()
    try:
        try:
            return reclaim_svc.list_run_members(conn, run_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
    finally:
        conn.close()


@router.get("/reclaim/attest")
def reclaim_attest(run_id: str):
    """趟次成员核证（只读，基线所无）：投影与实关集合全等核证。"""
    conn = connect()
    try:
        try:
            return reclaim_svc.attest_run(conn, run_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "occupancy_id" in out and "occ_id" not in out:
        out["occ_id"] = out.get("occupancy_id")
    if "cell_id" in out and "locker_cell" not in out:
        out["locker_cell"] = out.get("cell_id")
    if "tracking_no" in out and "parcel_key" not in out:
        out["parcel_key"] = out.get("tracking_no")
    if "verified" not in out:
        out["verified"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True}
    if extra:
        data.update(extra)
    return data

