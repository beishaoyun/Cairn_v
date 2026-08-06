"""漏洞闭环路由（skeleton §2.5；Agent 22 交付）。

覆盖：findings CRUD / 状态流转 / 证据（文件·请求响应包·命令） / 流量关联 / verify 落定 /
复测确认与 closed 门槛 / replay 触发 / history 审计 / 过滤 / export 占位（41/42 接管）。

鉴权：全局 Bearer 中间件统一拦截（H/T 同一 token，D2）。「仅人工」由业务规则落实
（服务层 actor 参数 + Dispatcher 写回策略白名单，规则 37）。
"""

from __future__ import annotations

import base64
import csv
import io
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ..db import get_db, next_id
from ..errors import CairnError, ErrorCode
from ..models import (
    EvidenceKind,
    FindingSeverity,
    FindingStatus,
    HttpEvidenceSource,
    TrafficLinkRole,
    VerifyIndependence,
    VerifyStage,
    VerifyVerdict,
    pagination_params,
    Page,
)
from ..services import findings as svc

router = APIRouter(prefix="/engagements/{engagement_id}/findings", tags=["findings"])

#: 证据文件类型白名单（规则 8：image/*, text/*, application/pdf）
_WHITELIST_PREFIXES = ("image/", "text/")
_WHITELIST_EXACT = ("application/pdf",)
_EXT_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp",
    "pdf": "application/pdf",
    "txt": "text/plain", "md": "text/markdown", "log": "text/plain",
    "csv": "text/csv", "json": "application/json", "xml": "text/xml",
}


def _mime_allowed(mime: str | None) -> bool:
    m = (mime or "").lower()
    if m.startswith(_WHITELIST_PREFIXES) or m in _WHITELIST_EXACT:
        return True
    return False


def _safe_rel_path(path: str) -> str:
    """路径防穿越（B7 / 规则 8）：仅允许相对路径，禁止 ``..`` 与绝对路径。"""
    p = (path or "").replace("\\", "/").strip().lstrip("/")
    if not p or p in (".", ".."):
        raise CairnError(ErrorCode.VALIDATION, message="非法证据路径", detail={"path": path})
    parts = [seg for seg in p.split("/") if seg not in ("", ".", "..")]
    if not parts:
        raise CairnError(ErrorCode.VALIDATION, message="非法证据路径", detail={"path": path})
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Request DTO
# ---------------------------------------------------------------------------


class EvidenceRefItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: EvidenceKind = EvidenceKind.file
    mime: str | None = None
    size: int | None = None


class HttpEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int | None = Field(default=None, ge=1)
    traffic_id: str | None = None
    source: HttpEvidenceSource = HttpEvidenceSource.agent_typed
    method: str
    url: str
    request_headers: str | None = None
    request_body: str | None = None
    response_status: int | None = Field(default=None, ge=100, le=599)
    response_headers: str | None = None
    response_body: str | None = None
    note: str | None = None
    captured_at: str | None = None


class CommandEvidenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    cwd: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    started_at: str | None = None
    duration_ms: int | None = None


class FindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    severity: FindingSeverity
    description: str = ""
    asset: str | None = None
    # 集成修复（40）：12 客户端 create_finding 请求体携带 detected_by（skeleton §3 签名），
    # 此前被 extra="forbid" 拒绝 → 422 VALIDATION；补入模型并透传给服务层。
    detected_by: str | None = Field(default=None, max_length=200)
    target_id: str | None = None
    remediation: str | None = None
    cvss_score: float | None = Field(default=None, ge=0, le=10)
    cvss_vector: str | None = None
    cwe_id: str | None = None
    category: str | None = None
    references: list[str] | None = None
    evidence_refs: list[str | EvidenceRefItem] | None = None
    traffic_ids: list[str] | None = None
    http: list[HttpEvidenceCreate] | None = None
    commands: list[CommandEvidenceCreate] | None = None
    source_fact_id: str | None = None
    coverage_item_id: str | None = None
    evidence_summary: str | None = None
    status: FindingStatus | None = None
    actor: str = "agent"  # agent=自动写回（只能 open）；human=人工可任意态


class FindingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: FindingStatus | None = None
    note: str | None = None
    actor: str = "human"  # 人工编辑路径默认 human；自动写回可显式传 worker 名
    severity: FindingSeverity | None = None
    verified_severity: FindingSeverity | None = None
    remediation: str | None = None
    category: str | None = None
    cvss_score: float | None = Field(default=None, ge=0, le=10)
    cvss_vector: str | None = None
    cwe_id: str | None = None
    references: list[str] | None = None
    evidence_summary: str | None = None


class EvidenceUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind = EvidenceKind.file
    path: str | None = None
    mime: str | None = None
    content_b64: str = ""  # 缺省为空：仅登记引用（文件已由 Dispatcher 上传到 evidence_root）


class VerifyRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_run_id: str | None = None
    stage: VerifyStage = VerifyStage.comparison
    independence: VerifyIndependence = VerifyIndependence.cross_worker
    input_traffic_digest: str | None = None
    observations: list[dict[str, Any]] | None = None
    verdict: VerifyVerdict
    verified_severity: FindingSeverity | None = None
    reason: str | None = None
    verified_traffic_ids: list[str] | None = None
    suggested_action: str | None = None
    actor: str = "verify"


class RetestConfirmationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str  # replay | verify | human
    note: str | None = None
    actor: str = "human"


class TrafficLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    traffic_ids: list[str]
    role: TrafficLinkRole = TrafficLinkRole.related
    source: HttpEvidenceSource = HttpEvidenceSource.captured
    actor: str = "human"


class ReplayCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger_traffic_id: str
    payload_variants: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------


def _finding_dict(row) -> dict:
    return svc._row_to_dict(row)


def _detail(conn: sqlite3.Connection, fid: str) -> dict:
    f = svc._get_finding(conn, fid)
    f["evidence"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, kind, path, mime, size, created_at FROM finding_evidence WHERE finding_id=? ORDER BY created_at",
            (fid,),
        )
    ]
    f["http_evidence"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM finding_http_evidence WHERE finding_id=? ORDER BY seq, captured_at", (fid,)
        )
    ]
    f["command_evidence"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM finding_command_evidence WHERE finding_id=? ORDER BY seq, started_at", (fid,)
        )
    ]
    f["traffic_links"] = [
        dict(r)
        for r in conn.execute(
            "SELECT id, traffic_id, role, source, created_at FROM finding_traffic_links WHERE finding_id=? ORDER BY created_at",
            (fid,),
        )
    ]
    f["retest"] = svc.retest_pass_count(conn, fid)
    return f


def _require_finding(conn: sqlite3.Connection, eid: str, fid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM findings WHERE id=? AND engagement_id=?", (fid, eid)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在或不属于本 engagement",
                         detail={"engagement_id": eid, "finding_id": fid})
    return row


# ---------------------------------------------------------------------------
# 列表 / 创建
# ---------------------------------------------------------------------------


@router.get("")
def list_findings(
    engagement_id: str,
    status: FindingStatus | None = None,
    severity: FindingSeverity | None = None,
    target_id: str | None = None,
    page: Page = Depends(pagination_params),
    db: sqlite3.Connection = Depends(get_db),
):
    where = ["engagement_id=?"]
    params: list = [engagement_id]
    if status is not None:
        where.append("status=?")
        params.append(status.value)
    if severity is not None:
        where.append("severity=?")
        params.append(severity.value)
    if target_id is not None:
        where.append("target_id=?")
        params.append(target_id)
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM findings WHERE {' AND '.join(where)}", params
    ).fetchone()["n"]
    rows = db.execute(
        f"SELECT * FROM findings WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (*params, page.limit, page.offset),
    ).fetchall()
    db.commit()
    return {
        "items": [_finding_dict(r) for r in rows],
        "total": total,
        "offset": page.offset,
        "limit": page.limit,
    }


