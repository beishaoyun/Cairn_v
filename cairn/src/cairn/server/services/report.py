"""报告聚合与渲染 + finalize 编排（Agent 41 · skeleton §2.6/§3 ``services/report.py``）。

职责：
- ``finalize``：人工收尾编排 —— 校验覆盖策略（21 ``report_ready``）→ Engagement 置
  completed（20 ``transition_status``）→ 自动生成报告（v2 §4.6/§12 规则 18；
  human-workflow §6）。
- ``aggregate``：从 Engagement + findings + 统一时间线（D3）聚合报告数据。
- ``render_markdown`` / ``render_html``：渲染可读交付物。
- ``stats`` / export：skeleton §2.5 指标统计。

**D4 证据附录策略（硬要求）**：报告内嵌触发请求/响应原文（``finding_http_evidence``，
captured 派生，body ≤64KB）+ 命令回显 + 复核记录（verify_runs independence）+
重放记录（replay_runs）；**大流量只给引用**（traffic_id + sha256 + digest），按需
还原（GET /traffic/{tid}），绝不内嵌 GB 级原始包。

鉴权：「仅人工」由 H 语义标注 + 业务 gate 双重落实（C5：Agent 容器不持 token；
Dispatcher 写回策略白名单不含 finalize/report）。服务层 ``actor`` 仅作审计记录。

黄金不变量：服务层无状态短事务；路由单 commit；枚举与 DDL CHECK 一致。
"""

from __future__ import annotations

import csv
import html
import io
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..db import next_id
from ..errors import CairnError, ErrorCode
from ..models import FindingSeverity, ReportFormat
from . import capture as capture_svc
from . import coverage as coverage_svc
from . import scope as scope_svc
from . import timeline as timeline_svc

logger = logging.getLogger("cairn.server.services.report")

#: 默认生成格式（PDF 可选扩展，DDL CHECK 已含 'pdf'，本包不实现）
DEFAULT_REPORT_FORMATS = ("markdown", "html")

#: 报告文件根默认值（路由从 ServerConfig.db_path 派生传入；无则回退相对路径）
DEFAULT_REPORTS_ROOT = os.path.join("data", "reports")

#: severity 排序权重（报告漏洞清单按严重性降序）
_SEVERITY_RANK = {s.value: i for i, s in enumerate(FindingSeverity)}  # critical 0 ... info 4

#: 时间线聚合上限（报告「方法流程」章节数据源，24 契约）
_TIMELINE_LIMIT = 500


