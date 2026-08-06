"""捕获/流量路由（skeleton §2.5 traffic；Agent 23 所有）。

端点：
- GET  ``/engagements/{eid}/traffic``              捕获索引/检索（T：Bearer 主 token）
- GET  ``/engagements/{eid}/traffic/{tid}``        还原（T；?for_model=true → digest）
- POST ``/engagements/{eid}/traffic``              代理回写索引（F8：受限代理 token，
        **非 Bearer 主 token**；中间件对该路径豁免，本模块用 ``require_capture_token`` 校验
        ``CAIRN_CAPTURE_TOKEN``）

> 流量关联端点 ``POST /engagements/{id}/findings/{fid}/traffic`` 由 22-findings 的路由
> 注册（模块排序先于本模块，避免重复路由遮蔽）；其 handler 委托本域的
> ``capture.link_finding_traffic``（见 ``services/findings.py`` 委托注释）。本模块不重复注册。

鉴权说明（F8/C5）：捕获代理持受限写 token（env ``CAIRN_CAPTURE_TOKEN``），仅能写
traffic 索引，不触碰 findings/coverage 等写接口；Agent 容器不持任何 Cairn token。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..db import get_db
from ..errors import CairnError, ErrorCode
from ..services import capture as capture_svc

router = APIRouter(tags=["traffic"])


# ---------------------------------------------------------------------------
# F8 代理受限 token 校验
# ---------------------------------------------------------------------------


def require_capture_token(request: Request) -> None:
    """校验捕获代理受限写 token（env ``CAIRN_CAPTURE_TOKEN``，非 Bearer 主 token）。

    对应 dispatch-config-spec §5 ``security.capture_token_env``（默认 ``CAIRN_CAPTURE_TOKEN``）。
    缺/错 → 401 AUTH_REQUIRED/AUTH_INVALID。仅 traffic 写入端点使用。
    """
    expected = os.environ.get("CAIRN_CAPTURE_TOKEN")
    if not expected:
        raise CairnError(ErrorCode.AUTH_REQUIRED, message="未配置 CAIRN_CAPTURE_TOKEN（捕获代理受限 token）")
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise CairnError(ErrorCode.AUTH_REQUIRED, message="缺少 Bearer Authorization 头（代理 token）")
    token = auth.split(" ", 1)[1].strip()
    if not token or token != expected:
        raise CairnError(ErrorCode.AUTH_INVALID, message="捕获代理受限 token 无效")


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


class TrafficIndexRequest(BaseModel):
    """代理回写元数据（F8；DB 只存元数据，字节在 traffic 文件）。"""

    model_config = ConfigDict(extra="forbid")

    captured_at: Optional[str] = None
    method: str = Field(min_length=1, max_length=16)
    url: str = Field(min_length=1, max_length=4096)
    host: Optional[str] = None
    client: Optional[str] = None          # C12：代理由 client_ip 反查的 worker 名；无法区分时 NULL
    client_ip: Optional[str] = None       # C12：来源容器 IP（bridge 独立 IP）
    status: Optional[int] = Field(default=None, ge=100, le=599)
    req_path: str = Field(min_length=1, max_length=1024)
    resp_path: Optional[str] = Field(default=None, max_length=1024)
    req_bytes: int = Field(ge=0)
    resp_bytes: Optional[int] = Field(default=None, ge=0)
    content_type: Optional[str] = None
    sha256: Optional[str] = None          # F2：全量包校验和（分片拼接后）
    chunk_count: int = Field(default=1, ge=1, le=64)
    seq: Optional[int] = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/engagements/{eid}/traffic")
def list_traffic(
    eid: str,
    request: Request,
    client: Optional[str] = Query(default=None),
    since: Optional[str] = Query(default=None),
    host: Optional[str] = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """捕获索引/检索（C12：按 worker 归属过滤；Dispatcher 派发前检索候选 traffic_ids）。"""
    return capture_svc.list_traffic(
        db, eid, client=client, since=since, host=host, offset=offset, limit=limit
    )


@router.get("/engagements/{eid}/traffic/{tid}")
def get_traffic(
    eid: str,
    tid: str,
    request: Request,
    for_model: bool = Query(default=False, description="true → digest（≤digest_budget）；false → 全量"),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """还原原始请求/响应（F2）。全量供报告/审计/replay；digest 只喂模型。"""
    return capture_svc.resolve_traffic(
        db,
        eid,
        tid,
        for_model=for_model,
        traffic_root=request.app.state.config.traffic_root,
    )


@router.post("/engagements/{eid}/traffic")
def index_traffic(
    eid: str,
    payload: TrafficIndexRequest,
    _auth: None = Depends(require_capture_token),
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """捕获索引回写（F8 代理唯一写入口）。Server 唯一 DB 写者。F5 fail-closed。"""
    return capture_svc.index_traffic(db, eid, entry=payload.model_dump())