@router.post("", status_code=201)
def create_finding(
    engagement_id: str,
    payload: FindingCreate,
    db: sqlite3.Connection = Depends(get_db),
):
    # 先确认 engagement 存在（服务层也查，此处提前返回 NOT_FOUND 更明确）
    if db.execute("SELECT id FROM engagements WHERE id=?", (engagement_id,)).fetchone() is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"engagement_id": engagement_id})
    # 集成修复（40）：请求体 detected_by 优先，缺省回退 actor 推导（兼容既有调用）。
    detected_by = payload.detected_by or (payload.actor if payload.actor != "human" else "human")
    f = svc.create_finding(
        db,
        engagement_id,
        payload=payload.model_dump(mode="json"),
        detected_by=detected_by,
        actor=payload.actor,
    )
    db.commit()
    return _detail(db, f["id"])


# ---------------------------------------------------------------------------
# export（占位：41/42 交付报告/导出时接管）
# ---------------------------------------------------------------------------


@router.get("/export")
def export_findings(
    engagement_id: str,
    format: str = "json",
    db: sqlite3.Connection = Depends(get_db),
):
    """漏洞清单导出（交付物）。41/42 接管前的最小实现：JSON 或 CSV。"""
    rows = db.execute(
        "SELECT * FROM findings WHERE engagement_id=? ORDER BY created_at", (engagement_id,)
    ).fetchall()
    db.commit()
    data = [_finding_dict(r) for r in rows]
    if format == "json":
        return {"engagement_id": engagement_id, "findings": data}
    if format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "status", "severity", "agent_severity", "verified_severity",
                         "title", "target_id", "detected_by", "created_at", "description"])
        for d in data:
            writer.writerow([d.get("id"), d.get("status"), d.get("severity"), d.get("agent_severity"),
                             d.get("verified_severity"), d.get("title"), d.get("target_id"),
                             d.get("detected_by"), d.get("created_at"), d.get("description")])
        return {"content": buf.getvalue()}
    raise CairnError(ErrorCode.VALIDATION, message="format 仅支持 json/csv", detail={"format": format})


# ---------------------------------------------------------------------------
# 详情 / 更新（状态升级仅人工，skeleton §2.5）
# ---------------------------------------------------------------------------


@router.get("/{fid}")
def get_finding(
    engagement_id: str, fid: str, db: sqlite3.Connection = Depends(get_db)
):
    _require_finding(db, engagement_id, fid)
    db.commit()
    return _detail(db, fid)


@router.put("/{fid}")
def update_finding(
    engagement_id: str,
    fid: str,
    payload: FindingUpdate,
    db: sqlite3.Connection = Depends(get_db),
):
    row = _require_finding(db, engagement_id, fid)
    sets: list[str] = []
    params: list = []
    d = payload.model_dump(mode="json")
    for col in ("remediation", "category", "cvss_vector", "cwe_id", "evidence_summary"):
        if d.get(col) is not None:
            sets.append(f"{col}=?")
            params.append(d[col])
    if d.get("cvss_score") is not None:
        sets.append("cvss_score=?")
        params.append(d["cvss_score"])
    if d.get("references") is not None:
        sets.append("references_=?")
        params.append(json.dumps(d["references"]))
    if d.get("verified_severity") is not None:
        sets.append("verified_severity=?")
        params.append(d["verified_severity"])
        sets.append("severity=?")  # 生效 severity 取 verified_severity（规则 27 双轨）
        params.append(d["verified_severity"])
    if d.get("severity") is not None:
        sets.append("severity=?")
        params.append(d["severity"])
    if sets:
        sets.append("updated_at=?")
        params.append(svc._now())
        db.execute(f"UPDATE findings SET {', '.join(sets)} WHERE id=?", (*params, fid))
    if d.get("status") is not None:
        svc.transition_finding(
            db, fid, to_status=d["status"], note=d.get("note"), actor=d.get("actor") or "human"
        )
    else:
        _ = row  # 无状态变更时无需 history
    db.commit()
    return _detail(db, fid)


