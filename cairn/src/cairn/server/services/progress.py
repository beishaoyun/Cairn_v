"""进度子域服务层（Agent 24 · capture-verify-progress-spec §7 / skeleton §3 progress）。

职责：task_runs / task_events 采集与读取（只增只读，前端/报告无写权限面），SSE/长轮询
数据源，以及 D3 统一时间线的 task 事件源。

关键契约：
- **A. task_runs**：``open_task_run(conn, *, engagement_id, project_id=None, task_type,
  worker)`` —— ``project_id`` 可空（B2：verify/audit/replay 为 engagement 级任务）。
  status 枚举 ``queued/running/success/failed/cancelled/unhealthy/rejected``；ID ``task-###``
  **Dispatcher 侧全局**（DDL §4.1），本模块经 ``counters`` 表（name='task'）自增生成
  （见 :func:`_global_next_id` —— 10 的 ``next_id`` 不含 task/event，故自实现）。
- **B. task_events**：``append_event(run_id, *, kind, level, message, raw_path)`` —— kind ∈
  step/tool/command/output/status/error；level ∈ debug/info/warn/error；``message`` ≤
  ``tuning.event_summary_max_bytes``（512B，超限截断）；原始流落 ``raw_path`` 分片文件
  （懒加载 ``GET /tasks/{id}/events/{seq}/raw``）。ID ``ev-###``（Dispatcher 侧全局，
  同经 ``counters`` 表 name='event'）。只增只读。
- **C. 事件流**：``events_after(run_id, after_seq, limit)`` 增量；``event_raw`` 懒加载原始分片。
- **D3**：``engagement_timeline`` 六源聚合见 ``services/timeline.py``（本模块 re-export，
  保持 skeleton §3 ``services/progress.engagement_timeline`` 签名可用）。

ID 生成线程安全：每请求独立 sqlite3 连接（``get_db``），``UPDATE counters SET
value=value+1 ... RETURNING value`` 在单语句内完成读-改-写，SQLite WAL 单写者串行化
并发 UPDATE（busy_timeout=5000），故并发下无丢失/重复（与 10 的 ``next_id('engagement')``
同一机制）。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from ..db import get_db  # noqa: F401  —— 供下游（30 等）沿用请求级连接模式
from ..errors import CairnError, ErrorCode
from ..models import TaskEventKind, TaskStatus

#: 枚举白名单（值 = DDL CHECK 逐字符，黄金不变量 7）
_TASK_STATUSES = frozenset(s.value for s in TaskStatus)
_EVENT_KINDS = frozenset(k.value for k in TaskEventKind)
_EVENT_LEVELS = frozenset({"debug", "info", "warn", "error"})

#: 摘要入库上限默认值（tuning.event_summary_max_bytes，dispatch-config-spec §7）
DEFAULT_EVENT_SUMMARY_MAX_BYTES = 512
#: SSE 心跳默认（tuning.sse_heartbeat_seconds）
DEFAULT_SSE_HEARTBEAT_SECONDS = 15
#: 长轮询 hold 默认（tuning.longpoll_hold_seconds）
DEFAULT_LONGPOLL_HOLD_SECONDS = 20


def _utcnow() -> str:
    """ISO8601 UTC（黄金不变量 8）。"""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 12 tuning 读取（不硬编码：sse_heartbeat_seconds / longpoll_hold_seconds /
# event_summary_max_bytes 读 ``cairn.dispatcher.config.TuningConfig`` 默认值）
# ---------------------------------------------------------------------------


def tuning_values() -> tuple[int, int, int]:
    """读 12 的 tuning 默认值 → ``(sse_heartbeat_seconds, longpoll_hold_seconds,
    event_summary_max_bytes)``。

    Dispatcher 侧 ``TuningConfig`` dataclass 默认值即权威；Server 不加载 dispatch.yaml，
    故取其 dataclass 默认。若 dispatcher 包不可导入（拆分部署），回退文档默认值
    （15 / 20 / 512，不硬编码于路由层）。
    """
    try:
        from cairn.dispatcher.config import TuningConfig

        t = TuningConfig()
        return (
            int(t.sse_heartbeat_seconds),
            int(t.longpoll_hold_seconds),
            int(t.event_summary_max_bytes),
        )
    except Exception:  # noqa: BLE001 —— dispatcher 未部署时回退文档默认
        return (
            DEFAULT_SSE_HEARTBEAT_SECONDS,
            DEFAULT_LONGPOLL_HOLD_SECONDS,
            DEFAULT_EVENT_SUMMARY_MAX_BYTES,
        )


# ---------------------------------------------------------------------------
# ID 生成（Dispatcher 侧全局，counters 表；DDL §4.1：task-### / ev-###）
# ---------------------------------------------------------------------------


def _global_next_id(conn: sqlite3.Connection, name: str, prefix: str, pad: int = 3) -> str:
    """全局自增 ID（``counters`` 表，非 engagement 作用域）。

    与 10 的 ``next_id('engagement')`` 同一原子机制：``INSERT OR IGNORE`` 种子行 +
    ``UPDATE ... RETURNING``（单语句原子读-改-写）。SQLite WAL 单写者 + busy_timeout
    保证并发安全。
    """
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES (?, 0)", (name,))
    row = conn.execute(
        "UPDATE counters SET value = value + 1 WHERE name = ? RETURNING value",
        (name,),
    ).fetchone()
    if row is None:  # pragma: no cover —— INSERT OR IGNORE 后必然存在
        raise CairnError(
            ErrorCode.INTERNAL, message=f"全局计数器初始化失败: {name}", detail={"name": name}
        )
    return f"{prefix}-{row['value']:0{pad}d}"


# ---------------------------------------------------------------------------
# 读取辅助
# ---------------------------------------------------------------------------


def _require_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="task_run 不存在", detail={"task_run_id": run_id})
    return row


def _require_engagement(conn: sqlite3.Connection, eid: str) -> sqlite3.Row:
    row = conn.execute("SELECT id FROM engagements WHERE id = ?", (eid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"engagement_id": eid})
    return row


def _event_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.setdefault("raw_offset", None)
    return d


# ---------------------------------------------------------------------------
# A. open_task_run —— task_runs 采集入口（Dispatcher/30 上报）
# ---------------------------------------------------------------------------


def open_task_run(
    conn: sqlite3.Connection,
    *,
    engagement_id: str,
    project_id: str | None = None,
    task_type: str,
    worker: str,
    status: str = "queued",
    started_at: str | None = None,
    outcome_note: str | None = None,
) -> dict:
    """打开一条任务运行（前端活动面板的「排队态」即此）。

    - ``project_id`` 可空（B2：verify/audit/replay 不挂 project）；
    - ``task_type`` ∈ bootstrap/reason/explore/verify/audit/replay（skeleton §3 TaskType）；
    - ``status`` 默认 ``queued``（DDL 默认 running 仅兜底，本服务显式给排队态）；
    - ID ``task-###`` 经 ``counters`` 表全局自增（DDL §4.1）。
    """
    _require_engagement(conn, engagement_id)
    task_type = (task_type or "").strip().lower()
    worker = (worker or "").strip()
    if not task_type or not worker:
        raise CairnError(ErrorCode.VALIDATION, message="open_task_run 必填：task_type/worker")
    if status not in _TASK_STATUSES:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知 task status: {status!r}")
    # task_type 无 DB CHECK（DDL §9.5 仅 NOT NULL），此处白名单校验防脏数据
    if task_type not in {"bootstrap", "reason", "explore", "verify", "audit", "replay"}:
        raise CairnError(
            ErrorCode.VALIDATION,
            message=f"未知 task_type: {task_type!r}",
            detail={"allowed": ["bootstrap", "reason", "explore", "verify", "audit", "replay"]},
        )

    run_id = _global_next_id(conn, "task", "task")
    now = started_at or _utcnow()
    conn.execute(
        """INSERT INTO task_runs
           (id, engagement_id, project_id, task_type, worker, status, started_at, finished_at, outcome_note)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
        (run_id, engagement_id, project_id, task_type, worker, status, now, outcome_note),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# B. append_event —— task_events 采集（只增只读）
