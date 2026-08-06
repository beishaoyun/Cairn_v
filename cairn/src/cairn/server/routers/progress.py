"""进度子域路由（Agent 24 · skeleton §2.5 尾部 / frontend-progress-view-design §3-§4）。

端点（全部 Bearer 主 token，T）：
- GET  ``/engagements/{eid}/tasks``               任务列表（活动面板；?active=true 看板轮询）
- GET  ``/tasks/{task_run_id}``                   单任务详情（event_count / latest_event）
- GET  ``/tasks/{task_run_id}/events``            增量事件（SSE / 长轮询 / 即时 JSON 三模式）
- POST ``/tasks/{task_run_id}/events/ticket``     SSE 一次性 ticket（EventSource 带不了 Header）
- GET  ``/tasks/{task_run_id}/events/{seq}/raw``  原始分片文件（懒加载）
- GET  ``/engagements/{eid}/timeline``            D3 统一时间线（六源聚合，只读）

SSE/鉴权接线：
- ``GET /tasks/{id}/events`` 已被 auth 中间件豁免（见 ``middlewares/auth.py`` ——
  EventSource 无法携带 Authorization 头）；SSE 模式用一次性 ticket 鉴权（handler 内消费），
  JSON/长轮询模式由 :func:`require_events_auth` 手动校验 Bearer token 或有效 ticket。
- 心跳/长轮询时长读 12 的 tuning（``services.progress.tuning_values``，默认 15s/20s），
  不硬编码。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..db import get_db
from ..errors import CairnError, ErrorCode
from ..models import TaskStatus
from ..services import progress as progress_svc

router = APIRouter(tags=["progress"])

#: SSE 轮询间隔（无新事件时 sleep）；测试可 monkeypatch 缩短
_SSE_POLL_SECONDS = 1.0
#: 长轮询轮询间隔（无新事件时 sleep）
_LONGPOLL_POLL_SECONDS = 0.5
#: SSE 心跳循环上限（测试 seam：生产为 None = 无限流；测试设小值让流可自然结束，便于 read()）
_SSE_MAX_HEARTBEATS: int | None = None

#: SSE 一次性 ticket TTL（秒）
_TICKET_TTL_SECONDS = 5.0
_tickets: dict[str, tuple[str, float]] = {}  # ticket -> (task_run_id, expires_at monotonic)
_ticket_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 一次性 ticket（SSE 鉴权；EventSource 带不了 Header）
# ---------------------------------------------------------------------------


def _issue_ticket(run_id: str) -> str:
    tok = secrets.token_urlsafe(24)
    with _ticket_lock:
        _tickets[tok] = (run_id, time.monotonic() + _TICKET_TTL_SECONDS)
    return tok


def _peek_ticket(tok: str, run_id: str) -> bool:
    """非消耗校验：存在、未过期、绑定 run_id（JSON/长轮询模式接受 ticket 鉴权用）。"""
    with _ticket_lock:
        entry = _tickets.get(tok)
        if entry is None:
            return False
        r, exp = entry
        if time.monotonic() > exp:
            _tickets.pop(tok, None)
            return False
        return r == run_id


def _consume_ticket(tok: str, run_id: str) -> None:
    """一次性消费：取出即删除（只可用一次）；无效/过期/错绑 → 抛错。"""
    with _ticket_lock:
        entry = _tickets.pop(tok, None)
        if entry is None:
            raise CairnError(
                ErrorCode.VALIDATION,
                message="ticket 无效或已使用（一次性）",
                detail={"ticket": tok},
            )
        r, exp = entry
        if time.monotonic() > exp:
            raise CairnError(
                ErrorCode.VALIDATION, message="ticket 已过期", detail={"expires_in": 0}
            )
        if r != run_id:
            raise CairnError(
                ErrorCode.VALIDATION,
                message="ticket 与 task_run 不匹配",
                detail={"task_run_id": run_id},
            )


def _is_sse_request(request: Request, mode: str | None) -> bool:
    if mode == "sse":
        return True
    accept = (request.headers.get("accept") or "").lower()
    return "text/event-stream" in accept


def require_events_auth(request: Request) -> None:
    """events 端点手动鉴权（该路径已豁免 Bearer 中间件）。

    - SSE 模式：ticket 由 handler 消费校验（本依赖不消费，保证错误帧路径清晰）；
    - JSON/长轮询模式：校验 Bearer token 或有效 ticket（非消耗 peek）。
    """
    mode = request.query_params.get("mode")
    if _is_sse_request(request, mode):
        return
    token = request.app.state.config.token()
    auth = (request.headers.get("Authorization") or "")
    if auth.lower().startswith("bearer ") and auth.split(" ", 1)[1].strip() == token:
        return
    ticket = request.query_params.get("ticket")
    run_id = request.path_params.get("task_run_id")
    if ticket and run_id and _peek_ticket(ticket, run_id):
        return
    raise CairnError(
        ErrorCode.AUTH_REQUIRED,
        message="events 端点需要 Bearer token 或有效 ticket",
    )


# ---------------------------------------------------------------------------
# SSE / 长轮询流
# ---------------------------------------------------------------------------


def _sse_frame(ev: dict) -> str:
    payload = json.dumps(ev, ensure_ascii=False)
    return f"event: {ev['kind']}\ndata: {payload}\n\n"


def _sse_events(
    db_path: str,
    run_id: str,
    after_seq: int,
    kind: str | None,
    level: str | None,
    heartbeat: int,
    poll: float,
):
    """SSE 生成器：先补推 after_seq+1.. 存量摘要，再实时推；每 heartbeat 秒发注释心跳。

    运行于 Starlette 线程池（sync generator）；客户端断开 → GeneratorExit，安全退出。
    ``db_path`` 来自 app config —— 生成器自开自关连接，不受请求级依赖（``get_db``）
    teardown 时序影响（StreamingResponse 的 body 在依赖清理之后才消费）。
    """
    from ..db import connect

    conn = None
    last_seq = after_seq
    heartbeat_count = 0
    try:
        conn = connect(db_path)
        for ev in progress_svc.events_after(conn, run_id, last_seq, limit=500, kind=kind, level=level):
            last_seq = ev["seq"]
            yield _sse_frame(ev)
        last_heartbeat = time.monotonic()
        while True:
            new = progress_svc.events_after(conn, run_id, last_seq, limit=500, kind=kind, level=level)
            if new:
                for ev in new:
                    last_seq = ev["seq"]
                    yield _sse_frame(ev)
            if time.monotonic() - last_heartbeat >= heartbeat:
                yield ": heartbeat\n\n"
                heartbeat_count += 1
                last_heartbeat = time.monotonic()
                if _SSE_MAX_HEARTBEATS is not None and heartbeat_count >= _SSE_MAX_HEARTBEATS:
                    return
            else:
                time.sleep(poll)
    except GeneratorExit:  # 客户端断开
        pass
    except Exception:  # noqa: BLE001 —— 流式读失败静默退出，不崩 worker
        return
    finally:
        if conn is not None:
            conn.close()


def _longpoll_events(
    conn: sqlite3.Connection,
    run_id: str,
    after_seq: int,
    limit: int,
    kind: str | None,
    level: str | None,
    hold: int,
) -> list[dict]:
    """长轮询：hold 秒内有新事件即返回，否则 hold 满返回空（前端 §3.3 降级路径）。"""
    deadline = time.monotonic() + max(0, int(hold))
    while True:
        items = progress_svc.events_after(conn, run_id, after_seq, limit, kind=kind, level=level)
        if items:
            return items
        if time.monotonic() >= deadline:
            return []
        time.sleep(_LONGPOLL_POLL_SECONDS)


# ---------------------------------------------------------------------------
# 任务列表 / 详情 / 收尾
# ---------------------------------------------------------------------------


class FinishTaskRequest(BaseModel):
    """任务收尾（12 客户端 path 假设 ``/tasks/{id}/finish``；仅终态）。"""

    model_config = ConfigDict(extra="forbid")

    status: TaskStatus
    outcome_note: str | None = Field(default=None, max_length=2000)


@router.post("/tasks/{task_run_id}/finish")
def finish_task(
    task_run_id: str,
    payload: FinishTaskRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """任务状态收尾（12 客户端 path 假设；skeleton §2.5 无显式写端点，本路由补齐）。"""
    return progress_svc.finish_task_run(
        db, task_run_id, status=payload.status.value, outcome_note=payload.outcome_note
    )


@router.get("/engagements/{eid}/tasks")
def list_tasks(
    eid: str,
    active: bool = Query(default=False, description="true → 仅 queued/running（看板轮询口径）"),
    task_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """任务列表（含 status/worker/duration/event_count/latest_event，前端 §2.1）。"""
    return progress_svc.list_task_runs(
        db,
        eid,
        active=active,
        task_type=task_type,
        status=status,
        offset=offset,
        limit=limit,
    )


@router.get("/tasks/{task_run_id}")
def get_task(task_run_id: str, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """单任务详情（event_count / latest_event / duration_seconds）。"""
    return progress_svc.get_task_run(db, task_run_id)


# ---------------------------------------------------------------------------
# 事件流（SSE / 长轮询 / 即时 JSON）
# ---------------------------------------------------------------------------


@router.get("/tasks/{task_run_id}/events")
def get_events(
    task_run_id: str,
    request: Request,
    after_seq: int = Query(default=0, ge=0, description="增量断点（seq > after_seq）"),
    limit: int = Query(default=200, ge=1, le=1000),
    kind: str | None = Query(default=None, description="按 kind 过滤：step/tool/command/output/status/error"),
    level: str | None = Query(default=None, description="按 level 过滤：debug/info/warn/error"),
    mode: str | None = Query(default=None, description="sse | longpoll；缺省按 Accept 判定，否则即时 JSON"),
    ticket: str | None = Query(default=None, description="SSE 一次性 ticket（POST .../events/ticket 签发）"),
    _auth: None = Depends(require_events_auth),
    db: sqlite3.Connection = Depends(get_db),
) -> Any:
    """增量事件：SSE（text/event-stream，心跳 + after_seq 续传）/ 长轮询（hold ≤20s）/ JSON。"""
    if _is_sse_request(request, mode):
        if not ticket:
            raise CairnError(
                ErrorCode.VALIDATION,
                message="SSE 需要一次性 ticket（POST /tasks/{id}/events/ticket 签发，5s 过期）",
            )
        _consume_ticket(ticket, task_run_id)
        heartbeat, _, _ = progress_svc.tuning_values()
        return StreamingResponse(
            _sse_events(
                request.app.state.config.db_path,
                task_run_id,
                after_seq,
                kind,
                level,
                heartbeat,
                _SSE_POLL_SECONDS,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if mode == "longpoll":
        _, hold, _ = progress_svc.tuning_values()
        items = _longpoll_events(db, task_run_id, after_seq, limit, kind, level, hold)
        return {"items": items, "last_seq": items[-1]["seq"] if items else after_seq}

    items = progress_svc.events_after(db, task_run_id, after_seq, limit, kind=kind, level=level)
    return {"items": items, "last_seq": items[-1]["seq"] if items else after_seq}


@router.post("/tasks/{task_run_id}/events/ticket")
def create_event_ticket(
    task_run_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """签发 SSE 一次性 ticket（5s 过期；EventSource 带不了 Authorization 头）。"""
    progress_svc.get_task_run(db, task_run_id)  # 404 兜底
    tok = _issue_ticket(task_run_id)
    return {"ticket": tok, "expires_in": int(_TICKET_TTL_SECONDS), "task_run_id": task_run_id}


@router.get("/tasks/{task_run_id}/events/{seq}/raw")
def get_event_raw(
    task_run_id: str,
    seq: int,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """懒加载事件原始分片文件（raw_path 相对 logs_root；缺失 → 404）。"""
    data = progress_svc.event_raw(db, task_run_id, seq, logs_root=request.app.state.config.logs_root)
    return Response(content=data["content"], media_type="text/plain")


# ---------------------------------------------------------------------------
# D3 统一时间线
# ---------------------------------------------------------------------------


@router.get("/engagements/{eid}/timeline")
def get_timeline(
    eid: str,
    after_ts: str | None = Query(default=None, description="增量断点：ts > after_ts"),
    limit: int = Query(default=200, ge=1, le=1000),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """D3 六源统一时间线（graph/task/finding/traffic/coverage/report），只读聚合。"""
    return progress_svc.engagement_timeline(db, eid, after_ts=after_ts, limit=limit)