# ---------------------------------------------------------------------------
# 证据：文件（H / 白名单）· http（T）· commands（T）
# ---------------------------------------------------------------------------


@router.post("/{fid}/evidence", status_code=201)
def upload_evidence(
    engagement_id: str,
    fid: str,
    payload: EvidenceUpload,
    request: Request,
    db: sqlite3.Connection = Depends(get_db),
):
    _require_finding(db, engagement_id, fid)
    rel = _safe_rel_path(payload.path or f"{fid}/evidence")
    mime = payload.mime or _EXT_MIME.get(rel.rsplit(".", 1)[-1].lower()) if "." in rel else payload.mime
    if not _mime_allowed(mime):
        raise CairnError(
            ErrorCode.VALIDATION,
            message="证据文件类型不在白名单（image/*, text/*, application/pdf）",
            detail={"mime": mime, "path": rel},
        )
    size = None
    if payload.content_b64:
        try:
            content = base64.b64decode(payload.content_b64, validate=False)
        except Exception:  # noqa: BLE001
            raise CairnError(ErrorCode.VALIDATION, message="content_b64 解码失败")
        size = len(content)
        root = request.app.state.config.evidence_root
        abs_path = f"{root.rstrip('/')}/{engagement_id}/{rel}"
        import os
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "wb") as fh:
            fh.write(content)
    ev = svc.attach_evidence(db, fid, kind=payload.kind.value, path=rel, mime=mime, size=size)
    db.commit()
    return ev


@router.get("/{fid}/evidence")
def list_evidence(engagement_id: str, fid: str, db: sqlite3.Connection = Depends(get_db)):
    _require_finding(db, engagement_id, fid)
    rows = db.execute(
        "SELECT id, kind, path, mime, size, created_at FROM finding_evidence WHERE finding_id=? ORDER BY created_at",
        (fid,),
    ).fetchall()
    db.commit()
    return {"items": [dict(r) for r in rows]}


@router.post("/{fid}/http", status_code=201)
def add_http_evidence(
    engagement_id: str, fid: str, payload: HttpEvidenceCreate,
    db: sqlite3.Connection = Depends(get_db),
):
    _require_finding(db, engagement_id, fid)
    he = svc.add_http_evidence(db, fid, http_obj=payload.model_dump(mode="json"))
    db.commit()
    return he


@router.get("/{fid}/http")
def list_http_evidence(engagement_id: str, fid: str, db: sqlite3.Connection = Depends(get_db)):
    _require_finding(db, engagement_id, fid)
    rows = db.execute(
        "SELECT * FROM finding_http_evidence WHERE finding_id=? ORDER BY seq, captured_at", (fid,)
    ).fetchall()
    db.commit()
    return {"items": [dict(r) for r in rows]}


@router.post("/{fid}/commands", status_code=201)
def add_command_evidence(
    engagement_id: str, fid: str, payload: CommandEvidenceCreate,
    db: sqlite3.Connection = Depends(get_db),
):
    _require_finding(db, engagement_id, fid)
    ce = svc.add_command_evidence(db, fid, command_obj=payload.model_dump(mode="json"))
    db.commit()
    return ce


@router.get("/{fid}/commands")
def list_command_evidence(engagement_id: str, fid: str, db: sqlite3.Connection = Depends(get_db)):
    _require_finding(db, engagement_id, fid)
    rows = db.execute(
        "SELECT * FROM finding_command_evidence WHERE finding_id=? ORDER BY seq, started_at", (fid,)
    ).fetchall()
    db.commit()
    return {"items": [dict(r) for r in rows]}