def _now_utc() -> str:
    """ISO8601 UTC 字符串（黄金不变量 8）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_engagement(conn: sqlite3.Connection, eid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM engagements WHERE id=?", (eid,)).fetchone()
    if row is None:
        raise CairnError(
            ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"id": eid}
        )
    return row


def _coverage_policy(conn: sqlite3.Connection, eid: str) -> dict:
    """finalize 收敛策略：``scope_policy.coverage``，缺省 21 的 DEFAULT_COVERAGE_POLICY。"""
    row = _require_engagement(conn, eid)
    try:
        scope_policy = json.loads(row["scope_policy"] or "{}")
    except (json.JSONDecodeError, TypeError):
        scope_policy = {}
    return scope_policy.get("coverage") or dict(coverage_svc.DEFAULT_COVERAGE_POLICY)


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def _finding_target(conn: sqlite3.Connection, tid: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM targets WHERE id=?", (tid,)).fetchone()
    if row is None:
        return {"value": tid, "kind": None}
    return {"value": row["value"], "kind": row["kind"], "criticality": row["criticality"]}


def _traffic_digest(
    conn: sqlite3.Connection,
    eid: str,
    traffic_id: str,
    traffic_root: str | None,
) -> str | None:
    """D4：大流量引用附 F2 digest（best-effort；文件不可读/损坏 → None）。"""
    try:
        meta = capture_svc.resolve_traffic(
            conn, eid, traffic_id, for_model=True, traffic_root=traffic_root
        )
        if meta.get("corrupt"):
            return None
        return meta.get("digest")
    except Exception:  # noqa: BLE001 —— 引用不因文件缺失而失败
        logger.warning("report: traffic digest 计算失败 traffic_id=%s", traffic_id)
        return None


def _finding_detail(
    conn: sqlite3.Connection,
    eid: str,
    row: sqlite3.Row,
    traffic_root: str | None,
) -> dict[str, Any]:
    """单个 finding 的完整报告视图（漏洞 + 证据 + 复核 + 重放 + D4 引用）。"""
    fid = row["id"]
    target = _finding_target(conn, row["target_id"])
    d = {
        "id": fid,
        "title": row["title"],
        "status": row["status"],
        "severity": row["severity"],
        "agent_severity": row["agent_severity"],
        "verified_severity": row["verified_severity"],
        "verify_status": row["verify_status"],
        "category": row["category"],
        "cwe_id": row["cwe_id"],
        "cvss_score": row["cvss_score"],
        "cvss_vector": row["cvss_vector"],
        "description": row["description"],
        "remediation": row["remediation"],
        "references": row["references_"],
        "detected_by": row["detected_by"],
        "source_fact_id": row["source_fact_id"],
        "coverage_item_id": row["coverage_item_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "fixed_at": row["fixed_at"],
        "closed_at": row["closed_at"],
        "retest_round": row["retest_round"],
        "retest_pass": row["retest_pass"],
        "reverify_count": row["reverify_count"],
        "target_id": row["target_id"],
        "target_value": target["value"],
        "target_kind": target["kind"],
    }
    # references_ 可能为 JSON 数组字符串（22 的 _row_to_dict 解析风格）
    if d["references"]:
        try:
            d["references"] = json.loads(d["references"])
        except (json.JSONDecodeError, TypeError):
            pass
    if row["evidence_summary"]:
        try:
            d["evidence_summary"] = json.loads(row["evidence_summary"])
        except (json.JSONDecodeError, TypeError):
            d["evidence_summary"] = row["evidence_summary"]
    else:
        d["evidence_summary"] = None

    # 内嵌请求/响应原文（captured 派生，body ≤64KB；D4 内嵌层）
    d["http_evidence"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM finding_http_evidence WHERE finding_id=? ORDER BY seq", (fid,)
        )
    ]
    # 命令回显证据（非 HTTP 类）
    d["command_evidence"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM finding_command_evidence WHERE finding_id=? ORDER BY seq", (fid,)
        )
    ]
    # 文件证据
    d["file_evidence"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM finding_evidence WHERE finding_id=? ORDER BY created_at", (fid,)
        )
    ]
    # 独立复核记录（F1：independence / verified_severity 如实标注）
    d["verify_runs"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM verify_runs WHERE finding_id=? ORDER BY created_at", (fid,)
        )
    ]
    # 确定性重放记录（F4；replay_runs 无 created_at 列，DDL §9.3 只有 started_at/finished_at）
    d["replay_runs"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM replay_runs WHERE finding_id=? ORDER BY started_at", (fid,)
        )
    ]
    # 复测分类型确认账本（C10）
    d["retest_confirmations"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM finding_retest_confirmations WHERE finding_id=? ORDER BY created_at",
            (fid,),
        )
    ]
    # 关联流量 → D4 引用（traffic_id + sha256 + digest；大流量不内嵌）
    refs: list[dict[str, Any]] = []
    for lt in capture_svc.get_linked_traffic(conn, fid):
        refs.append(
            {
                "traffic_id": lt["id"],
                "role": lt["role"],
                "source": lt["source"],
                "link_created_at": lt["created_at"],
                "method": lt.get("method"),
                "url": lt.get("url"),
                "status": lt.get("status"),
                "captured_at": lt.get("captured_at"),
                "req_path": lt.get("req_path"),
                "resp_path": lt.get("resp_path"),
                "req_bytes": lt.get("req_bytes"),
                "resp_bytes": lt.get("resp_bytes"),
                "sha256": lt.get("sha256"),
                "digest": _traffic_digest(conn, eid, lt["id"], traffic_root),
            }
        )
    d["traffic_refs"] = refs
    return d


def _coverage_matrix_snapshot(conn: sqlite3.Connection, eid: str) -> list[dict[str, Any]]:
    """热力图快照（HTML 渲染用）：每格 target/test_type/status/priority/depth。"""
    rows = []
    for r in conn.execute(
        """
        SELECT ci.id, ci.status, ci.depth_required, ci.priority_score, ci.retest_round,
               t.value AS target_value, tt.name AS test_type_name
        FROM coverage_items ci
        JOIN targets t   ON ci.target_id = t.id
        JOIN test_types tt ON ci.test_type_id = tt.id
        WHERE ci.engagement_id = ?
        ORDER BY t.value, tt.name
        """,
        (eid,),
    ):
        rows.append(dict(r))
    return rows


def aggregate(
    conn: sqlite3.Connection,
    eid: str,
    *,
    traffic_root: str | None = None,
    generated_by: str = "human",
) -> dict[str, Any]:
    """聚合报告数据（ReportData）。

    章节数据源：
    - 执行摘要：engagement + 漏洞严重性分布 + 覆盖总结；
    - 范围：targets 列表；
    - 方法：24 统一时间线渲染为有序步骤（timeline.engagement_timeline）；
    - 漏洞清单：findings 全量明细（含证据/复核/重放/引用）；
    - 覆盖总结：coverage.coverage_summary + 热力图快照；
    - 证据附录：D4 策略。
    """
    eng = scope_svc.get_engagement(conn, eid)
    if eng is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"id": eid})

    findings_rows = conn.execute(
        "SELECT * FROM findings WHERE engagement_id=? ORDER BY created_at", (eid,)
    ).fetchall()
    findings = [
        _finding_detail(conn, eid, r, traffic_root) for r in findings_rows
    ]
    # 漏洞清单按生效 severity 降序（verified_severity 存在时生效值=severity）
    findings.sort(key=lambda f: _SEVERITY_RANK.get(f["severity"], 99))

    coverage = coverage_svc.coverage_summary(conn, eid)
    targets = scope_svc.list_targets(conn, eid)
    timeline = timeline_svc.engagement_timeline(conn, eid, limit=_TIMELINE_LIMIT)

    return {
        "engagement": eng,
        "generated_at": _now_utc(),
        "generated_by": generated_by,
        "targets": targets,
        "coverage": coverage,
        "coverage_matrix": _coverage_matrix_snapshot(conn, eid),
        "findings": findings,
        "timeline": timeline,
    }


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _severity_line(f: dict[str, Any]) -> str:
    """severity 双轨（规则 27 / 8.1→9.0 标注）：agent 初判 vs verified 终判差异。"""
    line = f["agent_severity"]
    if f["verified_severity"] and f["verified_severity"] != f["agent_severity"]:
        line += f" → {f['verified_severity']}（复核修正）"
    elif f["verified_severity"]:
        line += f"（复核确认 {f['verified_severity']}）"
    if f["cvss_score"] is not None:
        line += f"  ·  CVSS {f['cvss_score']}"
        if f["cvss_vector"]:
            line += f"  `{f['cvss_vector']}`"
    return line


def _finding_markdown(f: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(f"### {f['title']}（{f['id']}）")
    out.append(f"- 生效 severity：**{_severity_line(f)}**")
    out.append(f"- 状态：`{f['status']}`  ·  检测者：`{f['detected_by']}`")
    out.append(f"- 目标：`{f['target_value']}`（{f['target_kind']}，target_id={f['target_id']}）")
    if f["category"]:
        out.append(f"- 类别：`{f['category']}`" + (f"  ·  CWE {f['cwe_id']}" if f["cwe_id"] else ""))
    if f["description"]:
        out.append("\n**描述**\n\n" + f["description"])
    if f["remediation"]:
        out.append("\n**修复建议**\n\n" + f["remediation"])
    if f["evidence_summary"]:
        out.append("\n**证据摘要**\n\n" + str(f["evidence_summary"]))
    if f["retest_round"] or f["retest_pass"]:
        out.append(
            f"\n**复测**：round={f['retest_round']}  pass={f['retest_pass']}  "
            f"reverify={f['reverify_count']}"
        )
    # D4 内嵌层：请求/响应原文（captured 派生，≤64KB）
    if f["http_evidence"]:
        out.append("\n**请求/响应证据（内嵌原文）**")
        for h in f["http_evidence"]:
            out.append(f"\n**{h['method']} {h['url']}**"
                       + (f" → {h['response_status']}" if h["response_status"] is not None else "")
                       + f"（source={h['source']}"
                       + (f"，traffic_id={h['traffic_id']}" if h["traffic_id"] else "")
                       + f"）")
            if h["request_headers"]:
                out.append("请求头：\n```\n" + h["request_headers"] + "\n```")
            if h["request_body"]:
                out.append("请求体：\n```\n" + h["request_body"] + "\n```")
            if h["response_headers"]:
                out.append("响应头：\n```\n" + h["response_headers"] + "\n```")
            if h["response_body"]:
                out.append("响应体：\n```\n" + h["response_body"] + "\n```")
            if h["note"]:
                out.append(f"*注：{h['note']}*")
    # 命令回显证据
    if f["command_evidence"]:
        out.append("\n**命令回显证据**")
        for c in f["command_evidence"]:
            out.append(
                f"\n`{c['command']}`（exit={c['exit_code']}，cwd={c['cwd'] or '/'}）"
            )
            if c["stdout"]:
                out.append("stdout：\n```\n" + c["stdout"] + "\n```")
            if c["stderr"]:
                out.append("stderr：\n```\n" + c["stderr"] + "\n```")
    # 独立复核记录（F1）
    if f["verify_runs"]:
        out.append("\n**独立复核记录（verify_runs）**")
        for v in f["verify_runs"]:
            out.append(
                f"- {v['id']} stage=`{v['stage']}` independence=`{v['independence']}` "
                f"verdict=`{v['verdict']}`"
                + (f" verified_severity=`{v['verified_severity']}`" if v["verified_severity"] else "")
                + (f" reason={v['reason']}" if v["reason"] else "")
            )
    # 确定性重放记录（F4）
    if f["replay_runs"]:
        out.append("\n**确定性重放记录（replay_runs）**")
        for r in f["replay_runs"]:
            out.append(
                f"- {r['id']} status=`{r['status']}` result={r['result']} "
                f"matched_original={r['matched_original']} variants={r['payload_variants']}"
                + (f" trigger_traffic_id={r['trigger_traffic_id']}" if r["trigger_traffic_id"] else "")
            )
    # D4 引用层：大流量只给引用（traffic_id + sha256 + digest）
    if f["traffic_refs"]:
        out.append("\n**关联流量引用（D4：大流量不内嵌，按需还原）**")
        for ref in f["traffic_refs"]:
            out.append(
                f"- {ref['traffic_id']} `{ref['method']} {ref['url']}`"
                f" role={ref['role']} status={ref['status']}"
                f" sha256={ref['sha256'] or '-'}"
                f" req={ref['req_bytes']}B resp={ref['resp_bytes'] or 0}B"
            )
            if ref["digest"]:
                out.append("  digest：\n```\n" + ref["digest"] + "\n```")
            else:
                out.append(
                    "  digest：不可用（需还原时 `GET /engagements/{eid}/traffic/"
                    + ref["traffic_id"]
                    + "?for_model=true`）"
                )
    return "\n".join(out)


def render_markdown(data: dict[str, Any]) -> str:
    """Markdown 报告（可读交付物）。"""
    eng = data["engagement"]
    cov = data["coverage"]
    lines: list[str] = []
    lines.append(f"# 渗透测试报告 — {eng['title']}")
    lines.append("")
    lines.append(
        f"- Engagement：`{eng['id']}`  状态：`{eng['status']}`  生成："
        f"{data['generated_at']}（{data['generated_by']}）"
    )
    if eng.get("authorized_start_at") or eng.get("authorized_end_at"):
        lines.append(
            f"- 授权窗口：{eng.get('authorized_start_at') or '—'} ~ {eng.get('authorized_end_at') or '—'}"
        )
    lines.append("")

    # 1. 执行摘要
    lines.append("## 1. 执行摘要")
    lines.append("")
    sev = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in data["findings"]:
        sev[f["severity"]] = sev.get(f["severity"], 0) + 1
    lines.append(
        f"共发现 **{len(data['findings'])}** 项漏洞："
        + "，".join(f"{k}={v}" for k, v in sev.items() if v)
    )
    lines.append(
        f"覆盖：{cov['total']} 格 / 已覆盖 {cov['covered']}（覆盖率 {cov['coverage_ratio']:.1%}）"
        f"，未测 {cov['untested']}，进行中 {cov['in_progress']}，豁免 {cov['waived']}，"
        f"不适用 {cov['not_applicable']}，partial {cov['partial']}"
    )
    lines.append("")

    # 2. 授权范围
    lines.append("## 2. 授权范围")
    lines.append("")
    for t in data["targets"]:
        lines.append(
            f"- `{t['value']}`（{t['kind']}，{t['scope_status']}"
            + (f"，criticality={t['criticality']}" if t.get("criticality") is not None else "")
            + (f"，auto_created" if t.get("auto_created") else "")
            + "）"
        )
    lines.append("")

    # 3. 方法流程（= 24 timeline 渲染为有序步骤列表）
    lines.append("## 3. 方法流程")
    lines.append("")
    lines.append("> 数据源：统一时间线（D3，六源聚合）。按时间顺序列为执行步骤。")
    lines.append("")
    if not data["timeline"]:
        lines.append("_（无时间线事件）_")
    for i, ev in enumerate(data["timeline"], start=1):
        ts = ev.get("ts") or ""
        src = ev.get("source") or ""
        kind = ev.get("kind") or ""
        actor = ev.get("actor") or ""
        summary = (ev.get("summary") or "").replace("\n", " ")
        lines.append(
            f"{i}. `[{ts}]` **{src}/{kind}**"
            + (f" by `{actor}`" if actor else "")
            + f" — {summary}"
        )
    lines.append("")

    # 4. 漏洞清单
    lines.append("## 4. 漏洞清单")
    lines.append("")
    if not data["findings"]:
        lines.append("_（无已登记漏洞）_")
    for f in data["findings"]:
        lines.append(_finding_markdown(f))
        lines.append("")

    # 5. 修复建议
    lines.append("## 5. 修复建议")
    lines.append("")
    remediated = [f for f in data["findings"] if f.get("remediation")]
    if not remediated:
        lines.append("_（各漏洞修复建议见 §4 漏洞清单对应条目）_")
    for f in remediated:
        lines.append(f"- **{f['title']}**（{f['id']}）：{f['remediation']}")
    lines.append("")

    # 6. 覆盖总结
    lines.append("## 6. 覆盖总结")
    lines.append("")
    lines.append(
        f"矩阵规模：{cov['total']} 格；已覆盖 {cov['covered']}（含 with_finding "
        f"{cov['with_finding']}、not_applicable {cov['not_applicable']}、waived {cov['waived']}）"
    )
    lines.append(f"未测 {cov['untested']}，进行中 {cov['in_progress']}，partial {cov['partial']}")
    lines.append("")

    # 7. 证据附录
    lines.append("## 7. 证据附录")
    lines.append("")
    lines.append("> D4 策略：内嵌触发请求/响应原文 + 命令回显 + 复核/重放记录；大流量仅引用。")
    lines.append("")
    if not data["findings"]:
        lines.append("_（无漏洞证据）_")
    for f in data["findings"]:
        lines.append(f"### {f['title']}（{f['id']}）")
        lines.append(f"- 证据引用：`{f['id']}` 详情见 §4 对应条目")
        if f["traffic_refs"]:
            lines.append("- 关联流量（引用）：")
            for ref in f["traffic_refs"]:
                lines.append(
                    f"  - `{ref['traffic_id']}` `{ref['method']} {ref['url']}`"
                    f" role={ref['role']} sha256={ref['sha256'] or '-'}"
                    f" req={ref['req_bytes']}B resp={ref['resp_bytes'] or 0}B"
                )
        lines.append("")
    return "\n".join(lines)


def _esc(value: Any) -> str:
    """HTML 转义（None → 空串）。"""
    if value is None:
        return ""
    return html.escape(str(value))


def _cell_status(status: str) -> str:
    """热力图单元格样式 class。"""
    return {
        "tested_no_issue": "ok",
        "tested_with_finding": "finding",
        "waived": "waived",
        "not_applicable": "na",
        "in_progress": "progress",
        "untested": "untested",
    }.get(status, "")


def render_html(data: dict[str, Any]) -> str:
    """HTML 报告（含时间线 + 覆盖热力图快照）。"""
    eng = data["engagement"]
    cov = data["coverage"]
    finding_blocks = []
    for f in data["findings"]:
        parts = [f"<h3>{_esc(f['title'])} <small>{_esc(f['id'])}</small></h3>"]
        parts.append(
            "<ul><li>生效 severity：<strong>%s</strong></li>"
            "<li>状态：<code>%s</code> · 检测者：<code>%s</code> · 目标：<code>%s</code></li></ul>"
            % (
                _esc(_severity_line(f)),
                _esc(f["status"]),
                _esc(f["detected_by"]),
                _esc(f["target_value"]),
            )
        )
        if f["description"]:
            parts.append(f"<p><strong>描述</strong><br>{_esc(f['description'])}</p>")
        if f["remediation"]:
            parts.append(f"<p><strong>修复建议</strong><br>{_esc(f['remediation'])}</p>")
        for h in f["http_evidence"]:
            parts.append(
                "<h4>请求/响应证据（内嵌）</h4>"
                f"<p><code>{_esc(h['method'])} {_esc(h['url'])}</code>"
                + (f" → {_esc(h['response_status'])}" if h["response_status"] is not None else "")
                + f" <small>source={_esc(h['source'])}</small></p>"
            )
            if h["request_headers"]:
                parts.append(f"<pre>请求头\n{_esc(h['request_headers'])}</pre>")
            if h["request_body"]:
                parts.append(f"<pre>请求体\n{_esc(h['request_body'])}</pre>")
            if h["response_headers"]:
                parts.append(f"<pre>响应头\n{_esc(h['response_headers'])}</pre>")
            if h["response_body"]:
                parts.append(f"<pre>响应体\n{_esc(h['response_body'])}</pre>")
        for c in f["command_evidence"]:
            parts.append(
                "<h4>命令回显证据</h4>"
                f"<p><code>{_esc(c['command'])}</code> exit={_esc(c['exit_code'])}</p>"
            )
            if c["stdout"]:
                parts.append(f"<pre>stdout\n{_esc(c['stdout'])}</pre>")
            if c["stderr"]:
                parts.append(f"<pre>stderr\n{_esc(c['stderr'])}</pre>")
        for v in f["verify_runs"]:
            parts.append(
                f"<p>复核 <code>{_esc(v['id'])}</code> stage={_esc(v['stage'])} "
                f"independence=<code>{_esc(v['independence'])}</code> "
                f"verdict=<code>{_esc(v['verdict'])}</code>"
                + (f" verified_severity={_esc(v['verified_severity'])}" if v["verified_severity"] else "")
                + "</p>"
            )
        for r in f["replay_runs"]:
            parts.append(
                f"<p>重放 <code>{_esc(r['id'])}</code> status={_esc(r['status'])} "
                f"result={_esc(r['result'])} matched_original={_esc(r['matched_original'])}</p>"
            )
        if f["traffic_refs"]:
            parts.append("<h4>关联流量引用（D4）</h4><ul>")
            for ref in f["traffic_refs"]:
                parts.append(
                    f"<li><code>{_esc(ref['traffic_id'])}</code> {_esc(ref['method'])} "
                    f"{_esc(ref['url'])} role={_esc(ref['role'])} sha256={_esc(ref['sha256']) or '-'}"
                    f" req={_esc(ref['req_bytes'])}B</li>"
                )
            parts.append("</ul>")
        finding_blocks.append("\n".join(parts))

    heatmap_rows = []
    for cell in data["coverage_matrix"]:
        heatmap_rows.append(
            "<tr>"
            f"<td>{_esc(cell['target_value'])}</td>"
            f"<td>{_esc(cell['test_type_name'])}</td>"
            f"<td>{_esc(cell['depth_required'])}</td>"
            f"<td class='{_cell_status(cell['status'])}'>{_esc(cell['status'])}</td>"
            f"<td>{_esc(cell['priority_score'])}</td>"
            "</tr>"
        )

    timeline_ol = []
    for ev in data["timeline"]:
        timeline_ol.append(
            f"<li><code>{_esc(ev.get('ts'))}</code> <strong>{_esc(ev.get('source'))}/"
            f"{_esc(ev.get('kind'))}</strong>"
            + (f" by <code>{_esc(ev.get('actor'))}</code>" if ev.get("actor") else "")
            + f" — {_esc(ev.get('summary'))}</li>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>渗透测试报告 — {_esc(eng['title'])}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; max-width: 1000px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
h1,h2,h3 {{ border-bottom: 1px solid #eee; padding-bottom: .2rem; }}
pre {{ background: #f6f8fa; padding: .6rem; border-radius: 4px; overflow-x: auto; font-size: .85rem; }}
code {{ background: #f0f0f0; padding: .1em .3em; border-radius: 3px; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #ddd; padding: .3rem .5rem; font-size: .85rem; text-align: left; }}
.ok {{ background: #dff0d8; }} .finding {{ background: #f2dede; }} .waived {{ background: #fcf8e3; }}
.na {{ background: #f5f5f5; }} .progress {{ background: #d9edf7; }} .untested {{ background: #fff; }}
</style>
</head>
<body>
<h1>渗透测试报告 — {_esc(eng['title'])}</h1>
<p>Engagement：<code>{_esc(eng['id'])}</code> · 状态：<code>{_esc(eng['status'])}</code> ·
生成：{_esc(data['generated_at'])}（{_esc(data['generated_by'])}）</p>

<h2>1. 执行摘要</h2>
<p>共发现 <strong>{len(data['findings'])}</strong> 项漏洞。
覆盖：{cov['total']} 格 / 已覆盖 {cov['covered']}（{cov['coverage_ratio']:.1%}），
未测 {cov['untested']}，豁免 {cov['waived']}。</p>

<h2>2. 授权范围</h2>
<ul>
{''.join(f"<li><code>{_esc(t['value'])}</code>（{_esc(t['kind'])}，{_esc(t['scope_status'])}）</li>" for t in data['targets'])}
</ul>

<h2>3. 方法流程</h2>
<ol>
{''.join(timeline_ol) or "<li>（无时间线事件）</li>"}
</ol>

<h2>4. 漏洞清单</h2>
{''.join(finding_blocks) or "<p>（无已登记漏洞）</p>"}

<h2>5. 修复建议</h2>
<ul>
{''.join(f"<li><strong>{_esc(f['title'])}</strong>（{_esc(f['id'])}）：{_esc(f.get('remediation'))}</li>" for f in data['findings'] if f.get('remediation')) or "<li>（各漏洞修复建议见 §4 对应条目）</li>"}
</ul>

<h2>6. 覆盖总结</h2>
<p>矩阵规模 {cov['total']} 格；已覆盖 {cov['covered']}，未测 {cov['untested']}，
进行中 {cov['in_progress']}，partial {cov['partial']}。</p>
<h3>覆盖热力图快照</h3>
<table>
<tr><th>目标</th><th>测试项</th><th>深度</th><th>状态</th><th>priority</th></tr>
{''.join(heatmap_rows)}
</table>

<h2>7. 证据附录</h2>
<p>D4 策略：内嵌触发请求/响应原文 + 命令回显 + 复核/重放记录；大流量仅引用。</p>
{''.join(f"<h3>{_esc(f['title'])}（{_esc(f['id'])}）</h3>" for f in data['findings']) or "<p>（无漏洞证据）</p>"}
</body>
</html>"""


