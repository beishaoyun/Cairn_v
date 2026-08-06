"""explore 任务（Agent 30）—— 覆盖项驱动 + findings + coverage_result 写回。

契约（C2/B1/B4/C9，coverage spec §3.2）：
- 派发前注入 ``{coverage_context}``（认领格子）+ **traffic_ids 候选**（``list_traffic(
  eid, client=<worker>, since=intent_start)``，C5：Agent 只从候选引用，不能自查捕获索引）。
- 写回顺序（coverage spec §3.2 规则 5 / dev-agents/30 §2-C）：
  claim 互斥（B1）→ ``write_coverage_result``（幂等，C9）→ findings 落库（writer）+
  证据（http/commands）+ ``link_traffic(role='trigger')``。
- ``outcome=not_applicable`` **只建议不置状态**（B4：item 保持 untested，reason 仍可见）。
- ``COVERAGE_ALREADY_COVERED``（他人认领/已测）→ 写回作废 + release（预期分支，下轮重排）。
- ``complete`` 字段被拒。

返回 ``TaskResult``：
- ``claimed=false``：格子忙（B1），不派发，40 换格；
- ``success``：data = ``{"description", "findings", "coverage", "fact_id"}``；
- ``retryable``：覆盖写回 COVERAGE_ALREADY_COVERED（作废+release）。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..coverage.writer import CoverageWriter
from ..errors import COVERAGE_ALREADY_COVERED, CairnClientError
from ..findings.writer import FindingsWriter
from .common import (
    CancelledError,
    PayloadError,
    TaskContext,
    TaskResult,
    emit_event,
    extract_json,
    run_conclude_phase,
    run_worker_phase,
    validate_explore_payload,
    writeback_retries,
)


def build_explore_prompt(
    *,
    graph_yaml: str,
    intent_id: str,
    intent_description: str,
    coverage_context: Any,
    traffic_candidates: list[dict],
    scope: str = "",
) -> str:
    """按 prompts §2 模板渲染 explore 提示（注入 coverage_context + traffic_ids 候选）。"""
    coverage_text = json.dumps(coverage_context, ensure_ascii=False)
    traffic_text = json.dumps(traffic_candidates, ensure_ascii=False)
    return f"""# Task
You are an authorized penetration tester working on ONE intent inside an engagement.
You explore only in the direction of this intent, which maps to one or more coverage
cells (asset × test type). The target is within the authorized scope.

# Output Requirements
Return only one raw JSON object:
{{"accepted": true, "data": {{
  "description": "<objective factual finding, concise>",
  "findings": [{{"title": "...", "severity": "high", "cvss_score": 8.1,
     "cwe_id": "CWE-521", "asset": "http://10.0.0.5:8080/admin", "description": "...",
     "remediation": "...",
     "evidence_refs": ["e-001/screenshot.png"],
     "traffic_ids": ["tr-001"],
     "http": [{{"method": "POST", "url": "http://10.0.0.5:8080/admin/login",
       "request_body": "...", "response_status": 302, "response_body": "..."}}],
     "commands": [{{"command": "...", "cwd": "/home/worker/workspace", "exit_code": 0,
       "stdout": "...", "stderr": ""}}]}}],
  "coverage": {{
    "covered_items": ["c-013"],
    "depth_achieved": "standard",
    "outcome": "finding_created",
    "tested_scope": {{"endpoints": ["/admin/login"], "params": ["user"], "partial": false}}
  }}
}}}}

# Rules
- Explore the intent thoroughly; you may fail, but report an honest objective conclusion.
- `covered_items` must be from the coverage context below and must be untested cells.
- Only report CONFIRMED vulnerabilities; each needs severity, CVSS, evidence.
- **Web vulnerabilities MUST include the exact triggering request + response** in `http[]`
  and reference your captured exchange via `traffic_ids` **from the candidate list only**
  (you CANNOT query the capture index yourself — C5).
- `evidence_refs` paths are RELATIVE (e.g. `e-001/screenshot.png`).
- If the cell is not applicable, return `outcome: "not_applicable"` and explain.
- NEVER output `complete`.

