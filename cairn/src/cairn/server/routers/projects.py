"""探索图项目路由（skeleton §2.4 / exploration-graph-spec §5；Agent 25）。

- ``GET/POST /projects``：列表（可过滤）/创建（播种 origin+goal）
- ``GET/DELETE /projects/{pid}``：详情（含 facts/intents/hints 摘要）/物理级联删除
- ``PUT /projects/{pid}/title`` / ``/status``：标题 / 状态（active|stopped，A2 无 completed）
- ``POST /projects/{pid}/reason/claim|heartbeat|release``：项目级 reason 租约

``GET /projects/{pid}/export`` 由 ``routers/export.py`` 提供（独立模块，见交付范围）。
读前（list/get）先跑超时清理（spec §3 注释：读到的即清理后状态）。

鉴权：全局 Bearer 中间件统一拦截（skeleton §2.4 的 H 语义靠业务规则落实）。
``GET /projects`` 目前被 10 的 auth 中间件豁免（占位期遗留，见 phase0-alignment #7），
本路由不重复加路由级鉴权以免破坏 ``test_server_foundation`` 的豁免断言。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from ..db import get_db
from ..errors import CairnError, ErrorCode
from ..models import ProjectStatus
from ..services import graph as svc

router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_id: str
    title: str
    bootstrap_enabled: bool = True


class TitleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ProjectStatus


class ReasonIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker: str


def _detail(db: sqlite3.Connection, pid: str) -> dict[str, Any]:
    project = svc.get_project(db, pid)
    if project is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="project 不存在", detail={"project_id": pid})
    return {
        **project,
        "facts": svc.list_facts(db, pid),
        "intents": svc.list_intents(db, pid, open_only=False),
        "hints": svc.list_hints(db, pid),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.get("")
def list_projects(
    engagement_id: str | None = None,
    status: ProjectStatus | None = None,
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """项目列表（可 ``?engagement_id=`` / ``?status=`` 过滤）。读前先跑超时清理。"""
    svc.intent_timeout_cleanup(db)
    svc.reason_timeout_cleanup(db)
    db.commit()
    return svc.list_projects(
        db,
        engagement_id=engagement_id,
        status=status.value if status is not None else None,
    )


@router.post("", status_code=201)
def create_project(
    payload: ProjectIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """创建项目（播种 origin/goal 特殊事实；ID 走 scoped_counters）。"""
    project = svc.create_project(
        db,
        engagement_id=payload.engagement_id,
        title=payload.title,
        bootstrap_enabled=payload.bootstrap_enabled,
    )
    db.commit()
    return project


@router.get("/{pid}")
def get_project(
    pid: str,
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """项目详情 + facts/intents/hints 摘要。读前先跑超时清理。"""
    svc.intent_timeout_cleanup(db, pid=pid)
    svc.reason_timeout_cleanup(db, pid=pid)
    db.commit()
    return _detail(db, pid)


@router.delete("/{pid}", status_code=204)
def delete_project(
    pid: str,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    svc.delete_project(db, pid)
    db.commit()
    return Response(status_code=204)


@router.put("/{pid}/title")
def set_project_title(
    pid: str,
    payload: TitleIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    project = svc.set_project_title(db, pid, payload.title)
    db.commit()
    return project


@router.put("/{pid}/status")
def set_project_status(
    pid: str,
    payload: StatusIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """置 active|stopped（A2 无 completed）；stopped 即冻结租约（B5）。"""
    project = svc.set_project_status(db, pid, payload.status.value)
    db.commit()
    return project


# ---------------------------------------------------------------------------
# 项目级 reason 租约
# ---------------------------------------------------------------------------


@router.post("/{pid}/reason/claim", status_code=204)
def claim_reason(
    pid: str,
    payload: ReasonIn,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    svc.claim_reason(db, pid, worker=payload.worker)
    db.commit()
    return Response(status_code=204)


@router.post("/{pid}/reason/heartbeat", status_code=204)
def heartbeat_reason(
    pid: str,
    payload: ReasonIn,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    svc.heartbeat_reason(db, pid, worker=payload.worker)
    db.commit()
    return Response(status_code=204)


@router.post("/{pid}/reason/release", status_code=204)
def release_reason(
    pid: str,
    payload: ReasonIn,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    svc.release_reason(db, pid, worker=payload.worker)
    db.commit()
    return Response(status_code=204)