# ---------------------------------------------------------------------------
# 报告生成 / 列表 / 下载
# ---------------------------------------------------------------------------


def _write_report_file(
    reports_root: str,
    eid: str,
    rid: str,
    fmt: str,
    content: str,
) -> str:
    """报告内容落盘；返回相对路径（DB 存相对路径，下载时 join reports_root）。"""
    rel = os.path.join(eid, f"{rid}.{fmt}")
    base = os.path.join(reports_root, eid)
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{rid}.{fmt}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return rel


def generate(
    conn: sqlite3.Connection,
    eid: str,
    *,
    generated_by: str = "human",
    formats: tuple[str, ...] | list[str] | None = None,
    traffic_root: str | None = None,
    reports_root: str | None = None,
) -> list[dict[str, Any]]:
    """生成报告（不改变 engagement 状态）。每种格式写一条 reports 记录（rpt-###）。

    版本可追溯：连续调用产生 rpt-001 / rpt-002，均可分别下载。
    """
    _require_engagement(conn, eid)
    fmt_list = list(formats or DEFAULT_REPORT_FORMATS)
    for fmt in fmt_list:
        if fmt not in (ReportFormat.markdown.value, ReportFormat.html.value):
            raise CairnError(
                ErrorCode.VALIDATION,
                message="报告格式仅支持 markdown/html（pdf 可选扩展）",
                detail={"format": fmt},
            )
    root = reports_root or DEFAULT_REPORTS_ROOT

    data = aggregate(conn, eid, traffic_root=traffic_root, generated_by=generated_by)
    out: list[dict[str, Any]] = []
    for fmt in fmt_list:
        content = (
            render_markdown(data) if fmt == ReportFormat.markdown.value else render_html(data)
        )
        rid = next_id(conn, "report", engagement_id=eid)
        rel = _write_report_file(root, eid, rid, fmt, content)
        conn.execute(
            "INSERT INTO reports (id, engagement_id, format, path, generated_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rid, eid, fmt, rel, generated_by, data["generated_at"]),
        )
        out.append(
            {
                "id": rid,
                "engagement_id": eid,
                "format": fmt,
                "path": rel,
                "generated_by": generated_by,
                "created_at": data["generated_at"],
            }
        )
    conn.commit()
    return out


