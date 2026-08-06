"""Engagement 生命周期 + 熔断 + scope 守卫查询路由（Agent 20 · skeleton §2.2）。

路由：GET/POST /engagements、GET/PUT/DELETE /engagements/{id}、
PUT /engagements/{id}/status、POST /engagements/{id}/kill、
GET /engagements/{id}/scope/check（12 客户端路径假设，对齐 scope.check）、
POST /engagements/{id}/finalize（由 Agent 41 实现：覆盖策略校验 + 置 completed + 生成报告）。

鉴权列（D2）：T/H 均为同一 Bearer（服务端中间件统一拦截，本模块不重复鉴权）；
``H`` 仅为「设计上应由人工操作」的语义标注。
"""

from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ...config import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from ..db import get_db
from ..errors import CairnError, ErrorCode
from ..models import EngagementStatus
from ..services import report as report_svc
from ..services import scope as scope_svc

router = APIRouter(prefix="/engagements", tags=["engagements"])


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


class EngagementOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: EngagementStatus
    authorized_start_at: str | None = None
    authorized_end_at: str | None = None
    scope_policy: dict = {}
    kill_switch: int
    created_by: str
    created_at: str
    completed_at: str | None = None


class EngagementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    authorized_start_at: str | None = None
    authorized_end_at: str | None = None
    scope_policy: dict | None = None


class EngagementUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1)
    authorized_start_at: str | None = None
    authorized_end_at: str | None = None
    scope_policy: dict | None = None


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EngagementStatus
    retest: bool = False


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@router.get("", response_model=list[EngagementOut])
def list_engagements(
    status: EngagementStatus | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return scope_svc.list_engagements(
        db,
        status=status.value if status else None,
        offset=offset,
        limit=limit,
    )


@router.post("", response_model=EngagementOut)
def create_engagement(
    payload: EngagementCreate,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    return scope_svc.create_engagement(
        db,
        title=payload.title,
        window_start=payload.authorized_start_at,
        window_end=payload.authorized_end_at,
        scope_policy=payload.scope_policy,
        created_by="human",
    )


@router.get("/{eid}", response_model=EngagementOut)
def get_engagement(eid: str, db: sqlite3.Connection = Depends(get_db)) -> dict:
    eng = scope_svc.get_engagement(db, eid)
    if eng is None:
        raise CairnError(
            ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"id": eid}
        )
    return eng


@router.put("/{eid}", response_model=EngagementOut)
def update_engagement(
    eid: str,
    payload: EngagementUpdate,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    return scope_svc.update_engagement(
        db,
        eid,
        title=payload.title,
        authorized_start_at=payload.authorized_start_at,
        authorized_end_at=payload.authorized_end_at,
        scope_policy=payload.scope_policy,
    )


@router.delete("/{eid}", status_code=204)
def delete_engagement(eid: str, db: sqlite3.Connection = Depends(get_db)) -> None:
    scope_svc.delete_engagement(db, eid)


# ---------------------------------------------------------------------------
# 状态机 / 熔断 / scope 守卫
# ---------------------------------------------------------------------------


@router.put("/{eid}/status", response_model=EngagementOut)
def update_status(
    eid: str,
    payload: StatusUpdate,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    return scope_svc.transition_status(
        db, eid, payload.status.value, retest=payload.retest
    )


@router.post("/{eid}/kill", response_model=EngagementOut)
def kill_engagement(eid: str, db: sqlite3.Connection = Depends(get_db)) -> dict:
    """项目熔断（v2 §4.12 / C1）。置 kill_switch=1；立即 SIGKILL 语义由
    Dispatcher（40/11）依据该标志落实（取消运行任务、停容器、拒新派发）。
    engagement 不存在 → 404（set_kill_switch 内部校验）。"""
    return scope_svc.set_kill_switch(db, eid, True)


@router.get("/{eid}/scope/check")
def scope_check(
    eid: str,
    value: str,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """运行时 scope guard（12 客户端路径假设，对齐 ``scope.check_scope_allowed``）。

    - authorized 命中（含 auto_created）→ 200 + target；
    - prohibited / 未命中 → 403 ``SCOPE_DENIED``（fail-closed，无 fallback）。
    """
    target = scope_svc.check_scope_allowed(db, eid, value)
    if target is None:
        raise CairnError(
            ErrorCode.SCOPE_DENIED,
            message=f"目标 {value!r} 不在授权范围",
            detail={"target_value": value},
        )
    return target


@router.post("/{eid}/finalize")
def finalize_engagement(
    eid: str,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """人工收尾（H：仅人工；Agent 41 实现，替换 20 留的 501 占位）。

    - 前置校验：21 ``report_ready``（高优先格到 required depth + 覆盖率 ≥95% +
      剩余全豁免 + findings 分诊完成）；不达标 → 409 ``COVERAGE_POLICY_UNMET`` +
      明细（豁免后可重试）。
    - 达标 → Engagement 置 completed → 自动生成报告（markdown + html）。

    双重「仅人工」：H 语义标注 + 业务 gate（覆盖收敛 + 置 completed 均非 Agent
    可达）；实际鉴权由 C5（Agent 容器不持 token）落实。
    """
    cfg = request.app.state.config
    reports_root = os.path.join(os.path.dirname(cfg.db_path) or ".", "reports")
    return report_svc.finalize(
        db,
        eid,
        generated_by="human",
        traffic_root=cfg.traffic_root,
        reports_root=reports_root,
    )
