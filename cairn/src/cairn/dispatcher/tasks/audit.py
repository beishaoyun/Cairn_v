"""audit 任务（Agent 30）—— 覆盖抽样复核（独立重测高优先格子，F3）。

契约（coverage spec §2 sample_audit / apply_audit_verdict；skeleton §3 TaskType 扩展）：
- 触发：21 的 ``sample_audit`` 选样（sampling 高优先抽样 / discrepancy 声称有 finding 却无
  finding）→ Dispatcher 派发 audit 任务由**另一 worker** 独立重测该覆盖项；
- 输出契约与 explore 同（description + findings + coverage），独立重测后给出
  ``verdict`` ∈ {match, coverage_discrepancy}；
- 落定：``POST /coverage/items/{cid}/audit``（verdict=coverage_discrepancy → 覆盖项回退
  untested + 缺口重排，F3/A3）。
- 本任务不消耗覆盖项认领（独立复核不 claim，不写 coverage_records），只写 audit_runs。

返回 ``TaskResult``：``data`` = ``{"item_id", "reason", "verdict", "output"}``。
"""

from __future__ import annotations

from typing import Any, Optional

from ..errors import CairnClientError
from .common import (
    CancelledError,
    PayloadError,
    TaskContext,
    TaskResult,
    emit_event,
    extract_json,
    run_worker_phase,
    validate_explore_payload,
)

#: audit 结论枚举（21 models.AuditVerdict）
AUDIT_VERDICTS = ("match", "coverage_discrepancy")

#: 覆盖项 status（展示用）
_COVERED_STATUS = ("tested_no_issue", "tested_with_finding")


def build_audit_prompt(
    *,
    item: dict,
    scope: str = "",
) -> str:
    """audit 提示：独立重测单个覆盖项，输出与 explore 同契约 + 独立 verdict。"""
    return f"""# Task
You are an INDEPENDENT auditor on an authorized penetration test. A coverage cell claims
to have been tested. Independently re-test it and verify the claim.

Coverage item:
- item_id: {item.get('id')}
- target: {item.get('target_value')} (id {item.get('target_id')})
- test_type: {item.get('test_type_name')} (id {item.get('test_type_id')})
- depth_required: {item.get('depth_required')}
- status: {item.get('status')}

# Output Requirements
Return only one raw JSON object (same shape as explore):
{{"accepted": true, "data": {{
  "description": "<what you actually found re-testing this cell>",
  "findings": [],
  "coverage": {{"covered_items": ["{item.get('id')}"], "depth_achieved": "standard",
    "outcome": "no_issue", "tested_scope": {{"endpoints": [], "partial": false}}}},
  "verdict": "match"
}}}}

# Rules
- `verdict` ∈ "match" | "coverage_discrepancy":
  - "match": the cell's claim holds (re-test produced the same/no result).
  - "coverage_discrepancy": the cell's claim does NOT hold (e.g. claimed finding but none
    reproducible, or claimed no_issue but a real issue is present).
- Base output ONLY on your independent re-test. Never trust the previous claim.
- NEVER output `complete`.

# Authorized scope
{scope}
"""


def run_audit(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    item: dict,
    scope: str = "",
    task_cfg: Optional[Any] = None,
    reason: str = "sampling",
    auditor: Optional[str] = None,
) -> TaskResult:
    """audit 单任务：独立重测覆盖项 → 落 audit verdict。"""
    timeout = getattr(task_cfg or ctx.config.tasks.audit, "timeout", 300)
    item_id = item.get("id") or item.get("item_id")
    prompt = build_audit_prompt(item=item, scope=scope)
    emit_event(ctx, "step", "info", f"audit: item={item_id} reason={reason} 独立重测")
    try:
        text, _sid = run_worker_phase(ctx, driver=driver, backend=backend, prompt=prompt, timeout=timeout, phase="audit")
    except CancelledError as exc:
        emit_event(ctx, "status", "warn", f"audit 被取消: {exc}")
        return TaskResult(status="cancelled", error=str(exc), error_code="CANCELLED")
    try:
        data = validate_explore_payload(extract_json(text), known_item_ids=[item_id] if item_id else ())
    except Exception as exc:  # noqa: BLE001
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))

    verdict = data.get("verdict")
    if verdict not in AUDIT_VERDICTS:
        # 兼容：无 verdict 字段时按 coverage.outcome 推断
        if data.get("coverage", {}).get("outcome") == "finding_created":
            verdict = "coverage_discrepancy"
        else:
            verdict = "match"

    # 落 audit_runs（21 apply_audit_verdict；POST /coverage/items/{cid}/audit）
    try:
        resp = ctx.client._request(
            "POST",
            f"/engagements/{ctx.eid}/coverage/items/{item_id}/audit",
            json={
                "verdict": verdict,
                "auditor": auditor or ctx.worker,
                "reason": reason if reason in ("sampling", "discrepancy", "manual") else "manual",
                "note": data.get("description"),
            },
        )
    except CairnClientError as exc:
        emit_event(ctx, "error", "warn", f"apply_audit_verdict 失败: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code=exc.error_code)

    emit_event(ctx, "status", "info", f"audit: verdict={verdict}")
    return TaskResult(
        status="success",
        data={"item_id": item_id, "reason": reason, "verdict": verdict, "output": data, "audit_run": resp},
        outcome_note=f"verdict={verdict}",
    )