def finalize(
    conn: sqlite3.Connection,
    eid: str,
    *,
    generated_by: str = "human",
    traffic_root: str | None = None,
    reports_root: str | None = None,
) -> dict[str, Any]:
    """人工收尾（v2 §4.6 / human-workflow §6 / 规则 18）。

    - 业务 gate：覆盖策略达标（21 ``report_ready``）；不达标 → 409
      ``COVERAGE_POLICY_UNMET`` + 明细（豁免后可重试）。
    - 达标 → Engagement 置 completed（20 ``transition_status``）→ 自动生成报告。

    「仅人工」双重：H 语义标注（路由）+ 本业务 gate（Agent 不可能绕过覆盖收敛）；
    实际鉴权由 C5（Agent 不持 token）+ Dispatcher 写回白名单落实。
    """
    row = _require_engagement(conn, eid)
    # 熔断 gate（规则 2）
    scope_svc.check_kill_switch(conn, eid)
    # 状态 gate：仅 active/paused 可 finalize（planning 未激活不可收尾；
    # completed/archived 已终态，重复 finalize 无意义）
    if row["status"] not in ("active", "paused"):
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message=f"仅 active/paused 可 finalize（当前 {row['status']}）",
            detail={"id": eid, "status": row["status"]},
        )

    policy = _coverage_policy(conn, eid)
    ok, detail = coverage_svc.report_ready(conn, eid, policy)
    if not ok:
        raise CairnError(
            ErrorCode.COVERAGE_POLICY_UNMET,
            message="覆盖策略未达标，需先豁免剩余项或补齐覆盖",
            detail=detail,
        )

    # 达标 → 置 completed
    engagement = scope_svc.transition_status(conn, eid, "completed")
    # 自动生成报告
    reports = generate(
        conn,
        eid,
        generated_by=generated_by,
        traffic_root=traffic_root,
        reports_root=reports_root,
    )
    return {
        "ok": True,
        "engagement": engagement,
        "reports": reports,
        "detail": detail,
    }