@router.post("/{fid}/traffic", status_code=201)
def link_traffic(
    engagement_id: str, fid: str, payload: TrafficLinkCreate,
    db: sqlite3.Connection = Depends(get_db),
):
    """关联捕获流量（role=trigger/related/verification/replay）。"""
    _require_finding(db, engagement_id, fid)
    links = svc.link_finding_traffic(
        db, fid, payload.traffic_ids, role=payload.role.value, source=payload.source.value,
        actor=payload.actor,
    )
    db.commit()
    return {"items": links}


# ---------------------------------------------------------------------------
# verify 落定（30 经 12 客户端调用；人工可强制，skeleton §2.5 H）
# ---------------------------------------------------------------------------


@router.post("/{fid}/verify", status_code=201)
def apply_verify(
    engagement_id: str, fid: str, payload: VerifyRunCreate,
    db: sqlite3.Connection = Depends(get_db),
):
    _require_finding(db, engagement_id, fid)
    f = svc.apply_verify_runs(db, fid, vr=payload.model_dump(mode="json"))
    db.commit()
    return f


# ---------------------------------------------------------------------------
# 复测确认 / 确定性重放
# ---------------------------------------------------------------------------


@router.post("/{fid}/retest", status_code=201)
def record_retest(
    engagement_id: str, fid: str, payload: RetestConfirmationCreate,
    db: sqlite3.Connection = Depends(get_db),
):
    """记录一条复测确认（replay/verify/human；同轮同类型幂等）。"""
    _require_finding(db, engagement_id, fid)
    f = svc.record_retest_confirmation(
        db, fid, kind=payload.kind, note=payload.note, actor=payload.actor
    )
    db.commit()
    return svc.retest_pass_count(db, fid)


@router.post("/{fid}/replay", status_code=201)
def trigger_replay(
    engagement_id: str, fid: str, payload: ReplayCreate,
    db: sqlite3.Connection = Depends(get_db),
):
    """登记一次确定性重放（queued）。重放执行引擎归 30（replay/engine.py），本端只落账。

    重放完成（remediated / unchanged）后由 30 写 ``replay_runs`` 结果并调
    ``record_retest_confirmation(kind='replay')``。
    """
    row = _require_finding(db, engagement_id, fid)
    t = db.execute(
        "SELECT id FROM traffic_entries WHERE id=? AND engagement_id=?", (payload.trigger_traffic_id, engagement_id)
    ).fetchone()
    if t is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="trigger traffic 不存在或不属于本 engagement",
                         detail={"traffic_id": payload.trigger_traffic_id})
    rpid = next_id(db, "replay_run", engagement_id=engagement_id)
    db.execute(
        "INSERT INTO replay_runs (id, engagement_id, finding_id, trigger_traffic_id, status, "
        "payload_variants, started_at) VALUES (?, ?, ?, ?, 'queued', ?, ?)",
        (rpid, engagement_id, fid, payload.trigger_traffic_id, payload.payload_variants, svc._now()),
    )
    db.commit()
    return dict(db.execute("SELECT * FROM replay_runs WHERE id=?", (rpid,)).fetchone())


@router.get("/{fid}/replay")
def list_replay(engagement_id: str, fid: str, db: sqlite3.Connection = Depends(get_db)):
    _require_finding(db, engagement_id, fid)
    rows = db.execute(
        "SELECT * FROM replay_runs WHERE finding_id=? ORDER BY created_at DESC", (fid,)
    ).fetchall()
    db.commit()
    return {"items": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# history 审计
# ---------------------------------------------------------------------------


@router.get("/{fid}/history")
def get_history(engagement_id: str, fid: str, db: sqlite3.Connection = Depends(get_db)):
    _require_finding(db, engagement_id, fid)
    rows = db.execute(
        "SELECT id, from_status, to_status, note, actor, created_at FROM finding_history "
        "WHERE finding_id=? ORDER BY created_at",
        (fid,),
    ).fetchall()
    db.commit()
    return {"items": [dict(r) for r in rows]}
