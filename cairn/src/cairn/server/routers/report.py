"""报告路由（skeleton §2.6 + §2.5 stats；Agent 41 交付）。

端点：
- POST ``/engagements/{eid}/report``                 生成报告（markdown/html；**H** 仅人工）
- GET  ``/engagements/{eid}/report``                 最新报告（T；12 客户端路径假设，取 latest）
- GET  ``/engagements/{eid}/report/{rpt_id}``        下载指定版本（T；rpt-001/002 可分别下载）
- GET  ``/engagements/{eid}/stats``                 指标统计（skeleton §2.5；severity 分布/覆盖趋势/任务成功率）

finalize 端点在 ``routers/engagements.py``（Agent 20 留的 501 占位由本 Agent 替换，
服务编排走 ``services.report.finalize``）。

鉴权：全局 Bearer 中间件统一拦截（H/T 同一 token，D2）。「仅人工」由业务规则落实
（C5：Agent 容器不持 token；Dispatcher 写回白名单不含 report/finalize）。

归属（写交接物说明）：findings/export 归 22、coverage/export 归 21（均已实现），
本模块不重复注册；stats 归本包。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from ..db import get_db
from ..errors import CairnError, ErrorCode
from ..services import report as report_svc

router = APIRouter(tags=["report"])


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _reports_root(request: Request) -> str:
    """报告文件根：派生自 ServerConfig.db_path（配置无 reports_root 字段，不改 10 文件）。"""
    cfg = request.app.state.config
    return os.path.join(os.path.dirname(cfg.db_path) or ".", "reports")


def _read_report_content(path: str, reports_root: str) -> str:
    """按 DB 相对路径读报告内容；文件缺失 → 404。"""
    abs_path = os.path.join(reports_root, path)
    if not os.path.isfile(abs_path):
        raise CairnError(
            ErrorCode.NOT_FOUND,
            message="报告文件不存在",
            detail={"path": path},
        )
    with open(abs_path, "r", encoding="utf-8") as fh:
        return fh.read()


class ReportGenerateRequest(BaseModel):
    """生成报告请求体（可选；缺省同时生成 markdown + html）。"""

    model_config = ConfigDict(extra="forbid")

    formats: list[str] | None = None
    generated_by: str = "human"


class ReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    engagement_id: str
    format: str
    path: str
    generated_by: str
    created_at: str


# ---------------------------------------------------------------------------
# 报告生成 / 下载
# ---------------------------------------------------------------------------


@router.post("/engagements/{eid}/report", response_model=list[ReportOut])
def generate_report(
    eid: str,
    payload: ReportGenerateRequest | None = None,
    request: Request = None,  # type: ignore[assignment]
    db: sqlite3.Connection = Depends(get_db),
) -> list[dict[str, Any]]:
    """生成报告（H：仅人工）。版本连续 rpt-001/002，均可分别下载。

    不改变 engagement 状态；finalize 会自动生成报告，本端点用于人工重生成/补版本。
    """
    body = payload or ReportGenerateRequest()
    return report_svc.generate(
        db,
        eid,
        generated_by=body.generated_by or "human",
        formats=tuple(body.formats) if body.formats else None,
        traffic_root=request.app.state.config.traffic_root,
        reports_root=_reports_root(request),
    )


@router.get("/engagements/{eid}/report")
def latest_report(
    eid: str,
    request: Request = None,  # type: ignore[assignment]
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """最新报告（T）。12 客户端 ``get_report`` 路径假设。

    返回报告元数据 + 内容（markdown/html 原文），供 42 前端预览。
    """
    report = report_svc.latest_report(db, eid)
    if report is None:
        raise CairnError(
            ErrorCode.NOT_FOUND,
            message="暂无报告",
            detail={"engagement_id": eid},
        )
    report["content"] = _read_report_content(report["path"], _reports_root(request))
    return report


@router.get("/engagements/{eid}/report/{rpt_id}")
def download_report(
    eid: str,
    rpt_id: str,
    request: Request = None,  # type: ignore[assignment]
    db: sqlite3.Connection = Depends(get_db),
):
    """下载指定版本报告（T；rpt-001/002 可分别下载）。

    markdown → text/plain；html → text/html（浏览器直接预览）。
    """
    report = report_svc.get_report(db, eid, rpt_id)
    content = _read_report_content(report["path"], _reports_root(request))
    media_type = "text/html; charset=utf-8" if report["format"] == "html" else "text/plain; charset=utf-8"
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(content, media_type=media_type)


# ---------------------------------------------------------------------------
# stats（skeleton §2.5）
# ---------------------------------------------------------------------------


@router.get("/engagements/{eid}/stats")
def engagement_stats(
    eid: str,
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """指标统计（漏洞按 severity 分布 / 覆盖趋势 / 任务成功率）。"""
    return report_svc.stats(db, eid)
