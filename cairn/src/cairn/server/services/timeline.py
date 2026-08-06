"""统一时间线聚合服务（Agent 24 · D3 / capture-verify-progress-spec §7.4）。

把散落六处的证据流（图时间线 / task_events / findings 历史 / traffic 捕获 / 覆盖写回 /
报告版本）串成一条**可回放、可审计**的 engagement 级时间轴 —— 报告「方法流程」章节与
人工回溯都从这里聚合。只读聚合，**不加新表**：各事件源已有时间戳，服务端按 ts 归并。

统一结构（不跨源做归一化计算）：
``{ts, source, kind, actor, summary, ref}``

- ``source`` ∈ graph | task | finding | traffic | coverage | report
- ``kind``    源内事件类型（fact_created / step / status_change / captured / waiver / report…）
- ``actor``   操作者（worker / human / generated_by；无则 None）
- ``summary`` 人类可读摘要
- ``ref``     源关联 id（fact/intent/hint / task_event / finding / traffic / coverage_records
  / waiver / audit_run / report）

排序：按 ISO8601 UTC 解析为 epoch 升序（兼容 ``Z`` 与 ``+00:00`` 混合格式 —— 服务端
各处时间戳写入格式不一致，字符串比较不可靠）。``after_ts`` 增量：``ts > after_ts``。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ..errors import CairnError, ErrorCode


def _iso_to_epoch(ts: str | None) -> float:
    """ISO8601 UTC → epoch 秒（兼容 ``Z`` 与 ``+00:00`` 后缀）；解析失败按 0 兜底。"""
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):  # pragma: no cover —— 脏数据兜底
        return 0.0


def _require_engagement(conn: sqlite3.Connection, eid: str) -> None:
    row = conn.execute("SELECT id FROM engagements WHERE id = ?", (eid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"engagement_id": eid})


# ---------------------------------------------------------------------------
# 六源
# ---------------------------------------------------------------------------


def _graph_events(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """图事件源：facts.created_at / intents.concluded_at / hints.created_at。"""
    rows: list[dict] = []
    for r in conn.execute(
        """SELECT f.id, f.created_at, f.description
           FROM facts f JOIN projects p ON p.id = f.project_id
           WHERE p.engagement_id = ?""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["created_at"],
                "source": "graph",
                "kind": "fact_created",
                "actor": None,
                "summary": r["description"],
                "ref": r["id"],
            }
        )
    for r in conn.execute(
        """SELECT i.id, i.concluded_at, i.creator, i.description
           FROM intents i JOIN projects p ON p.id = i.project_id
           WHERE p.engagement_id = ? AND i.concluded_at IS NOT NULL""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["concluded_at"],
                "source": "graph",
                "kind": "intent_concluded",
                "actor": r["creator"],
                "summary": r["description"],
                "ref": r["id"],
            }
        )
    for r in conn.execute(
        """SELECT h.id, h.created_at, h.creator, h.content
           FROM hints h JOIN projects p ON p.id = h.project_id
           WHERE p.engagement_id = ?""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["created_at"],
                "source": "graph",
                "kind": "hint_created",
                "actor": r["creator"],
                "summary": r["content"],
                "ref": r["id"],
            }
        )
    return rows


def _task_events(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """task 事件源：task_events.ts（已按 seq 分片；task_runs 提供 worker/type）。"""
    rows: list[dict] = []
    for r in conn.execute(
        """SELECT te.id, te.ts, te.kind, te.message, te.task_run_id, tr.worker, tr.task_type
           FROM task_events te JOIN task_runs tr ON tr.id = te.task_run_id
           WHERE tr.engagement_id = ?""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["ts"],
                "source": "task",
                "kind": r["kind"],
                "actor": r["worker"],
                "summary": r["message"],
                "ref": r["id"],
                "task_run_id": r["task_run_id"],
                "task_type": r["task_type"],
            }
        )
    return rows


def _finding_events(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """finding 事件源：finding_history（状态流转 + actor）。"""
    rows: list[dict] = []
    for r in conn.execute(
        """SELECT fh.id, fh.created_at, fh.from_status, fh.to_status, fh.note, fh.actor, fh.finding_id
           FROM finding_history fh JOIN findings f ON f.id = fh.finding_id
           WHERE f.engagement_id = ?""",
        (eid,),
    ):
        transition = f"{r['from_status']}→{r['to_status']}" if r["from_status"] else r["to_status"]
        rows.append(
            {
                "ts": r["created_at"],
                "source": "finding",
                "kind": "status_change",
                "actor": r["actor"],
                "summary": r["note"] or transition,
                "ref": r["finding_id"],
                "transition": transition,
            }
        )
    return rows


def _traffic_events(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """traffic 事件源：traffic_entries.captured_at（捕获时间点 + 关联 worker）。"""
    rows: list[dict] = []
    for r in conn.execute(
        """SELECT id, captured_at, client, method, url, status
           FROM traffic_entries WHERE engagement_id = ?""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["captured_at"],
                "source": "traffic",
                "kind": "captured",
                "actor": r["client"],
                "summary": f"{r['method']} {r['url']}"
                + (f" [{r['status']}]" if r["status"] is not None else ""),
                "ref": r["id"],
            }
        )
    return rows


