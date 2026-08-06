"""verify 任务（Agent 30）—— 两阶段盲审（blind→comparison）→ verdict。

契约（F1/F6/F7/C7，capture-verify-progress-spec §4，prompts §4）：
- 派发**排除创建该 finding 的 worker**（``verify_eligible`` 且 ≠ 创建者）；
  一次任务 = 两次顺序模型调用（blind 只喂 digest+scope → comparison 喂 observations+
  finding+digest），共用同一任务 run。
- ``verify_policy.verify_model`` 非空 → comparison（或整任务）换模型池 →
  ``independence=cross_model``（C7 硬独立性）；否则 cross_worker（同模型族标注有限）。
  单 worker 兜底降级 ``cross_run``（最终仍需人工确认，F7）。
- ``http_mismatch`` 比对在任务内完成：fetch ``resolve_traffic`` 全量，对 claim http[] 比对
  （C2：捕获字节为准；不一致 → 标记 http_mismatch → 按 needs_more_evidence 处理）。
- 落定调 22 ``apply_verify_runs``（POST /findings/{fid}/verify）。
- ``verdict=needs_more_evidence`` → ``reverify_count+1`` 受 ``max_reverify`` 上限（F6，
  超限升级 needs_review 由 22 服务端落实）。

返回 ``TaskResult``：``data`` = ``{"blind": ..., "comparison": ..., "independence": ...,
"http_mismatch": bool}``。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..errors import CairnClientError
from .common import (
    CancelledError,
    PayloadError,
    TaskContext,
    TaskError,
    TaskResult,
    emit_event,
    extract_json,
    run_worker_phase,
    validate_verify_blind_payload,
    validate_verify_compare_payload,
)

#: verify 独立级别（C7/F7；cross_worker 同模型族 → 标注「有限」）
INDEPENDENCE_CROSS_WORKER = "cross_worker"
INDEPENDENCE_CROSS_MODEL = "cross_model"
INDEPENDENCE_CROSS_RUN = "cross_run"

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})


def build_verify_blind_prompt(*, traffic_digest: str, scope: str = "") -> str:
    """阶段一（盲审）提示（prompts §4.1）：只给 digest + scope，不给 claim。"""
    return f"""# Task
You are an INDEPENDENT analyst on an authorized penetration test. You have NOT been told
what vulnerability to look for. Analyze the captured traffic and report what YOU see.

You receive:
1. `{{traffic_digest}}` — downsampled but byte-verified request/response exchanges from the
   transparent proxy (truncation is marked `... [truncated, sha256=...]`).
2. The authorized scope.

# Your job
Independently identify any vulnerabilities the traffic demonstrates. Assign YOUR OWN severity.

# Output Requirements
Return only one raw JSON object:
{{"accepted": true, "data": {{
  "observations": [{{"vuln": "...", "severity": "high", "traffic_id": "tr-001",
    "basis": "..."}}],
  "traffic_note": "no issue demonstrable / limited coverage"
}}}}

# Rules
- Base observations ONLY on the traffic digest. Do NOT assume what an attacker would do.
- Do NOT run new tools against the target in this phase.
- Honest negatives are welcome: `observations: []` is a valid answer.

# Context
## Traffic digest
{traffic_digest}
## Authorized scope
{scope}
"""


def build_verify_compare_prompt(
    *,
    observations: list[dict],
    finding: dict,
    traffic_digest: str,
    scope: str = "",
) -> str:
    """阶段二（对照）提示（prompts §4.2）：盲审观察 + finding claim + digest。"""
    obs_text = json.dumps(observations, ensure_ascii=False)
    finding_text = json.dumps(finding, ensure_ascii=False)
    return f"""# Task
You are the INDEPENDENT reviewer for an authorized penetration test. Below is your OWN
blind analysis of the captured traffic (stage 1), followed by a proposed finding from
another agent. Compare them and issue a verdict.

You receive:
1. `{{observations}}` — your stage-1 independent findings.
2. `{{finding}}` — the proposer's finding (title, severity, description, claimed http[],
   commands).
3. `{{traffic_digest}}` — the same byte-verified exchanges.

# Your job
Issue a verdict on whether the proposed finding is REAL and correctly rated.
Any mismatch between the proposer's `http[]` and the captured bytes → flag it.

# Output Requirements
Return only one raw JSON object:
{{"accepted": true, "data": {{
  "stage": "comparison",
  "verdict": "confirmed",
  "verified_severity": "high",
  "reason": "...",
  "verified_traffic_ids": ["tr-001"],
  "http_mismatch": false,
  "suggested_action": "none"
}}}}

# Rules
- `confirmed` ONLY if the claim is supported by your own observations + the digest.
- `needs_more_evidence` if the traffic cannot demonstrate the root cause.
- You do NOT modify the finding; you only emit a verdict.

