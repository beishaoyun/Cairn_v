"""Dispatcher 面向的写路由（Agent 40 补齐 · phase0-alignment #15/#19）。

24 的服务层已就绪（``services.progress.open_task_run`` / ``append_event`` /
``finish_task_run``，``services.scope.expire_engagements``，``scheduler_state`` DDL 已建），
但 REST 写路由形态待 30/40 联调时定 —— 本模块补齐 Dispatcher 主循环需要的三组端点：

- ``POST /engagements/{eid}/task_runs``：打开一条 task_run（12 客户端 ``open_task_run``
  路径假设；服务层 ``services.progress.open_task_run``）。
- ``POST /tasks/{run_id}/events``：追加 task 事件（12 客户端 ``append_event`` 路径假设；
  服务层 ``services.progress.append_event``）。
- ``GET|PUT|DELETE /scheduler_state[/{key}]``：调度状态落库/回载（DDL §7；
  C8 计数 / worker 冷却 / reason_checkpoints / runtime_project_ids 持久化）。
- ``POST /engagements/expire``：授权窗口到期自动 pause（v2 §9.1；服务层
  ``services.scope.expire_engagements``）。

鉴权：全局 Bearer 中间件统一拦截（CairnClient 每请求带 Bearer token；C5 不受影响——
这是 Dispatcher ↔ Server 之间，不是 Agent 容器）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from ..db import get_db
from ..errors import CairnError, ErrorCode
from ..models import TaskEventKind, TaskStatus
from ..services import progress as progress_svc
from ..services import scope as scope_svc

router = APIRouter(tags=["dispatch"])

#: task_events.kind 白名单（DDL CHECK 逐字符，黄金不变量 7）
_EVENT_KINDS = frozenset(k.value for k in TaskEventKind)
#: task_events.level 白名单
_EVENT_LEVELS = frozenset({"debug", "info", "warn", "error"})
#: task_runs.status 白名单
_TASK_STATUSES = frozenset(s.value for s in TaskStatus)

_svc_note = "服务层已就绪（24）；本模块只补 REST 写路由"


def _utcnow() -> str:
    """ISO8601 UTC（黄金不变量 8）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OpenTaskRunIn(BaseModel):
    """POST /engagements/{eid}/task_runs 请求体（对齐 12 客户端 open_task_run）。"""

    model_config = ConfigDict(extra="forbid")

    task_type: str = Field(min_length=1)
    worker: str = Field(min_length=1)
    project_id: str | None = None
    status: str | None = None  # 缺省 queued


class AppendEventIn(BaseModel):
    """POST /tasks/{run_id}/events 请求体（对齐 12 客户端 append_event）。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    level: str = "info"
    message: str | None = None
    raw_path: str | None = None


class SchedulerStateIn(BaseModel):
    """PUT /scheduler_state/{key} 请求体。``value`` 存原始 JSON 字符串（DDL §7 value TEXT）。"""

    model_config = ConfigDict(extra="forbid")

    value: str


# ---------------------------------------------------------------------------
# task_runs / task_events 写路由（24 服务层就绪，此处补齐 REST 形态）
# ---------------------------------------------------------------------------


@router.post("/engagements/{eid}/task_runs", status_code=201)
def open_task_run(
    eid: str,
    payload: OpenTaskRunIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """打开一条 task_run（默认 queued；24 服务层。``_svc_note``）。"""
    if payload.status is not None and payload.status not in _TASK_STATUSES:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知 task status: {payload.status!r}")
    return progress_svc.open_task_run(
        db,
        engagement_id=eid,
        project_id=payload.project_id,
        task_type=payload.task_type,
        worker=payload.worker,
        status=payload.status or "queued",
    )


@router.post("/tasks/{run_id}/events", status_code=201)
def append_event(
    run_id: str,
    payload: AppendEventIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """追加 task 事件（24 服务层）。kind/level 白名单在服务层二次校验，此处先软检。"""
    if payload.kind not in _EVENT_KINDS:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知事件 kind: {payload.kind!r}")
    if payload.level not in _EVENT_LEVELS:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知事件 level: {payload.level!r}")
    return progress_svc.append_event(
        db,
        run_id,
        kind=payload.kind,
        level=payload.level,
        message=payload.message,
        raw_path=payload.raw_path,
    )


# ---------------------------------------------------------------------------
# scheduler_state 落库 / 回载（DDL §7；C8 计数 / worker 冷却 / 运行时项目）
# ---------------------------------------------------------------------------


@router.get("/scheduler_state")
def list_scheduler_state(
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """列出全部调度状态（key → value JSON 字符串）。"""
    rows = db.execute(
        "SELECT key, value, updated_at FROM scheduler_state ORDER BY key"
    ).fetchall()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.get("/scheduler_state/{key}")
def get_scheduler_state(
    key: str,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """读取单个调度状态 key。"""
    row = db.execute(
        "SELECT key, value, updated_at FROM scheduler_state WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="调度状态 key 不存在", detail={"key": key})
    return dict(row)


@router.put("/scheduler_state/{key}")
def put_scheduler_state(
    key: str,
    payload: SchedulerStateIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Upsert 调度状态（key → value JSON 字符串；updated_at 刷新）。"""
    now = _utcnow()
    db.execute(
        "INSERT INTO scheduler_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, payload.value, now),
    )
    db.commit()
    row = db.execute(
        "SELECT key, value, updated_at FROM scheduler_state WHERE key = ?", (key,)
    ).fetchone()
    return dict(row)


@router.delete("/scheduler_state/{key}", status_code=204)
def delete_scheduler_state(
    key: str,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    db.execute("DELETE FROM scheduler_state WHERE key = ?", (key,))
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 授权窗口到期自动 pause（v2 §9.1 / B5）
# ---------------------------------------------------------------------------


@router.post("/engagements/expire")
def expire_engagements(
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """窗口到期自动 pause（B5 释放租约）。返回处理数。"""
    n = scope_svc.expire_engagements(db)
    db.commit()
    return {"expired": n}


@router.post("/engagements/{eid}/capture/reconcile")
def capture_reconcile(
    eid: str,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """C2 捕获完整性对账（23 服务层；结果落 scheduler_state key ``capture_gap:{eid}``）。"""
    from ..services import capture as capture_svc

    result = capture_svc.reconcile(db, eid)
    db.commit()
    return result