# Context
## Graph snapshot
{graph_yaml}
## Current intent
{intent_id} — {intent_description}
## Coverage context
{coverage_text}
## Traffic candidates (your captured exchanges, reference by id)
{traffic_text}
## Authorized scope
{scope}
"""


def build_explore_conclude_prompt(
    *,
    intent_id: str,
    intent_description: str,
    coverage_context: Any,
) -> str:
    """explore_conclude 提示（prompts §3）：收尾只总结已确认成果。"""
    coverage_text = json.dumps(coverage_context, ensure_ascii=False)
    return f"""# Task
This is the conclude phase for intent {intent_id}. It overrides any earlier instruction:
STOP exploring, STOP waiting, STOP running commands. Summarize only what was already
confirmed before this prompt.

# Output Requirements
Return only one raw JSON object, same shape as explore:
{{"accepted": true, "data": {{"description": "<confirmed key findings>",
  "findings": [...], "coverage": {{"covered_items": [...], "depth_achieved": "...",
  "outcome": "...", "tested_scope": {{...}}}}}}}}

# Context
## Current intent
{intent_id} — {intent_description}
## Coverage context
{coverage_text}

# Rules
- Base output ONLY on already-confirmed information. No `complete`.
"""


def collect_traffic_candidates(
    ctx: TaskContext,
    *,
    worker: str,
    since: Optional[str] = None,
) -> list[dict]:
    """检索本 worker 时间窗内捕获（C5：Agent 只从候选引用；不能自查捕获索引）。"""
    if not ctx.eid:
        return []
    try:
        return ctx.client.list_traffic(ctx.eid, client=worker, since=since) or []
    except CairnClientError as exc:
        ctx.log(f"list_traffic 失败: {exc}")
        return []


def _coverage_context_for(
    item_ids: list[str],
    coverage: dict,
) -> list[dict]:
    """从 ``get_coverage`` 矩阵构建 {coverage_context}（认领格子的 target/test_type/depth）。"""
    cells = coverage.get("cells", [])
    test_types = {t["id"]: t for t in coverage.get("test_types", [])}
    targets = {t["id"]: t for t in coverage.get("targets", [])}
    out: list[dict] = []
    for c in cells:
        if c.get("item_id") in item_ids:
            tt = test_types.get(c.get("test_type_id"), {})
            tg = targets.get(c.get("target_id"), {})
            out.append({
                "item_id": c.get("item_id"),
                "target_id": c.get("target_id"),
                "target_value": tg.get("value"),
                "test_type_id": c.get("test_type_id"),
                "test_type_name": tt.get("name"),
                "depth_required": c.get("depth_required"),
                "status": c.get("status"),
            })
    return out


def run_explore(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    intent: dict,
    graph_yaml: str,
    scope: str = "",
    task_cfg: Optional[Any] = None,
    traffic_since: Optional[str] = None,
    claimed_item_ids: Optional[list[str]] = None,
) -> TaskResult:
    """explore execute 阶段（含 B1 认领 + 覆盖/findings 写回）。"""
    timeout = getattr(task_cfg or ctx.config.tasks.explore, "timeout", 300)
    intent_id = intent.get("id", "")
    intent_desc = intent.get("description", "")
    item_ids = list(intent.get("coverage_item_ids") or [])

    # 覆盖项认领（B1）：claimed_item_ids 由 40 派发前认领传入；未传则此处认领
    writer = CoverageWriter(ctx.client, log=ctx.log, retries=writeback_retries(ctx))
    if claimed_item_ids is None:
        claimed, busy = writer.claim_all(ctx.eid, item_ids, intent_id)
        if busy:
            for c in claimed:
                writer.release_item(ctx.eid, c, intent_id)
            emit_event(ctx, "status", "warn", f"explore 格子忙，不派发: busy={busy}")
            return TaskResult(
                status="failed",
                error=f"覆盖项已被他人认领/已测: {busy}",
                error_code=COVERAGE_ALREADY_COVERED,
                extra={"claimed": False, "busy": busy},
            )
        claimed_item_ids = claimed
    elif not item_ids:
        claimed_item_ids = []

    # coverage_context + traffic 候选（C5）
    coverage = {}
    try:
        coverage = ctx.client.get_coverage(ctx.eid) or {}
    except CairnClientError as exc:
        ctx.log(f"get_coverage 失败: {exc}")
    cov_ctx = _coverage_context_for(item_ids, coverage)
    traffic_candidates = collect_traffic_candidates(ctx, worker=ctx.worker, since=traffic_since)
    prompt = build_explore_prompt(
        graph_yaml=graph_yaml,
        intent_id=intent_id,
        intent_description=intent_desc,
        coverage_context=cov_ctx,
        traffic_candidates=traffic_candidates,
        scope=scope,
    )
    emit_event(ctx, "step", "info", f"explore: intent={intent_id} items={item_ids} traffic_candidates={len(traffic_candidates)}")
    try:
        text, _sid = run_worker_phase(ctx, driver=driver, backend=backend, prompt=prompt, timeout=timeout, phase="explore_execute")
    except CancelledError as exc:
        for c in claimed_item_ids:
            writer.release_item(ctx.eid, c, intent_id)
        emit_event(ctx, "status", "warn", f"explore 被取消: {exc}")
        return TaskResult(status="cancelled", error=str(exc), error_code="CANCELLED")
    except PayloadError as exc:
        for c in claimed_item_ids:
            writer.release_item(ctx.eid, c, intent_id)
        return TaskResult(status="failed", error=str(exc), error_code="VALIDATION")

    try:
        data = validate_explore_payload(
            extract_json(text),
            known_item_ids=item_ids,
            claimed_item_ids=claimed_item_ids,
        )
    except Exception as exc:  # noqa: BLE001
        for c in claimed_item_ids:
            writer.release_item(ctx.eid, c, intent_id)
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))

    # 写回：conclude intent（写 fact，取 fact_id）→ coverage → findings
    fact_id = _conclude_intent_best_effort(ctx, intent_id, data.get("description", ""))
    coverage_result = data["coverage"]
    try:
        writer.write_result(
            ctx.eid,
            item_ids=coverage_result["covered_items"],
            depth_achieved=coverage_result["depth_achieved"],
            outcome=coverage_result["outcome"],
            intent_id=intent_id,
            fact_id=fact_id,
            evidence_refs=[r for f in data.get("findings", []) for r in (f.get("evidence_refs") or [])] or None,
            tested_scope=coverage_result.get("tested_scope"),
            partial=coverage_result.get("partial", False),
        )
    except CairnClientError as exc:
        # COVERAGE_ALREADY_COVERED → 写回作废 + release（预期分支，下轮 reason 重排）
        if exc.error_code == COVERAGE_ALREADY_COVERED:
            for c in claimed_item_ids:
                writer.release_item(ctx.eid, c, intent_id)
            emit_event(ctx, "status", "warn", f"explore 覆盖写回作废（他人认领/已测）: {exc}")
            return TaskResult(
                status="retryable",
                error=str(exc),
                error_code=COVERAGE_ALREADY_COVERED,
                extra={"released": claimed_item_ids},
            )
        raise

    # findings 落库 + 证据 + link_traffic(role='trigger')
    created_findings: list[dict] = []
    if data.get("findings"):
        fw = FindingsWriter(ctx.client, log=ctx.log, retries=writeback_retries(ctx))
        created_findings = fw.write(
            ctx.eid,
            findings=data["findings"],
            detected_by=ctx.worker,
            actor="agent",
            source_fact_id=fact_id,
            coverage_item_id=coverage_result["covered_items"][0] if coverage_result["covered_items"] else None,
        )
        emit_event(ctx, "status", "info", f"explore: findings 落库 {len(created_findings)} 条")

    emit_event(ctx, "status", "info", f"explore: coverage outcome={coverage_result['outcome']}")
    return TaskResult(
        status="success",
        data={**data, "fact_id": fact_id, "created_findings": created_findings},
        outcome_note=f"outcome={coverage_result['outcome']}, findings={len(created_findings)}",
    )


def run_explore_conclude(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    intent: dict,
    session_id: Optional[str] = None,
    task_cfg: Optional[Any] = None,
    claimed_item_ids: Optional[list[str]] = None,
) -> TaskResult:
    """explore conclude 阶段（收尾；写回覆盖/findings 与 execute 相同契约）。"""
    timeout = getattr(task_cfg or ctx.config.tasks.explore, "conclude_timeout", 90)
    intent_id = intent.get("id", "")
    intent_desc = intent.get("description", "")
    item_ids = list(intent.get("coverage_item_ids") or [])
    coverage = {}
    try:
        coverage = ctx.client.get_coverage(ctx.eid) or {}
    except CairnClientError as exc:
        ctx.log(f"get_coverage 失败: {exc}")
    cov_ctx = _coverage_context_for(item_ids, coverage)
    prompt = build_explore_conclude_prompt(
        intent_id=intent_id, intent_description=intent_desc, coverage_context=cov_ctx
    )
    emit_event(ctx, "step", "info", "explore conclude: 收尾总结")
    try:
        text, _sid = run_conclude_phase(ctx, driver=driver, backend=backend, prompt=prompt, timeout=timeout, session_id=session_id, phase="explore_conclude")
    except CancelledError as exc:
        return TaskResult(status="cancelled", error=str(exc), error_code="CANCELLED")
    try:
        data = validate_explore_payload(
            extract_json(text),
            known_item_ids=item_ids,
            claimed_item_ids=claimed_item_ids,
        )
    except Exception as exc:  # noqa: BLE001
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))

    writer = CoverageWriter(ctx.client, log=ctx.log, retries=writeback_retries(ctx))
    fact_id = _conclude_intent_best_effort(ctx, intent_id, data.get("description", ""))
    cr = data["coverage"]
    try:
        writer.write_result(
            ctx.eid,
            item_ids=cr["covered_items"],
            depth_achieved=cr["depth_achieved"],
            outcome=cr["outcome"],
            intent_id=intent_id,
            fact_id=fact_id,
            tested_scope=cr.get("tested_scope"),
            partial=cr.get("partial", False),
        )
    except CairnClientError as exc:
        if exc.error_code == COVERAGE_ALREADY_COVERED:
            for c in claimed_item_ids or []:
                writer.release_item(ctx.eid, c, intent_id)
            return TaskResult(status="retryable", error=str(exc), error_code=COVERAGE_ALREADY_COVERED)
        raise
    created_findings = []
    if data.get("findings"):
        created_findings = FindingsWriter(ctx.client, log=ctx.log, retries=writeback_retries(ctx)).write(
            ctx.eid,
            findings=data["findings"],
            detected_by=ctx.worker,
            actor="agent",
            source_fact_id=fact_id,
            coverage_item_id=cr["covered_items"][0] if cr["covered_items"] else None,
        )
    return TaskResult(status="success", data={**data, "fact_id": fact_id, "created_findings": created_findings})


def _conclude_intent_best_effort(
    ctx: TaskContext,
    intent_id: str,
    description: str,
) -> Optional[str]:
    """conclude intent（25 图子域）：写 fact + 释放 intent，返回 fact_id（best-effort）。

    ``conclude_intent`` 客户端方法携带 ``facts``（描述列表）；返回 facts 含 id。
    graph 子域（25）未就绪/失败时返回 None（覆盖写回 fact_id 可空），不阻断。
    """
    if not ctx.project_id or not intent_id:
        return None
    try:
        resp = ctx.client.conclude_intent(
            ctx.project_id, intent_id, worker=ctx.worker, facts=[description]
        )
    except CairnClientError as exc:
        ctx.log(f"conclude_intent 失败（忽略）: {exc}")
        return None
    facts = resp.get("facts") if isinstance(resp, dict) else None
    if facts and isinstance(facts, list) and facts:
        return facts[-1].get("id") if isinstance(facts[-1], dict) else None
    return None
