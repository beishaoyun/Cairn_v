"""范围目标 CRUD 路由（Agent 20 · skeleton §2.2 授权范围）。

GET/POST /engagements/{eid}/targets（T）、PUT/DELETE /engagements/{eid}/targets/{tid}（H）。
删除应用层 gate：仍被未结算 findings/coverage_items 引用 → 409 + 引用清单（human-workflow §2）。
请求体 ``scope`` 为 ``scope_status`` 的兼容别名（12 客户端 create_target 用 ``scope`` 键）。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from ...config import MAX_PAGE_SIZE
from ..db import get_db
from ..models import ScopeStatus, TargetKind
from ..services import scope as scope_svc

router = APIRouter(prefix="/engagements/{eid}/targets", tags=["targets"])


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


class TargetOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    engagement_id: str
    value: str
    kind: TargetKind
    scope_status: ScopeStatus
    criticality: float
    auto_created: int
    note: str | None = None
    added_by: str
    added_at: str


class TargetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)
    scope_status: ScopeStatus = Field(
        default=ScopeStatus.authorized,
        validation_alias=AliasChoices("scope", "scope_status"),
    )
    kind: TargetKind | None = None
    criticality: float = Field(default=0.5, ge=0, le=1)
    note: str | None = None


class TargetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str | None = Field(default=None, min_length=1)
    scope_status: ScopeStatus | None = Field(
        default=None, validation_alias=AliasChoices("scope", "scope_status")
    )
    kind: TargetKind | None = None
    criticality: float | None = Field(default=None, ge=0, le=1)
    note: str | None = None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("", response_model=list[TargetOut])
def list_targets(
    eid: str,
    scope_status: ScopeStatus | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=MAX_PAGE_SIZE),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return scope_svc.list_targets(
        db,
        eid,
        scope_status=scope_status.value if scope_status else None,
        offset=offset,
        limit=limit,
    )


@router.post("", response_model=TargetOut)
def create_target(
    eid: str,
    payload: TargetCreate,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    return scope_svc.create_target(
        db,
        eid,
        value=payload.value,
        scope_status=payload.scope_status.value,
        kind=payload.kind.value if payload.kind else None,
        criticality=payload.criticality,
        note=payload.note,
    )


@router.put("/{tid}", response_model=TargetOut)
def update_target(
    eid: str,
    tid: str,
    payload: TargetUpdate,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    return scope_svc.update_target(
        db,
        eid,
        tid,
        value=payload.value,
        scope_status=payload.scope_status.value if payload.scope_status else None,
        kind=payload.kind.value if payload.kind else None,
        criticality=payload.criticality,
        note=payload.note,
    )


@router.delete("/{tid}", status_code=204)
def delete_target(
    eid: str,
    tid: str,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """删除 gate：未结算引用 → 409；否则删除（DB 层 CASCADE 保持不动）。"""
    scope_svc.delete_target(db, eid, tid)
    return Response(status_code=204)