def list_reports(conn: sqlite3.Connection, eid: str) -> list[dict[str, Any]]:
    """报告版本列表（按创建时间升序，供可追溯版本）。"""
    _require_engagement(conn, eid)
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM reports WHERE engagement_id=? ORDER BY created_at, id", (eid,)
        )
    ]


def latest_report(conn: sqlite3.Connection, eid: str) -> dict[str, Any] | None:
    """最新报告（12 客户端 ``GET /engagements/{eid}/report`` 取 latest）。"""
    row = conn.execute(
        "SELECT * FROM reports WHERE engagement_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
        (eid,),
    ).fetchone()
    return dict(row) if row else None


def get_report(conn: sqlite3.Connection, eid: str, rpt_id: str) -> dict[str, Any]:
    """按 rpt_id 取报告记录（404 若不存在或不属于该 engagement）。"""
    row = conn.execute(
        "SELECT * FROM reports WHERE id=? AND engagement_id=?", (rpt_id, eid)
    ).fetchone()
    if row is None:
        raise CairnError(
            ErrorCode.NOT_FOUND,
            message="报告不存在",
            detail={"report_id": rpt_id, "engagement_id": eid},
        )
    return dict(row)


# ---------------------------------------------------------------------------
# stats（skeleton §2.5：severity 分布 / 覆盖趋势 / 任务成功率）
# ---------------------------------------------------------------------------


