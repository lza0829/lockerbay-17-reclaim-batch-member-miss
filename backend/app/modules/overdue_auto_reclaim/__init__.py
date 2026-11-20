"""超时未取自动回收（overdue_auto_reclaim）。

对外定死：
- 对每条 open 占用，以其已落地的 reclaim_deadline_at 与当前时刻比较：
  已到期（deadline <= now）必须关闭占用、格口回 free、留下可查回收记录；
  未到期一律不动。
- reclaim_deadline_at 在开账时物化（seed / ingest 开占用即写入）；
  历史缺账的 open 占用先按 opened_at + 现行 overdue_hours 补写落库再裁，
  已落地的 deadline 绝不按「现行柜规现场重算」绕过。

关账落库形态（两种择一，本题择「占用账不可变关闭」）：
- occupancies 增列 reclaim_deadline_at / closed_reason；到期关账写
  status='reclaimed'、closed_reason='overdue'、closed_at=扫描时刻，
  守卫式 UPDATE 保证关闭字段一旦落库即不可变；不建独立回收关账边表。

趟次成员投影落库形态（两种择一，本题择「独立 reclaim_run_members 表」）：
- 每趟扫描生成 run_id，写入 reclaim_runs（趟次回执）与
  reclaim_run_members（成员投影：run_id、占用 id、关闭时刻等），
  与本趟实关集合同成败固化；成员表 occupancy_id 全表唯一，
  同一占用二次固化直接撞 UNIQUE → 整趟回滚，杜绝重复/半截成员。

成员提交形态（两种择一，本题择「关账同事务写满成员投影，缺一回滚」）：
- 补写、扫描、关账、格口回 free、趟次回执、成员投影全部落在同一事务，
  由调用方一次 commit；任一步抛错（含成员写不满）→ 调用方 rollback，
  不留下孤儿/半截成员行。
- 提交前在同事务内强制四件硬约束，违一即抛错整趟回滚：
  1) 本趟关闭的占用每条都有成员行（禁止回收成功而成员缺行）；
  2) 每条成员对应占用确已关闭（禁止成员含未关闭占用）；
  3) closed_n == 实关数 == 成员数（禁止 closed_n 与成员数分叉）；
  4) 成员集合与本趟实关集合全等（含格口必 free / 被合法再占的既有关账校验）。

核证：
- list_reclaims() 只读输出回收占用明细（占用、格口、deadline、关闭时刻）；
- list_run_members() 只读输出趟次成员投影；
- attest_run() 只读复算趟次成员核证（与占用账对撞，valid=false 即投影分叉）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

STATUS = "implemented"

# 关账落库形态择一：占用账不可变关闭（非独立关账边表）
PERSIST_FORM = "occupancy_closed_reason"
# 趟次成员落库形态择一：独立 reclaim_run_members 表（非批次回执内嵌快照）
MEMBER_PERSIST_FORM = "standalone_reclaim_run_members_table"
# 成员提交形态择一：关账同事务写满成员投影，缺一回滚
MEMBER_COMMIT_PATH = "close_and_project_same_transaction_all_or_none"
# 失败闭路径：扫描与关账同事务提交
TXN_PATH = "scan_and_close_same_transaction"

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
CLOSED_REASON_OVERDUE = "overdue"
STATUS_OCCUPIED = "occupied"
STATUS_RECLAIMED = "reclaimed"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime(TS_FMT)


def new_run_id() -> str:
    return f"RUN-{uuid4().hex[:12]}"


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


def _insert_run_member(conn, member: dict) -> None:
    """单条成员投影落库；独立函数便于故障注入验证整趟回滚。

    _FAULT_INJECTION["fail_next"] 为测试故障开关：置 True 后下一次写入抛错，
    模拟「成员投影写了半截」；抛错后自动复位，重试即恢复正常。
    """
    if _FAULT_INJECTION["fail_next"]:
        _FAULT_INJECTION["fail_next"] = False
        raise RuntimeError("注入故障：成员投影写入中断（半截）")
    conn.execute(
        "INSERT INTO reclaim_run_members"
        "(run_id, occupancy_id, cell_id, tracking_no, reclaim_deadline_at, closed_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            member["run_id"],
            member["occupancy_id"],
            member["cell_id"],
            member["tracking_no"],
            member["reclaim_deadline_at"],
            member["closed_at"],
        ),
    )


# 测试用故障注入开关（生产路径恒为 False）
_FAULT_INJECTION = {"fail_next": False}


def run_scan(conn, *, overdue_hours: int, now: str | None = None) -> dict:
    """扫描并关账：补写 → 按已落地 deadline 裁决 → 关账 → 格口回 free
    → 写趟次回执与成员投影 → 同事务全等核证。

    全程同一事务（调用方负责一次 commit；异常须整体 rollback）。
    now 可注入 ISO-8601 时刻以便夹具对照复现，缺省取当前时刻。
    趟次（含零成员空趟）必落 reclaim_runs；成员与实关集合全等，缺一回滚。
    """
    scan_now = parse_ts(now).strftime(TS_FMT) if now else now_utc()
    run_id = new_run_id()

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

    reclaimed: list[dict] = []
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

    closed_ids = [r["occupancy_id"] for r in reclaimed]

    # 趟次回执先落（成员表外键指向 run_id）；含空趟，closed_n 随实关数固化
    conn.execute(
        "INSERT INTO reclaim_runs(run_id, scanned_at, closed_n, backfilled_deadlines, created_at) "
        "VALUES (?,?,?,?,?)",
        (run_id, scan_now, len(closed_ids), backfilled, now_utc()),
    )

    # 成员投影：一条实关占用一行，写不满（含 UNIQUE 撞键）即抛错整趟回滚
    members = []
    for r in reclaimed:
        member = {
            "run_id": run_id,
            "occupancy_id": r["occupancy_id"],
            "cell_id": r["cell_id"],
            "tracking_no": r["tracking_no"],
            "reclaim_deadline_at": r["reclaim_deadline_at"],
            "closed_at": r["closed_at"],
        }
        _insert_run_member(conn, member)
        members.append(member)

    _assert_run_projection(conn, run_id, scan_now, closed_ids)

    return {
        "run_id": run_id,
        "scanned_at": scan_now,
        "backfilled_deadlines": backfilled,
        "reclaimed_count": len(reclaimed),
        "reclaimed": reclaimed,
        "members": members,
        "member_count": len(members),
        "closed_n": len(members),
    }


def _assert_run_projection(conn, run_id: str, scan_now: str, closed_ids: list[str]) -> None:
    """同事务硬约束核证：投影集合与本趟实关集合全等，违一抛错整趟回滚。"""
    member_rows = conn.execute(
        "SELECT occupancy_id FROM reclaim_run_members WHERE run_id=?",
        (run_id,),
    ).fetchall()
    member_ids = {r["occupancy_id"] for r in member_rows}
    closed_set = set(closed_ids)

    # 禁止回收成功而成员缺行：本趟实关集合必须全部有成员行
    missing = closed_set - member_ids
    if missing:
        raise RuntimeError(
            "回收成员投影缺失（回收成功而成员缺行）: " + ",".join(sorted(missing))
        )
    # 禁止成员含未关闭占用 / 成员越界：成员必须全部是本趟实关且已关占用
    extra = member_ids - closed_set
    if extra:
        raise RuntimeError(
            "回收成员投影含非本趟关闭占用: " + ",".join(sorted(extra))
        )

    # 禁止成员含未关闭占用：逐条与占用账对撞
    open_member = conn.execute(
        "SELECT m.occupancy_id FROM reclaim_run_members m "
        "JOIN occupancies o ON o.id = m.occupancy_id "
        "WHERE m.run_id=? AND (o.status='occupied' OR o.closed_at IS NULL "
        "                       OR o.closed_reason IS NULL) LIMIT 1",
        (run_id,),
    ).fetchone()
    if open_member:
        raise RuntimeError(
            "回收成员投影含未关闭占用: " + open_member["occupancy_id"]
        )

    # 禁止 closed_n 与成员数分叉：趟次回执、实关数、成员数三者相等
    run_row = conn.execute(
        "SELECT closed_n FROM reclaim_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if run_row is None or run_row["closed_n"] != len(closed_ids) or len(member_ids) != len(closed_ids):
        raise RuntimeError(
            f"回收趟次 closed_n 分叉: closed_n={run_row['closed_n'] if run_row else None} "
            f"实关={len(closed_ids)} 成员={len(member_ids)}"
        )

    # 成员关闭时刻须与趟次扫描/关账时刻一致（投影与关账同源）
    bad_ts = conn.execute(
        "SELECT occupancy_id FROM reclaim_run_members WHERE run_id=? AND closed_at<>?",
        (run_id, scan_now),
    ).fetchall()
    if bad_ts:
        raise RuntimeError(
            "回收成员关闭时刻与趟次关账时刻不一致: "
            + ",".join(r["occupancy_id"] for r in bad_ts)
        )


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


def list_runs(conn) -> list[dict]:
    """只读：全部回收趟次回执（新趟在前；同刻以落库 rowid 兜底排序）。"""
    rows = conn.execute(
        "SELECT run_id, scanned_at, closed_n, backfilled_deadlines, created_at "
        "FROM reclaim_runs ORDER BY rowid DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_run(conn, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT run_id, scanned_at, closed_n, backfilled_deadlines, created_at "
        "FROM reclaim_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


def list_run_members(conn, run_id: str | None = None) -> dict:
    """回收趟次成员投影只读接口。

    run_id 缺省取最新一趟；无任何趟次时回空投影（closed_n=0、members=[]）。
    找不到指定 run_id 抛 LookupError，由路由转 404。
    """
    if run_id is None:
        latest = conn.execute(
            "SELECT run_id FROM reclaim_runs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            return {"run_id": None, "closed_n": 0, "member_count": 0, "members": [], "valid": True}
        run_id = latest["run_id"]

    run = _fetch_run(conn, run_id)
    if run is None:
        raise LookupError(f"回收趟次不存在: {run_id}")

    members = [
        dict(r)
        for r in conn.execute(
            "SELECT run_id, occupancy_id, cell_id, tracking_no, reclaim_deadline_at, closed_at "
            "FROM reclaim_run_members WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    ]
    return {
        "run_id": run_id,
        "scanned_at": run["scanned_at"],
        "closed_n": run["closed_n"],
        "member_count": len(members),
        "members": members,
        "valid": run["closed_n"] == len(members),
    }


def attest_run(conn, run_id: str) -> dict:
    """趟次成员核证（只读，基线所无的新接口）：以占用账为真，复算投影全等。

    校验四件硬约束并给出每项判定与总 valid：
    - no_missing_members  本趟实关占用均有成员行（禁止回收成功而成员缺行）
    - no_unclosed_members 成员对应占用均已关闭（禁止成员含未关闭占用）
    - counts_aligned      closed_n == 成员数 == 实关数（禁止数字分叉）
    - set_equal           成员集合与本趟关闭集合全等
    """
    run = _fetch_run(conn, run_id)
    if run is None:
        raise LookupError(f"回收趟次不存在: {run_id}")

    member_rows = conn.execute(
        "SELECT occupancy_id, closed_at FROM reclaim_run_members WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    member_ids = [r["occupancy_id"] for r in member_rows]
    member_set = set(member_ids)

    # 以占用账为真：本趟扫描时刻被 overdue 关闭的占用集合
    closed_rows = conn.execute(
        "SELECT id FROM occupancies "
        "WHERE closed_reason=? AND closed_at=? AND status=?",
        (CLOSED_REASON_OVERDUE, run["scanned_at"], STATUS_RECLAIMED),
    ).fetchall()
    closed_set = {r["id"] for r in closed_rows}

    # 成员是否都已关闭（禁止成员含未关闭占用）
    unclosed = [
        r["occupancy_id"]
        for r in member_rows
        if conn.execute(
            "SELECT 1 FROM occupancies WHERE id=? AND status='occupied'",
            (r["occupancy_id"],),
        ).fetchone()
    ]
    # 成员关闭时刻是否与趟次关账时刻同源
    ts_mismatch = sorted(
        r["occupancy_id"] for r in member_rows if r["closed_at"] != run["scanned_at"]
    )

    no_missing = closed_set <= member_set
    no_unclosed = not unclosed and not ts_mismatch
    counts_aligned = (
        run["closed_n"] == len(member_ids) == len(closed_set)
    )
    set_equal = member_set == closed_set

    checks = {
        "no_missing_members": no_missing,
        "no_unclosed_members": no_unclosed,
        "counts_aligned": counts_aligned,
        "set_equal": set_equal,
    }
    return {
        "run_id": run_id,
        "scanned_at": run["scanned_at"],
        "closed_n": run["closed_n"],
        "member_count": len(member_ids),
        "closed_count": len(closed_set),
        "members": [dict(r) for r in member_rows],
        "missing_member_ids": sorted(closed_set - member_set),
        "extra_member_ids": sorted(member_set - closed_set),
        "unclosed_member_ids": unclosed,
        "closed_at_mismatch_ids": ts_mismatch,
        "checks": checks,
        "valid": all(checks.values()),
    }
