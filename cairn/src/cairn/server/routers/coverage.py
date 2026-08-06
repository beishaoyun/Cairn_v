"""覆盖度子域路由（skeleton §2.3 + Dispatcher 写回/认领端点；Agent 21）。

- 矩阵/热力图：``GET /engagements/{id}/coverage``（§4.1 数据契约；A3 实时 priority）
- 覆盖项：``GET/POST /items``、``PUT /items/{cid}``（调整深度/校准）
- 豁免：``POST /items/{cid}/waive``（B4：仅人工 + 必填 reason）
- 缺口：``GET /gaps``（compute_gaps JSON，priority 降序；reason 输入）
- 写回：``POST /result``（explore coverage_result；Idempotency-Key 头，C9）
- 抽样复核：``GET /audit``、``POST /items/{cid}/audit``（F3）
- B1 认领/释放：``POST /items/{cid}/claim|release``（Dispatcher 30/40 派发前认领、失败回退）
- 导出：``GET /export``（覆盖矩阵 + 豁免理由/审计）

``POST /engagements/{id}/finalize`` 由 41-report-finalize 实现（本路由不含）。
鉴权：全局 Bearer 中间件统一拦截（skeleton §2.3 的 H 语义靠「Agent 不持 token」落实）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..db import get_db, next_id
from ..errors import CairnError, ErrorCode
from ..models import (
    AuditVerdict,
    CoverageItemStatus,
    CoverageOutcome,
    Page,
    PageResult,
    SeedSource,
    TestDepth,
    WaiverKind,
    pagination_params,
)
from ..services.coverage import (
    apply_audit_verdict,
    claim_item_for_intent,
    compute_gaps,
    coverage_summary,
    priority_score,
    release_item_for_intent,
    upsert_coverage_item,
    utcnow,
    waive_item,
    write_coverage_result,
)

router = APIRouter(prefix="/engagements/{engagement_id}/coverage", tags=["coverage"])

#: Dispatcher 写回幂等键头（12 client 同值；coverage_records 以 (item_id, intent_id) 去重）
_IDEMPOTENCY_HEADER = "Idempotency-Key"


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class CoverageItemIn(BaseModel):
    """人工播种覆盖项。"""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    test_type_id: str
    depth: TestDepth | None = None
    seed_source: SeedSource = SeedSource.human


class CoverageItemUpdate(BaseModel):
    """调整深度 / 强制校准（人工）。coverage_items 无 note 列，故仅支持 depth/status。"""

    model_config = ConfigDict(extra="forbid")

    depth_required: TestDepth | None = None
    status: CoverageItemStatus | None = None


class WaiveIn(BaseModel):
    """人工豁免（kind + 必填 reason）。"""

    model_config = ConfigDict(extra="forbid")

    kind: WaiverKind
    reason: str = Field(min_length=1)
    by: str = "human"


class ClaimIn(BaseModel):
    """B1 格子认领/释放。"""

    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(min_length=1)


class CoverageResultIn(BaseModel):
    """explore 写回（coverage spec §3.2）。"""

    model_config = ConfigDict(extra="forbid")

    item_ids: list[str] = Field(min_length=1)
    depth_achieved: TestDepth
    outcome: CoverageOutcome
    fact_id: str | None = None
    intent_id: str = Field(min_length=1)
    evidence_refs: list[str] | None = None
    tested_scope: Any = None
    partial: bool = False


class AuditIn(BaseModel):
    """手动触发/确认抽样复核（F3）。verdict 空 = 仅建 pending audit_run。"""

    model_config = ConfigDict(extra="forbid")

    verdict: AuditVerdict | None = None
    auditor: str | None = None
    reason: str = "manual"
    depth_reached: TestDepth | None = None
    note: str | None = None


def _require_engagement(db: sqlite3.Connection, eid: str) -> None:
    if db.execute("SELECT 1 FROM engagements WHERE id=?", (eid,)).fetchone() is None:
        raise CairnError(ErrorCode.NOT_FOUND, message=f"engagement 不存在: {eid}")


def _require_item(db: sqlite3.Connection, eid: str, cid: str) -> sqlite3.Row:
    item = db.execute(
        "SELECT * FROM coverage_items WHERE id=? AND engagement_id=?", (cid, eid)
    ).fetchone()
    if item is None:
        raise CairnError(ErrorCode.NOT_FOUND, message=f"覆盖项不存在: {cid}")
    return item


# ---------------------------------------------------------------------------
# 矩阵 + 热力图数据（coverage spec §4.1；A3 实时 priority）
# ---------------------------------------------------------------------------


@router.get("")
def get_coverage_matrix(
    engagement_id: str, db: sqlite3.Connection = Depends(get_db)
) -> dict:
    """矩阵 + 热力图数据。``priority`` 一律实时计算（A3），不读缓存列。"""
    _require_engagement(db, engagement_id)
    targets = [
        {"id": r["id"], "value": r["value"], "criticality": r["criticality"]}
        for r in db.execute(
            "SELECT id, value, criticality FROM targets WHERE engagement_id=?", (engagement_id,)
        )
    ]
    test_types = [
        {"id": r["id"], "name": r["name"], "category": r["category"], "risk": r["risk"]}
        for r in db.execute(
            "SELECT id, name, category, risk FROM test_types "
            "WHERE engagement_id=? AND enabled=1",
            (engagement_id,),
        )
    ]
    cells = []
    for r in db.execute(
        """
        SELECT ci.id, ci.target_id, ci.test_type_id, ci.status, ci.depth_required,
               ci.priority_score, ci.last_result, ci.tested_at, ci.retest_round,
               t.criticality, tt.risk,
               (SELECT cr.partial FROM coverage_records cr
                WHERE cr.item_id = ci.id ORDER BY cr.created_at DESC LIMIT 1) AS partial
        FROM coverage_items ci
        JOIN targets t    ON ci.target_id = t.id
        JOIN test_types tt ON ci.test_type_id = tt.id
        WHERE ci.engagement_id = ?
        """,
        (engagement_id,),
    ):
        prio = priority_score(r["criticality"], r["risk"], r["depth_required"])
        cells.append({
            "item_id": r["id"], "target_id": r["target_id"], "test_type_id": r["test_type_id"],
            "status": r["status"], "priority": round(prio, 3), "depth_required": r["depth_required"],
            "last_result": r["last_result"], "tested_at": r["tested_at"],
            "partial": bool(r["partial"]), "retest_round": r["retest_round"],
        })
    return {"targets": targets, "test_types": test_types, "cells": cells,
            "summary": coverage_summary(db, engagement_id)}


# ---------------------------------------------------------------------------
# 覆盖项列表 / 人工播种 / 校准
# ---------------------------------------------------------------------------


@router.get("/items")
def list_coverage_items(
    engagement_id: str,
    status: CoverageItemStatus | None = None,
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """覆盖项列表（12 client 期望裸 list）。可选 ``?status=`` 过滤。"""
    _require_engagement(db, engagement_id)
    if status is None:
        rows = db.execute(
            "SELECT * FROM coverage_items WHERE engagement_id=? ORDER BY created_at",
            (engagement_id,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM coverage_items WHERE engagement_id=? AND status=? ORDER BY created_at",
            (engagement_id, status.value),
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/items")
def create_coverage_item(
    engagement_id: str,
    payload: CoverageItemIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """人工播种覆盖项（幂等 upsert）。"""
    _require_engagement(db, engagement_id)
    item = upsert_coverage_item(
        db, engagement_id, payload.target_id, payload.test_type_id,
        payload.depth.value if payload.depth is not None else None,
        seed_source=payload.seed_source.value,
    )
    db.commit()
    return dict(item)


@router.put("/items/{cid}")
def update_coverage_item(
    engagement_id: str,
    cid: str,
    payload: CoverageItemUpdate,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """调整深度 / 强制校准（人工）。"""
    _require_engagement(db, engagement_id)
    _require_item(db, engagement_id, cid)
    sets: list[str] = []
    params: list[Any] = []
    if payload.depth_required is not None:
        sets.append("depth_required=?")
        params.append(payload.depth_required.value)
    if payload.status is not None:
        sets.append("status=?")
        params.append(payload.status.value)
    if sets:
        db.execute(f"UPDATE coverage_items SET {', '.join(sets)} WHERE id=?", params + [cid])
        db.commit()
    return dict(_require_item(db, engagement_id, cid))


@router.post("/items/{cid}/waive")
def waive_coverage_item(
    engagement_id: str,
    cid: str,
    payload: WaiveIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """人工豁免（B4：kind + 必填 reason；not_applicable 建 waiver 才置状态）。"""
    _require_engagement(db, engagement_id)
    w = waive_item(db, engagement_id, cid, kind=payload.kind.value, reason=payload.reason, by=payload.by)
    db.commit()
    return dict(w)


# ---------------------------------------------------------------------------
# 缺口清单（reason 输入；priority 降序）
# ---------------------------------------------------------------------------


@router.get("/gaps")
def list_gaps(
    engagement_id: str,
    threshold: float = Query(default=0.0, ge=0.0),
    exclude_in_progress: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    """缺口清单（compute_gaps JSON，priority 降序；12 client 期望裸 list）。

    reason 消费须传 ``exclude_in_progress=true``（B1）。
    """
    _require_engagement(db, engagement_id)
    return compute_gaps(
        db, engagement_id,
        threshold=threshold, exclude_in_progress=exclude_in_progress, limit=limit,
    )


# ---------------------------------------------------------------------------
# 写回（C9/B1：Dispatcher explore coverage_result）
# ---------------------------------------------------------------------------


@router.post("/result")
def write_coverage_result_route(
    engagement_id: str,
    payload: CoverageResultIn,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """explore 覆盖写回。携带 ``Idempotency-Key`` 头防重放（(item_id, intent_id) 去重，C9）。

    B1：``current_intent_id != intent_id`` → 409 COVERAGE_ALREADY_COVERED（预期分支，
    写回作废 + release，下轮 reason 重排）。``outcome=not_applicable`` 只建议不置状态（B4）。
    """
    _require_engagement(db, engagement_id)
    _idempotency_key = request.headers.get(_IDEMPOTENCY_HEADER)  # noqa: F841 —— 已成功时客户端重试用
    write_coverage_result(
        db, engagement_id,
        item_ids=payload.item_ids,
        depth_achieved=payload.depth_achieved.value,
        outcome=payload.outcome.value,
        fact_id=payload.fact_id,
        intent_id=payload.intent_id,
        evidence_refs=payload.evidence_refs,
        tested_scope=payload.tested_scope,
        partial=payload.partial,
    )
    db.commit()
    return {"ok": True, "engagement_id": engagement_id, "covered_items": payload.item_ids}


# ---------------------------------------------------------------------------
# B1 认领/释放（Dispatcher 30/40 派发前认领、失败/超时回退；服务端唯一写者）
# ---------------------------------------------------------------------------


@router.post("/items/{cid}/claim")
def claim_coverage_item(
    engagement_id: str,
    cid: str,
    payload: ClaimIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """B1 格子认领：untested 且未被认领 → 置 in_progress + current_intent_id。

    已被他人认领返回 200 ``{"claimed": false}``（并发下预期分支，不派发，下轮换格）。
    """
    _require_engagement(db, engagement_id)
    _require_item(db, engagement_id, cid)
    claimed = claim_item_for_intent(db, cid, payload.intent_id)
    db.commit()
    return {"item_id": cid, "intent_id": payload.intent_id, "claimed": claimed}


@router.post("/items/{cid}/release")
def release_coverage_item(
    engagement_id: str,
    cid: str,
    payload: ClaimIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """B1 格子释放：仅 ``current_intent_id == intent_id`` 才回退 untested（NULL 不放行）。"""
    _require_engagement(db, engagement_id)
    _require_item(db, engagement_id, cid)
    release_item_for_intent(db, cid, payload.intent_id)
    db.commit()
    return {"item_id": cid, "intent_id": payload.intent_id, "released": True}


# ---------------------------------------------------------------------------
# 抽样复核（F3）
# ---------------------------------------------------------------------------


@router.get("/audit")
def list_audit_runs(
    engagement_id: str,
    page: Page = Depends(pagination_params),
    db: sqlite3.Connection = Depends(get_db),
) -> PageResult:
    """覆盖抽样复核历史（audit_runs）。"""
    _require_engagement(db, engagement_id)
    total = db.execute(
        "SELECT COUNT(*) FROM audit_runs WHERE engagement_id=?", (engagement_id,)
    ).fetchone()[0]
    rows = db.execute(
        "SELECT * FROM audit_runs WHERE engagement_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (engagement_id, page.limit, page.offset),
    ).fetchall()
    return PageResult[dict](items=[dict(r) for r in rows], total=total,
                            offset=page.offset, limit=page.limit)


@router.post("/items/{cid}/audit")
def audit_coverage_item(
    engagement_id: str,
    cid: str,
    payload: AuditIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """手动触发/确认抽样复核（F3）。

    - 带 ``verdict``：落定审计（audit_runs 留痕；coverage_discrepancy → item 回退 untested）；
    - 不带 ``verdict``：仅建 pending audit_run（verdict NULL），供 auditor 稍后确认。
    """
    _require_engagement(db, engagement_id)
    _require_item(db, engagement_id, cid)
    reason = payload.reason if payload.reason in ("sampling", "discrepancy", "manual") else "manual"
    auditor = payload.auditor or "human"
    if payload.verdict is not None:
        ar = apply_audit_verdict(
            db, engagement_id, item_id=cid, verdict=payload.verdict.value,
            auditor=auditor, reason=reason,
            depth_reached=payload.depth_reached.value if payload.depth_reached is not None else None,
            note=payload.note,
        )
    else:
        audit_id = next_id(db, "audit_run", engagement_id=engagement_id)
        db.execute(
            "INSERT INTO audit_runs (id, engagement_id, coverage_item_id, reason, auditor, verdict, created_at) "
            "VALUES (?,?,?,?,?,NULL,?)",
            (audit_id, engagement_id, cid, reason, auditor, utcnow()),
        )
        ar = db.execute("SELECT * FROM audit_runs WHERE id=?", (audit_id,)).fetchone()
    db.commit()
    return dict(ar)


# ---------------------------------------------------------------------------
# 导出（含豁免理由/审计；供报告与交付）
# ---------------------------------------------------------------------------


@router.get("/export")
def export_coverage(
    engagement_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> dict:
    """覆盖矩阵导出（含豁免理由/审计）。"""
    _require_engagement(db, engagement_id)
    items = []
    for r in db.execute(
        """
        SELECT ci.*, t.value AS target_value, t.criticality AS target_criticality,
               tt.name AS test_type_name
        FROM coverage_items ci
        JOIN targets t    ON ci.target_id = t.id
        JOIN test_types tt ON ci.test_type_id = tt.id
        WHERE ci.engagement_id = ?
        """,
        (engagement_id,),
    ):
        d = dict(r)
        d["waivers"] = [
            dict(w) for w in db.execute("SELECT * FROM waivers WHERE item_id=?", (r["id"],))
        ]
        d["audits"] = [
            dict(a) for a in db.execute("SELECT * FROM audit_runs WHERE coverage_item_id=?", (r["id"],))
        ]
        items.append(d)
    return {"items": items, "count": len(items), "engagement_id": engagement_id}