def _count_by(conn: sqlite3.Connection, table: str, eid: str, col: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT {col} AS k, COUNT(*) AS n FROM {table} WHERE engagement_id=? "
        f"GROUP BY {col} ORDER BY n DESC",
        (eid,),
    ).fetchall()
    return {r["k"]: r["n"] for r in rows}


def stats(conn: sqlite3.Connection, eid: str) -> dict[str, Any]:
    """Engagement 指标统计（漏洞 severity 分布 / 覆盖趋势 / 任务成功率）。"""
    _require_engagement(conn, eid)

    severity_by_effective = {s.value: 0 for s in FindingSeverity}
    severity_by_effective.update(_count_by(conn, "findings", eid, "severity"))
    agent_severity = _count_by(conn, "findings", eid, "agent_severity")
    findings_by_status = _count_by(conn, "findings", eid, "status")

    # 覆盖趋势：coverage_records 按自然日计数 + 汇总
    cov_trend_rows = conn.execute(
        """
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n, outcome
        FROM coverage_records WHERE engagement_id=?
        GROUP BY day, outcome ORDER BY day
        """,
        (eid,),
    ).fetchall()
    coverage_trend: dict[str, dict[str, int]] = {}
    for r in cov_trend_rows:
        day = r["day"]
        bucket = coverage_trend.setdefault(day, {})
        bucket["count"] = bucket.get("count", 0) + r["n"]
        bucket[r["outcome"]] = bucket.get(r["outcome"], 0) + r["n"]

    # 任务成功率
    task_by_status = _count_by(conn, "task_runs", eid, "status")
    task_total = sum(task_by_status.values())
    task_success = task_by_status.get("success", 0)
    task_failed = task_by_status.get("failed", 0)

    # verify / replay 审计（verify_runs 无 engagement_id 列，经 findings 归属）
    verify_by_verdict: dict[str, int] = {}
    for r in conn.execute(
        """
        SELECT vr.verdict AS k, COUNT(*) AS n FROM verify_runs vr
        JOIN findings f ON f.id = vr.finding_id
        WHERE f.engagement_id = ?
        GROUP BY vr.verdict ORDER BY n DESC
        """,
        (eid,),
    ).fetchall():
        if r["k"]:
            verify_by_verdict[r["k"]] = r["n"]
    replay_by_result = _count_by(conn, "replay_runs", eid, "result")

    return {
        "engagement_id": eid,
        "findings": {
            "total": sum(severity_by_effective.values()),
            "by_severity": severity_by_effective,
            "by_agent_severity": agent_severity,
            "by_status": findings_by_status,
        },
        "coverage": coverage_svc.coverage_summary(conn, eid),
        "coverage_trend": coverage_trend,
        "tasks": {
            "total": task_total,
            "by_status": task_by_status,
            "success_rate": round(task_success / task_total, 4) if task_total else None,
            "failed": task_failed,
        },
        "verify": {"by_verdict": verify_by_verdict},
        "replay": {"by_result": replay_by_result},
    }


# ---------------------------------------------------------------------------
# 导出（findings / coverage）
# ---------------------------------------------------------------------------

# 归属决策（见 dev-agents/notes/41-report-finalize.md §stats/export 归属）：
# - findings/export 归 22（routers/findings.py 已实现 JSON/CSV）；
# - coverage/export 归 21（routers/coverage.py 已实现含豁免理由/审计）；
# - stats 归本包 41（上述实现）。
# 本包不重复注册上述 export 路由（避免遮蔽），只补 stats。
