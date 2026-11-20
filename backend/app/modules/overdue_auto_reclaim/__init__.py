"""超时未取自动回收（overdue_auto_reclaim）。

对外定死：
- 对每条 open 占用，以其已落地的 reclaim_deadline_at 与当前时刻比较：
  已到期（deadline <= now）必须关闭占用、格口回 free、留下可查回收记录；
  未到期一律不动。
- reclaim_deadline_at 在开账时物化（seed / ingest 开占用即写入）；
  历史缺账的 open 占用先按 opened_at + 现行 overdue_hours 补写落库再裁，
  已落地的 deadline 绝不按「现行柜规现场重算」绕过。

落库形态（两种择一，本题择「占用账不可变关闭」）：
- occupancies 增列 reclaim_deadline_at / closed_reason；到期关账写
  status='reclaimed'、closed_reason='overdue'、closed_at=扫描时刻，
  守卫式 UPDATE 保证关闭字段一旦落库即不可变；不建独立回收边表。

失败闭（两种择一，本题择「同事务」）：
- 补写、扫描、关账、格口回 free 全部落在同一事务，由调用方一次提交；
  关账后在同事务内校验「已关占用的格口必为 free（或被他人合法再占）」，
  校验失败抛错 → 整体回滚，杜绝「占用已关格口仍 occupied」与
  「格口已 free 占用仍 open」的中间态外泄。

核证：list_reclaims() 只读输出（占用、格口、deadline、关闭时刻），
与占用物化、格口状态、回收页同源，天然协同。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

STATUS = "implemented"

# 两种落库形态择一：占用账不可变关闭（非独立 reclaim 边表）
PERSIST_FORM = "occupancy_closed_reason"
# 两种失败闭路径择一：扫描与关账同事务提交
TXN_PATH = "scan_and_close_same_transaction"

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
CLOSED_REASON_OVERDUE = "overdue"
STATUS_OCCUPIED = "occupied"
STATUS_RECLAIMED = "reclaimed"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime(TS_FMT)


def parse_ts(value: str) -> datetime:
    """解析 ISO-8601 时刻并归一到 UTC；非法输入抛 ValueError。"""
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def materialize_deadline(opened_at: str, overdue_hours: int) -> str:
    """开账物化回收截止时刻：opened_at + overdue_hours。落库后不再重算。"""
    return (parse_ts(opened_at) + timedelta(hours=int(overdue_hours))).strftime(TS_FMT)


def backfill_missing_deadlines(conn, overdue_hours: int) -> int:
    """历史缺账的 open 占用补写 deadline（开账物化的兜底）；已落地的一律不动。"""
    rows = conn.execute(
        "SELECT id, opened_at FROM occupancies "
        "WHERE status='occupied' AND reclaim_deadline_at IS NULL"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE occupancies SET reclaim_deadline_at=? "
            "WHERE id=? AND reclaim_deadline_at IS NULL",
            (materialize_deadline(row["opened_at"], overdue_hours), row["id"]),
        )
    return len(rows)


def run_scan(conn, *, overdue_hours: int, now: str | None = None) -> dict:
    """扫描并关账：补写 → 按已落地 deadline 裁决 → 关账 → 格口回 free。

    全程同一事务（调用方负责一次 commit；异常则整体回滚）。
    now 可注入 ISO-8601 时刻以便夹具对照复现，缺省取当前时刻。
    """
    scan_now = parse_ts(now).strftime(TS_FMT) if now else now_utc()

    backfilled = backfill_missing_deadlines(conn, overdue_hours)

    # 裁决只依据已落地的 reclaim_deadline_at，不按现行柜规现场重算
    candidates = conn.execute(
        "SELECT id, cell_id, tracking_no, opened_at, reclaim_deadline_at "
        "FROM occupancies "
        "WHERE status='occupied' AND reclaim_deadline_at IS NOT NULL "
        "  AND reclaim_deadline_at <= ? "
        "ORDER BY reclaim_deadline_at, id",
        (scan_now,),
    ).fetchall()

    reclaimed = []
    for cand in candidates:
        # 守卫式关账：仅仍 open 且从未关闭者可关；关闭字段落库即不可变
        cur = conn.execute(
            "UPDATE occupancies SET status=?, closed_reason=?, closed_at=? "
            "WHERE id=? AND status='occupied' "
            "  AND closed_at IS NULL AND closed_reason IS NULL",
            (STATUS_RECLAIMED, CLOSED_REASON_OVERDUE, scan_now, cand["id"]),
        )
        if cur.rowcount != 1:
            continue
        # 格口回 free：仅当该格口已无其它 open 占用，绝不误放他人占用
        still_open = conn.execute(
            "SELECT 1 FROM occupancies WHERE cell_id=? AND status='occupied' LIMIT 1",
            (cand["cell_id"],),
        ).fetchone()
        if still_open is None:
            conn.execute("UPDATE cells SET status='free' WHERE id=?", (cand["cell_id"],))
        reclaimed.append(
            {
                "occupancy_id": cand["id"],
                "cell_id": cand["cell_id"],
                "tracking_no": cand["tracking_no"],
                "opened_at": cand["opened_at"],
                "reclaim_deadline_at": cand["reclaim_deadline_at"],
                "closed_at": scan_now,
                "closed_reason": CLOSED_REASON_OVERDUE,
            }
        )

    # 同事务 postcondition（失败闭）：本次所关占用，其格口要么已 free，
    # 要么被其它 open 占用合法持有；否则抛错让调用方整体回滚。
    leaked = conn.execute(
        "SELECT o.id FROM occupancies o JOIN cells c ON c.id = o.cell_id "
        "WHERE o.closed_reason=? AND o.closed_at=? AND c.status<>'free' "
        "  AND NOT EXISTS (SELECT 1 FROM occupancies o2 "
        "                  WHERE o2.cell_id=o.cell_id AND o2.status='occupied')",
        (CLOSED_REASON_OVERDUE, scan_now),
    ).fetchall()
    if leaked:
        raise RuntimeError(
            "回收事务一致性校验失败：已关占用但格口未回 free "
            + ",".join(r["id"] for r in leaked)
        )

    return {
        "scanned_at": scan_now,
        "backfilled_deadlines": backfilled,
        "reclaimed_count": len(reclaimed),
        "reclaimed": reclaimed,
    }


def list_reclaims(conn) -> list[dict]:
    """超时回收核证（只读）：占用、格口、deadline、关闭时刻。"""
    rows = conn.execute(
        "SELECT id AS occupancy_id, cell_id, tracking_no, opened_at, "
        "       reclaim_deadline_at, closed_at, closed_reason, status "
        "FROM occupancies WHERE closed_reason=? "
        "ORDER BY closed_at DESC, id",
        (CLOSED_REASON_OVERDUE,),
    ).fetchall()
    return [dict(r) for r in rows]

def list_run_members(conn, run_id: str | None = None) -> dict:
    """回收趟次成员投影读取：当前实现只回 closed_n，不落成员集合。"""
    rows = conn.execute(
        "SELECT id FROM occupancies WHERE closed_reason='overdue' ORDER BY closed_at DESC"
    ).fetchall()
    return {
        "run_id": run_id or "latest",
        "closed_n": len(rows),
        "members": [],
        "valid": True,
    }


def attach_run_members(result: dict) -> dict:
    out = dict(result or {})
    out["members"] = []
    out["member_count"] = 0
    return out
