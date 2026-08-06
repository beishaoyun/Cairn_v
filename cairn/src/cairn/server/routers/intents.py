"""探索图 intent 路由（skeleton §2.4 / exploration-graph-spec §5；Agent 25）。

- ``POST /projects/{pid}/intents``：创建 intent（goal 禁 from/to、worker∈{null,creator}）
- ``POST /projects/{pid}/intents/{iid}/claim|heartbeat``：认领/心跳（首次心跳即认领）
- ``POST /projects/{pid}/intents/{iid}/release``：释放（仅持有者）
- ``POST /projects/{pid}/intents/{iid}/conclude``：**三子域编排**——
  facts 写图（本服务）+ coverage_result 转发 21 ``services.coverage.write_coverage_result``
  + findings[] 转发 22 ``services.findings.create_finding``（agent 只能 open）。**同请求同事务**。

21/22 服务经 import 守卫接入（本仓库已就绪；缺失时留 TODO 跳过并打 warning）。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from ..db import get_db
from ..services import graph as svc

# 21/22 服务（conclude 三子域编排；import 守卫 + TODO 兜底）
try:
    from ..services import coverage as _coverage_svc
except ImportError:  # pragma: no cover —— 21 未就绪时兜底
    _coverage_svc = None

try:
    from ..services import findings as _findings_svc
except ImportError:  # pragma: no cover —— 22 未就绪时兜底
    _findings_svc = None

logger = logging.getLogger("cairn.server.routers.intents")

router = APIRouter(prefix="/projects/{pid}/intents", tags=["intents"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class IntentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    creator: str
    from_fact_ids: list[str] = []
    to_fact_id: str | None = None
    worker: str | None = None  # 可选：创建时只能为 null 或 == creator（spec §4-2）


class LeaseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker: str


class ConcludeIn(BaseModel):
    """conclude 三子域编排载荷（spec §5）。

    ``facts`` 为字符串或 ``{description}`` 列表；``coverage_result``/``findings``
    原样透传 21/22 服务（字段校验在服务层）。
    """

    model_config = ConfigDict(extra="forbid")

    worker: str
    facts: list[Any] | None = None
    coverage_result: dict[str, Any] | None = None
    findings: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
def create_intent(
    pid: str,
    payload: IntentIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    intent = svc.create_intent(
        db, pid,
        description=payload.description,
        creator=payload.creator,
        from_fact_ids=payload.from_fact_ids,
        to_fact_id=payload.to_fact_id,
        worker=payload.worker,
    )
    db.commit()
    return intent


@router.post("/{iid}/claim", status_code=204)
def claim_intent(
    pid: str,
    iid: str,
    payload: LeaseIn,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """认领 intent（12 客户端 claim_intent 路径假设；他人持有 → 409 LEASE_CONFLICT）。"""
    svc.claim_intent(db, pid, iid, worker=payload.worker)
    db.commit()
    return Response(status_code=204)


@router.post("/{iid}/heartbeat", status_code=204)
def heartbeat_intent(
    pid: str,
    iid: str,
    payload: LeaseIn,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """intent 心跳；**首次心跳即认领**（worker=NULL → 置为请求者，spec §5）。"""
    svc.heartbeat_intent(db, pid, iid, worker=payload.worker)
    db.commit()
    return Response(status_code=204)


@router.post("/{iid}/release", status_code=204)
def release_intent(
    pid: str,
    iid: str,
    payload: LeaseIn,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    svc.release_intent(db, pid, iid, worker=payload.worker)
    db.commit()
    return Response(status_code=204)


@router.post("/{iid}/conclude", status_code=204)
def conclude_intent(
    pid: str,
    iid: str,
    payload: ConcludeIn,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """conclude 三子域编排（spec §5）：facts 写图 + coverage_result 转发 21 + findings 转发 22。

    同请求同事务：任一子域写失败 → 整请求抛错、不 commit（连接关闭回滚），Dispatcher
    侧按 403/409 语义静默收场（spec §4-7/8）。
    """
    # 1) 图子域：写 facts（只增）+ concluded_at + 释放 intent 租约
    result = svc.conclude_intent(db, pid, iid, worker=payload.worker, facts=payload.facts)

    project = svc.get_project(db, pid)
    eid = project["engagement_id"] if project else None

    # 2) 覆盖子域（21）：转发 coverage_result（B1 认领校验在 21 内部）
    if eid and payload.coverage_result is not None:
        if _coverage_svc is None:
            # TODO(21)：services.coverage.write_coverage_result 未就绪，conclude 覆盖写回被跳过
            logger.warning("conclude: services.coverage 未就绪，跳过 coverage_result 转发 pid=%s", pid)
        else:
            cr = payload.coverage_result
            fact_id = cr.get("fact_id")
            if fact_id is None and result.get("fact_ids"):
                # 溯源：默认关联本次产出的首个 fact（只溯源，不阻塞）
                fact_id = result["fact_ids"][0]
            # 字段缺省走 21 服务校验（VALIDATION），避免裸 KeyError → 500
            _coverage_svc.write_coverage_result(
                db, eid,
                item_ids=cr.get("item_ids") or [],
                depth_achieved=cr.get("depth_achieved"),
                outcome=cr.get("outcome"),
                fact_id=fact_id,
                intent_id=cr.get("intent_id") or iid,
                evidence_refs=cr.get("evidence_refs"),
                tested_scope=cr.get("tested_scope"),
                partial=bool(cr.get("partial", False)),
            )

    # 3) 漏洞子域（22）：findings[] 转发（agent 只能 open；source_fact_id 溯源本次关键 fact）
    if eid and payload.findings:
        if _findings_svc is None:
            # TODO(22)：services.findings.create_finding 未就绪，conclude findings 转发被跳过
            logger.warning("conclude: services.findings 未就绪，跳过 findings 转发 pid=%s", pid)
        else:
            for item in payload.findings:
                fd = dict(item)
                if fd.get("source_fact_id") is None and result.get("fact_ids"):
                    fd["source_fact_id"] = result["fact_ids"][0]
                _findings_svc.create_finding(
                    db, eid,
                    payload=fd,
                    detected_by=payload.worker,
                    actor="agent",
                )

    db.commit()
    return Response(status_code=204)