# ---------------------------------------------------------------------------


def append_event(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    kind: str,
    level: str = "info",
    message: str | None = None,
    raw_path: str | None = None,
    raw_offset: int | None = None,
    ts: str | None = None,
) -> dict:
    """向 task_run 追加一条事件（进度流，前端实时可见）。

    - ``kind`` ∈ step/tool/command/output/status/error（F9 分类在 30/dispatcher，本模块只收）；
    - ``level`` ∈ debug/info/warn/error；
    - ``message`` 摘要 ≤ ``tuning.event_summary_max_bytes``（512B，超限截断）；
    - ``raw_path`` 原始 stdout/stderr 分片文件（相对 logs_root，懒加载）；
    - seq 单调（per run 自增）；ID ``ev-###`` 全局自增（DDL §4.1）。
    """
    _require_run(conn, run_id)
    if kind not in _EVENT_KINDS:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知事件 kind: {kind!r}")
    if level not in _EVENT_LEVELS:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知事件 level: {level!r}")

    summary = message or ""
    if not isinstance(summary, str):
        summary = str(summary)
    _, _, max_bytes = tuning_values()
    # ≤512B 字节级截断（spec §7.2：message ≤ tuning.event_summary_max_bytes；避免 UTF-8 多字节超限）
    encoded = summary.encode("utf-8")
    if len(encoded) > max_bytes:
        summary = encoded[:max_bytes].decode("utf-8", "ignore")

    seq_row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM task_events WHERE task_run_id = ?", (run_id,)
    ).fetchone()
    seq = int(seq_row["m"]) + 1

    ev_id = _global_next_id(conn, "event", "ev")
    now = ts or _utcnow()
    conn.execute(
        """INSERT INTO task_events (id, task_run_id, seq, ts, kind, level, message, raw_path, raw_offset)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ev_id, run_id, seq, now, kind, level, summary, raw_path, raw_offset),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM task_events WHERE id = ?", (ev_id,)).fetchone()
    return dict(row)


def finish_task_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    outcome_note: str | None = None,
) -> dict:
    """任务状态收尾（12 客户端 path 假设 ``/tasks/{id}/finish``；skeleton §2.5 无显式写端点，
    phase0-alignment #1 归 24 实现）。

    仅允许终态 ``success/failed/cancelled/unhealthy/rejected``；置 ``finished_at`` +
    ``outcome_note``。``queued/running`` 拒收（中间态由 open_task_run 建立）。
    """
    row = _require_run(conn, run_id)
    if status not in _TASK_STATUSES:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知 task status: {status!r}")
    if status in ("queued", "running"):
        raise CairnError(
            ErrorCode.VALIDATION,
            message="finish 只能写终态（queued/running 由 open_task_run 建立）",
            detail={"status": status},
        )
    conn.execute(
        "UPDATE task_runs SET status=?, finished_at=?, outcome_note=? WHERE id=?",
        (status, _utcnow(), outcome_note, run_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())


# ---------------------------------------------------------------------------
# C. events_after / event_raw —— 增量读取与原始流懒加载
# ---------------------------------------------------------------------------


def events_after(
    conn: sqlite3.Connection,
    run_id: str,
    after_seq: int = 0,
    limit: int = 100,
    *,
    kind: str | None = None,
    level: str | None = None,
) -> list[dict]:
    """增量事件（SSE 断点续传 / 长轮询 / 前端汇总轮询共用）。

    ``seq > after_seq`` 升序，最多 ``limit`` 条；``kind``/``level`` 可选过滤。
    """
    _require_run(conn, run_id)
    clauses = ["task_run_id = ?", "seq > ?"]
    params: list = [run_id, int(after_seq)]
    if kind is not None:
        if kind not in _EVENT_KINDS:
            raise CairnError(ErrorCode.VALIDATION, message=f"未知事件 kind: {kind!r}")
        clauses.append("kind = ?")
        params.append(kind)
    if level is not None:
        if level not in _EVENT_LEVELS:
            raise CairnError(ErrorCode.VALIDATION, message=f"未知事件 level: {level!r}")
        clauses.append("level = ?")
        params.append(level)
    rows = conn.execute(
        f"SELECT * FROM task_events WHERE {' AND '.join(clauses)} "
        "ORDER BY seq ASC LIMIT ?",
        [*params, max(1, int(limit))],
    ).fetchall()
    return [_event_to_dict(r) for r in rows]


def event_raw(
    conn: sqlite3.Connection,
    run_id: str,
    seq: int,
    *,
    logs_root: str | None = None,
) -> dict:
    """懒加载事件原始分片文件（``GET /tasks/{id}/events/{seq}/raw``）。

    ``raw_path`` 为相对 logs_root 的分片文件（spec §7.2：``logs/{task_run_id}/{seq}.chunk``）。
    ``raw_offset`` 存在时从该字节偏移读取（分片/拼接语义）。文件缺失 → 404（懒加载降级，
    不阻断事件摘要流）。
    """
    _require_run(conn, run_id)
    row = conn.execute(
        "SELECT * FROM task_events WHERE task_run_id = ? AND seq = ?", (run_id, int(seq))
    ).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="事件不存在", detail={"task_run_id": run_id, "seq": seq})
    if not row["raw_path"]:
        raise CairnError(ErrorCode.NOT_FOUND, message="该事件无原始分片文件", detail={"seq": seq})

    root = logs_root
    if root is None:
        from ...config import DEFAULT_LOGS_ROOT

        root = DEFAULT_LOGS_ROOT
    rel = (row["raw_path"] or "").strip()
    # B7 式路径校验：相对、禁绝对/反斜杠/``..`` 穿越
    if (
        not rel
        or rel.startswith("/")
        or "\\" in rel
        or any(p in ("", ".", "..") for p in rel.split("/"))
    ):
        raise CairnError(ErrorCode.VALIDATION, message="raw_path 非法（须相对且无穿越）", detail={"raw_path": rel})
    full = os.path.join(root, rel)
    if not os.path.isfile(full):
        raise CairnError(ErrorCode.NOT_FOUND, message="原始分片文件缺失", detail={"path": rel})

    with open(full, "rb") as fh:
        if row["raw_offset"]:
            fh.seek(int(row["raw_offset"]))
        data = fh.read()
    return {
        "id": row["id"],
        "task_run_id": run_id,
        "seq": row["seq"],
        "raw_path": rel,
        "raw_offset": row["raw_offset"],
        "content": data.decode("utf-8", "replace"),
        "bytes": len(data),
    }


# ---------------------------------------------------------------------------
# 任务列表 / 详情（前端活动面板数据源）
# ---------------------------------------------------------------------------


def _duration_seconds(row: sqlite3.Row) -> float | None:
    """started_at→finished_at 秒数；running 用 now−started_at。"""
    started = row["started_at"]
    if not started:
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    end_s = row["finished_at"]
    if end_s:
        try:
            end = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        end = datetime.now(timezone.utc)
    return max(0.0, round((end - start).total_seconds(), 1))


def _latest_event(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM task_events WHERE task_run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)
    ).fetchone()
    return _event_to_dict(row) if row else None


def _enrich_run(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    d = dict(row)
    d["event_count"] = int(
        conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_run_id = ?", (row["id"],)
        ).fetchone()[0]
    )
    d["duration_seconds"] = _duration_seconds(row)
    latest = _latest_event(conn, row["id"])
    d["latest_event"] = latest
    return d


def get_task_run(conn: sqlite3.Connection, run_id: str) -> dict:
    """单任务详情（含 event_count / latest_event / duration_seconds，前端 §2.1 字段）。"""
    return _enrich_run(conn, _require_run(conn, run_id))


def list_task_runs(
    conn: sqlite3.Connection,
    eid: str,
    *,
    active: bool = False,
    task_type: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> list[dict]:
    """Engagement 任务列表（活动面板 / 看板轮询）。

    ``active=true``：仅 queued/running（活动任务汇总口径，前端 2s 轮询）。
    """
    _require_engagement(conn, eid)
    clauses = ["engagement_id = ?"]
    params: list = [eid]
    if active:
        clauses.append("status IN ('queued', 'running')")
    if task_type is not None:
        clauses.append("task_type = ?")
        params.append(task_type)
    if status is not None:
        if status not in _TASK_STATUSES:
            raise CairnError(ErrorCode.VALIDATION, message=f"未知 task status: {status!r}")
        clauses.append("status = ?")
        params.append(status)
    rows = conn.execute(
        f"SELECT * FROM task_runs WHERE {' AND '.join(clauses)} "
        "ORDER BY CASE WHEN status IN ('queued','running') THEN 0 ELSE 1 END, started_at DESC "
        "LIMIT ? OFFSET ?",
        [*params, max(1, int(limit)), int(offset)],
    ).fetchall()
    return [_enrich_run(conn, r) for r in rows]


# ---------------------------------------------------------------------------
# D3 统一时间线（六源聚合，只读）—— 实现在 services/timeline.py，此处 re-export
# 保持 skeleton §3 ``services/progress.engagement_timeline`` 签名可用。
# ---------------------------------------------------------------------------


def engagement_timeline(
    conn: sqlite3.Connection, eid: str, *, after_ts: str | None = None, limit: int = 200
) -> list[dict]:
    """D3 统一时间线：归并六源（图/task/finding/traffic/coverage/report），按 ts 升序。

    只读聚合，不加新表；实现见 :mod:`cairn.server.services.timeline`。
    """
    from .timeline import engagement_timeline as _impl

    return _impl(conn, eid, after_ts=after_ts, limit=limit)