def _coverage_events(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """coverage 事件源：coverage_records + waivers + audit_runs。"""
    rows: list[dict] = []
    for r in conn.execute(
        """SELECT cr.id, cr.created_at, cr.outcome, cr.depth_achieved, cr.item_id, ci.tested_by
           FROM coverage_records cr LEFT JOIN coverage_items ci ON ci.id = cr.item_id
           WHERE cr.engagement_id = ?""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["created_at"],
                "source": "coverage",
                "kind": "coverage_result",
                "actor": r["tested_by"],
                "summary": f"coverage {r['depth_achieved']} → {r['outcome']}",
                "ref": r["id"],
                "item_id": r["item_id"],
            }
        )
    for r in conn.execute(
        """SELECT id, created_at, kind, reason, created_by, item_id
           FROM waivers WHERE engagement_id = ?""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["created_at"],
                "source": "coverage",
                "kind": f"waiver:{r['kind']}",
                "actor": r["created_by"],
                "summary": r["reason"],
                "ref": r["id"],
                "item_id": r["item_id"],
            }
        )
    for r in conn.execute(
        """SELECT id, created_at, reason, verdict, auditor, coverage_item_id
           FROM audit_runs WHERE engagement_id = ?""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["created_at"],
                "source": "coverage",
                "kind": f"audit:{r['reason']}",
                "actor": r["auditor"],
                "summary": r["verdict"] or r["reason"],
                "ref": r["id"],
                "item_id": r["coverage_item_id"],
            }
        )
    return rows


def _report_events(conn: sqlite3.Connection, eid: str) -> list[dict]:
    """report 事件源：reports.created_at。"""
    rows: list[dict] = []
    for r in conn.execute(
        """SELECT id, created_at, format, generated_by FROM reports WHERE engagement_id = ?""",
        (eid,),
    ):
        rows.append(
            {
                "ts": r["created_at"],
                "source": "report",
                "kind": "report",
                "actor": r["generated_by"],
                "summary": f"{r['format']} report generated",
                "ref": r["id"],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def engagement_timeline(
    conn: sqlite3.Connection,
    eid: str,
    *,
    after_ts: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """D3 统一时间线：六源归并，按 ts 升序，``limit`` 截断，``after_ts`` 增量。

    - 只读聚合，不加新表；
    - 每源取（ts, actor, 摘要, 关联 id），不跨源做归一化计算；
    - 排序/``after_ts`` 比较用 epoch（兼容 Z / +00:00 混合格式）；
    - 报告「方法流程」章节（41）与前端时间轴 Tab（42）都从这里取数。
    """
    _require_engagement(conn, eid)
    rows: list[dict] = []
    rows += _graph_events(conn, eid)
    rows += _task_events(conn, eid)
    rows += _finding_events(conn, eid)
    rows += _traffic_events(conn, eid)
    rows += _coverage_events(conn, eid)
    rows += _report_events(conn, eid)

    if after_ts is not None and after_ts != "":
        after_epoch = _iso_to_epoch(after_ts)
        rows = [r for r in rows if _iso_to_epoch(r.get("ts")) > after_epoch]

    rows.sort(key=lambda r: _iso_to_epoch(r.get("ts")))
    return rows[: max(1, int(limit))]