# Context
## Observations (stage 1)
{obs_text}
## Proposed finding
{finding_text}
## Traffic digest
{traffic_digest}
## Authorized scope
{scope}
"""


def select_verify_worker(
    creator_worker: str,
    workers: list[Any],
    *,
    independence: str = INDEPENDENCE_CROSS_WORKER,
) -> Optional[str]:
    """verify 派发选择函数（F1/F7）：排除创建者 + 仅 ``verify_eligible``。

    40 的 loop 接入本函数做 worker 选择：
    - 返回一个可复核 worker 名（≠ 创建者，verify_eligible=True）；
    - 无独立 worker 时返回 ``None``（由 40 决定降级 ``cross_run`` 或标记等待独立复核）。
    输入 ``workers`` 为 ``WorkerConfig``（含 name/verify_eligible）列表。
    """
    candidates = [
        w.name
        for w in workers
        if w.name != creator_worker and getattr(w, "verify_eligible", True)
    ]
    if not candidates:
        return None
    # 稳定选择：按 priority 降序，再按 name 字典序（确定性，便于测试）
    candidates.sort(
        key=lambda n: (
            -getattr(next((w for w in workers if w.name == n), None), "priority", 0),
            n,
        )
    )
    return candidates[0]


def resolve_finding_traffic_digest(
    ctx: TaskContext,
    *,
    eid: str,
    traffic_ids: list[str],
) -> str:
    """聚合 finding 关联流量的 digest（F2：verify 输入 ≤digest_budget；只喂 digest）。"""
    parts: list[str] = []
    for tid in traffic_ids:
        try:
            meta = ctx.client.resolve_traffic(eid, tid, for_model=True)
        except CairnClientError as exc:
            ctx.log(f"resolve_traffic(digest) 失败: {tid} {exc}")
            continue
        digest = meta.get("digest") if isinstance(meta, dict) else None
        if digest:
            parts.append(f"### traffic {tid}\n{digest}")
    return "\n\n".join(parts) or "(无可用捕获流量 digest)"


def detect_http_mismatch(
    ctx: TaskContext,
    *,
    eid: str,
    finding: dict,
) -> bool:
    """C2 http_mismatch 比对：agent claim 的 http[] 与捕获全量字节是否一致。

    不一致 → True（verify 按 needs_more_evidence 处理，捕获字节为准，不得冒充）。
    无 http[]/无捕获 → False（命令证据型不适用）。best-effort（resolve 失败不阻断）。
    """
    http_claims = finding.get("http_evidence") or finding.get("http") or []
    if not http_claims:
        return False
    mismatches = 0
    for h in http_claims:
        tid = h.get("traffic_id")
        if not tid:
            continue
        try:
            full = ctx.client.resolve_traffic(eid, tid, for_model=False)
        except CairnClientError:
            continue
        captured = {
            "method": full.get("method"),
            "url": full.get("url"),
            "request_body": _body_of(full.get("request")),
            "response_status": full.get("status"),
            "response_body": _body_of(full.get("response")),
        }
        claimed = {
            "method": h.get("method"),
            "url": h.get("url"),
            "request_body": h.get("request_body"),
            "response_status": h.get("response_status"),
            "response_body": h.get("response_body"),
        }
        # 语义比对：method/url/status 必须一致；body 允许截断差异（agent 只写语义注释）
        if claimed["method"] and captured["method"] and claimed["method"].upper() != captured["method"].upper():
            mismatches += 1
        if claimed["url"] and captured["url"] and claimed["url"] != captured["url"]:
            mismatches += 1
        if claimed["response_status"] is not None and captured["response_status"] is not None and (
            int(claimed["response_status"]) != int(captured["response_status"])
        ):
            mismatches += 1
    return mismatches > 0


def _body_of(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def run_verify(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    finding: dict,
    eid: str,
    scope: str = "",
    verify_policy: Optional[dict] = None,
    task_cfg: Optional[Any] = None,
    independence: str = INDEPENDENCE_CROSS_WORKER,
    max_reverify: int = 3,
) -> TaskResult:
    """verify 单任务：两次顺序模型调用（blind→comparison）→ 落定 22。

    ``independence``：由 40 依派发结果传入（cross_worker/cross_model/cross_run）。
    ``verify_model`` 非空时 comparison 阶段换模型池 → independence=cross_model（由 40
    传入相应 driver 或在 comparison 前切换；本函数接受 ``verify_policy.verify_model``
    标记以记录 independence=cross_model）。
    """
    timeout = getattr(task_cfg or ctx.config.tasks.verify, "timeout", 300)
    policy = verify_policy or {}
    fid = finding.get("id")
    traffic_ids = _finding_traffic_ids(finding)
    digest = resolve_finding_traffic_digest(ctx, eid=eid, traffic_ids=traffic_ids)

    emit_event(ctx, "step", "info", f"verify: finding={fid} 阶段一 blind（独立观察，不喂 claim）")
    blind_prompt = build_verify_blind_prompt(traffic_digest=digest, scope=scope)
    try:
        blind_text, sid = run_worker_phase(ctx, driver=driver, backend=backend, prompt=blind_prompt, timeout=timeout, phase="verify", stage="blind")
    except CancelledError as exc:
        return TaskResult(status="cancelled", error=str(exc), error_code="CANCELLED")
    try:
        blind = validate_verify_blind_payload(extract_json(blind_text))
    except TaskError as exc:
        # accepted=false / 模型拒绝 → 任务 rejected（TV-16；F1 不落任何字段）
        if getattr(exc, "error_code", "") == "MODEL_REJECTED":
            emit_event(ctx, "error", "warn", f"verify 模型拒绝（accepted=false）: {exc}")
            return TaskResult(status="rejected", error=str(exc), error_code="MODEL_REJECTED")
        emit_event(ctx, "error", "warn", f"verify blind 契约失败: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))
    except Exception as exc:  # noqa: BLE001
        emit_event(ctx, "error", "warn", f"verify blind 契约失败: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))
    observations = blind.get("observations") or []

    # 阶段二 comparison（verify_model 非空 → independence=cross_model，C7）
    verify_model = policy.get("verify_model")
    if verify_model:
        independence = INDEPENDENCE_CROSS_MODEL
    emit_event(ctx, "step", "info", f"verify: 阶段二 comparison（independence={independence}）")
    compare_prompt = build_verify_compare_prompt(
        observations=observations, finding=finding, traffic_digest=digest, scope=scope
    )
    try:
        cmp_text, _sid = run_worker_phase(
            ctx, driver=driver, backend=backend, prompt=compare_prompt, timeout=timeout, session_id=sid,
            phase="verify", stage="comparison",
        )
    except CancelledError as exc:
        return TaskResult(status="cancelled", error=str(exc), error_code="CANCELLED")
    try:
        cmp = validate_verify_compare_payload(extract_json(cmp_text), traffic_ids=traffic_ids)
    except TaskError as exc:
        if getattr(exc, "error_code", "") == "MODEL_REJECTED":
            emit_event(ctx, "error", "warn", f"verify comparison 模型拒绝（accepted=false）: {exc}")
            return TaskResult(status="rejected", error=str(exc), error_code="MODEL_REJECTED")
        emit_event(ctx, "error", "warn", f"verify comparison 契约失败: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))
    except Exception as exc:  # noqa: BLE001
        emit_event(ctx, "error", "warn", f"verify comparison 契约失败: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))

    verdict = cmp.get("verdict")
    http_mismatch = bool(cmp.get("http_mismatch")) or detect_http_mismatch(ctx, eid=eid, finding=finding)
    # C2：捕获字节不一致 → 按 needs_more_evidence 处理（不落 agent 手写内容为 captured）
    effective_verdict = verdict
    if verdict == "confirmed" and http_mismatch:
        effective_verdict = "needs_more_evidence"
        emit_event(ctx, "status", "warn", "verify: http_mismatch 检测到 → 降级 needs_more_evidence")
    # P0 告警（TV-03）：critical 复核 confirmed → error 级事件（前端活动面板置红）
    if effective_verdict == "confirmed" and cmp.get("verified_severity") == "critical":
        emit_event(ctx, "error", "error", f"P0 告警：critical 级漏洞复核 confirmed（finding={fid}）")

    # 落定 22 apply_verify_runs（POST /findings/{fid}/verify）
    apply_payload = {
        "task_run_id": ctx.run_id,
        "stage": "comparison",
        "independence": independence,
        "input_traffic_digest": digest,
        "observations": observations,
        "verdict": effective_verdict,
        "verified_severity": cmp.get("verified_severity"),
        "reason": cmp.get("reason"),
        "verified_traffic_ids": cmp.get("verified_traffic_ids") or [],
        "suggested_action": cmp.get("suggested_action"),
        "actor": ctx.worker,
    }
    try:
        ctx.client._request("POST", f"/engagements/{eid}/findings/{fid}/verify", json=apply_payload)
    except CairnClientError as exc:
        emit_event(ctx, "error", "warn", f"apply_verify_runs 失败: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code=exc.error_code)

    emit_event(ctx, "status", "info", f"verify: verdict={effective_verdict} severity={cmp.get('verified_severity')}")
    return TaskResult(
        status="success",
        data={
            "blind": blind,
            "comparison": cmp,
            "independence": independence,
            "http_mismatch": http_mismatch,
            "verdict": effective_verdict,
        },
        outcome_note=f"verdict={effective_verdict}, independence={independence}",
    )


def _finding_traffic_ids(finding: dict) -> list[str]:
    """提取 finding 的 traffic ids（traffic_links / verified_traffic_ids / traffic_ids 字段）。"""
    tids: list[str] = []
    for link in finding.get("traffic_links") or []:
        if link.get("traffic_id"):
            tids.append(link["traffic_id"])
    for key in ("traffic_ids", "verified_traffic_ids"):
        for t in finding.get(key) or []:
            if t not in tids:
                tids.append(t)
    return tids
