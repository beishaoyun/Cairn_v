"""Pydantic DTO 基础：分页 / 错误响应 / 通用枚举。

枚举值须与 ``docs/database-ddl-draft.md`` 各表 CHECK 约束**逐字符一致**（黄金不变量 7）。
下游 Agent（20-25 等）在各自 routers/services 里直接复用本模块枚举，避免重复定义漂移。
"""

from __future__ import annotations

import enum
from typing import Any, Generic, TypeVar

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from ..config import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 统一响应基础
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """统一错误响应体（v2 §7.2）：``{"error_code","message","detail"}``。"""

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: str
    detail: Any = None


class Page(BaseModel):
    """列表接口统一分页参数（v2 §7.2：offset/limit）。"""

    model_config = ConfigDict(extra="forbid")

    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


class PageResult(BaseModel, Generic[T]):
    """列表接口统一返回包装（items + 分页元信息）。"""

    items: list[T]
    total: int = 0
    offset: int = 0
    limit: int = DEFAULT_PAGE_SIZE


def pagination_params(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
) -> Page:
    """FastAPI 依赖：把 ``?offset=&limit=`` 解析成 :class:`Page`。

    用法：``def list_x(page: Page = Depends(pagination_params))``。
    """
    return Page(offset=offset, limit=limit)


# ---------------------------------------------------------------------------
# 通用枚举（值 = DDL CHECK 逐字符）
# ---------------------------------------------------------------------------


class EngagementStatus(enum.StrEnum):
    planning = "planning"
    active = "active"
    paused = "paused"
    completed = "completed"
    archived = "archived"


class ProjectStatus(enum.StrEnum):
    active = "active"
    stopped = "stopped"


class TargetKind(enum.StrEnum):
    domain = "domain"
    ip = "ip"
    cidr = "cidr"
    url = "url"
    hostname = "hostname"


class ScopeStatus(enum.StrEnum):
    authorized = "authorized"
    prohibited = "prohibited"


class TestDepth(enum.StrEnum):
    """test_types.default_depth / coverage_items.depth_required /
    coverage_records.depth_achieved / audit_runs.depth_reached 共用。"""

    baseline = "baseline"
    standard = "standard"
    deep = "deep"


class TestTypeCategory(enum.StrEnum):
    recon = "recon"
    scan = "scan"
    webapp = "webapp"
    network = "network"
    config = "config"
    osint = "osint"
    auth = "auth"
    other = "other"


class CoverageItemStatus(enum.StrEnum):
    untested = "untested"
    in_progress = "in_progress"
    tested_no_issue = "tested_no_issue"
    tested_with_finding = "tested_with_finding"
    not_applicable = "not_applicable"
    waived = "waived"


class SeedSource(enum.StrEnum):
    auto = "auto"
    human = "human"


class CoverageOutcome(enum.StrEnum):
    no_issue = "no_issue"
    finding_created = "finding_created"
    not_applicable = "not_applicable"


class WaiverKind(enum.StrEnum):
    not_applicable = "not_applicable"
    out_of_scope = "out_of_scope"
    risk_accepted = "risk_accepted"


class FindingSeverity(enum.StrEnum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class VerifyStatus(enum.StrEnum):
    none = "none"
    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"


class FindingStatus(enum.StrEnum):
    open = "open"
    pending_verify = "pending_verify"
    pending_false_positive = "pending_false_positive"
    verified = "verified"
    needs_review = "needs_review"
    fixed = "fixed"
    false_positive = "false_positive"
    accepted = "accepted"
    closed = "closed"


class EvidenceKind(enum.StrEnum):
    screenshot = "screenshot"
    file = "file"
    command_log = "command_log"
    raw = "raw"


class HttpEvidenceSource(enum.StrEnum):
    captured = "captured"
    agent_typed = "agent_typed"


class TrafficLinkRole(enum.StrEnum):
    trigger = "trigger"
    related = "related"
    verification = "verification"
    replay = "replay"


class VerifyStage(enum.StrEnum):
    blind = "blind"
    comparison = "comparison"
    escalated = "escalated"


class VerifyIndependence(enum.StrEnum):
    cross_worker = "cross_worker"
    cross_model = "cross_model"
    cross_run = "cross_run"
    human = "human"
    none = "none"


class VerifyVerdict(enum.StrEnum):
    confirmed = "confirmed"
    rejected = "rejected"
    needs_more_evidence = "needs_more_evidence"


class ReplayStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    blocked = "blocked"


class ReplayResult(enum.StrEnum):
    unchanged = "unchanged"
    remediated = "remediated"
    ambiguous = "ambiguous"
    error = "error"


class AuditReason(enum.StrEnum):
    sampling = "sampling"
    discrepancy = "discrepancy"
    manual = "manual"


class AuditVerdict(enum.StrEnum):
    covered_matches = "covered_matches"
    coverage_discrepancy = "coverage_discrepancy"


class TaskStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    success = "success"
    failed = "failed"
    cancelled = "cancelled"
    unhealthy = "unhealthy"
    rejected = "rejected"


class TaskEventKind(enum.StrEnum):
    step = "step"
    tool = "tool"
    command = "command"
    output = "output"
    status = "status"
    error = "error"


class ReportFormat(enum.StrEnum):
    markdown = "markdown"
    html = "html"
    pdf = "pdf"


class EngagementCounterKind(enum.StrEnum):
    """engagement_counters.kind（DDL §4.1；A4 统一 ID 前缀 ↔ kind 映射）。"""

    finding = "finding"
    evidence = "evidence"
    http_evidence = "http_evidence"
    command_evidence = "command_evidence"
    finding_traffic_link = "finding_traffic_link"
    coverage_item = "coverage_item"
    coverage_record = "coverage_record"
    waiver = "waiver"
    report = "report"
    traffic = "traffic"
    verify_run = "verify_run"
    replay_run = "replay_run"
    audit_run = "audit_run"
    retest_confirmation = "retest_confirmation"
    target = "target"
    finding_history = "finding_history"
