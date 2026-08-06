"""bootstrap 任务（Agent 30）—— 攻击面发现 + discoveries 播种 + sweep_complete。

契约（coverage spec §3.3 / prompts §5/§6）：
- 输出 ``{fact, sweep_complete, discoveries[], coverage}``；``complete`` 字段**被拒**
  （黄金不变量 5；bootstrap 用 ``sweep_complete`` 表「初探完成」，绝非项目完成）。
- discoveries 触发 ``coverage.writer.seed_from_discovery`` 播种覆盖项（目标 + 测试项）。
- 任务超时来源 ``tasks.bootstrap.timeout``；取消/kill switch 经 cancellation 全程挂载。
- 提供 execute 与 conclude（二阶段收尾）两个纯函数，供 40 编排。

返回 ``TaskResult``：``data`` = ``{"fact": ..., "sweep_complete": ..., "discoveries": [...],
"coverage": {...}, "seeded": {...}}``。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..coverage.writer import CoverageWriter
from .common import (
    CancelledError,
    PayloadError,
    TaskContext,
    TaskResult,
    extract_json,
    emit_event,
    run_conclude_phase,
    run_worker_phase,
    validate_bootstrap_payload,
    writeback_retries,
)


def build_bootstrap_prompt(
    *,
    origin: str,
    goal: str,
    hints: Any = None,
    scope: str = "",
) -> str:
    """按 prompts §5 模板渲染 bootstrap 提示（占位符 origin/goal/hints/scope）。"""
    hints_text = ""
    if hints:
        try:
            hints_text = json.dumps(hints, ensure_ascii=False)
        except (TypeError, ValueError):
            hints_text = str(hints)
    return f"""# Task
You are starting an authorized penetration test engagement. There is NO provable "completion" —
you only perform an initial sweep:
1. Discover the attack surface of the authorized scope (hosts, ports, services, web apps).
2. Write key recon results as facts.
3. Report `discoveries` so the platform can seed the coverage matrix.

# Output Requirements
Return only one raw JSON object:
{{"accepted": true, "data": {{
  "fact": {{"description": "<key recon findings, concise>"}},
  "sweep_complete": {{"description": "initial sweep done, attack surface recorded"}},
  "discoveries": [{{"target": "10.0.0.5", "port": 80, "service": "http"}}],
  "coverage": {{"outcome": "no_issue"}}
}}}}

# Rules
- Only touch assets inside the authorized scope. Prohibited targets are off-limits.
- `discoveries` must match real results; the platform seeds coverage items from it.
- If you cannot finish within budget, still output the partial discoveries you made.
- NEVER output a field named `complete` — that concept is removed from the platform.

# Context
## Origin
{origin}
## Goal
{goal}
## Hints
{hints_text}
## Authorized scope
{scope}
"""


def build_bootstrap_conclude_prompt() -> str:
    """bootstrap_conclude 提示（prompts §6）：收尾只总结已确认成果。"""
    return """# Task
Conclude phase for the initial sweep. STOP all work. Summarize only already-confirmed
recon findings and the partial discoveries.

# Output Requirements
Return only one raw JSON object:
{"accepted": true, "data": {"fact": {"description": "<confirmed recon summary>"},
  "discoveries": [...], "coverage": {"outcome": "no_issue"}}}

# Rules
- Base output ONLY on already-confirmed information. Do not plan, do not wait.
- Never output a field named `complete`.
"""


def run_bootstrap(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    origin: str,
    goal: str,
    hints: Any = None,
    scope: str = "",
    task_cfg: Optional[Any] = None,
) -> TaskResult:
    """bootstrap execute 阶段（含 discoveries 播种）。"""
    timeout = getattr(task_cfg or ctx.config.tasks.bootstrap, "timeout", 300)
    prompt = build_bootstrap_prompt(origin=origin, goal=goal, hints=hints, scope=scope)
    emit_event(ctx, "step", "info", "bootstrap: 开始攻击面发现")
    try:
        text, _sid = run_worker_phase(ctx, driver=driver, backend=backend, prompt=prompt, timeout=timeout, phase="bootstrap")
    except CancelledError as exc:
        emit_event(ctx, "status", "warn", f"bootstrap 被取消: {exc}")
        return TaskResult(status="cancelled", error=str(exc), error_code="CANCELLED")
    except PayloadError as exc:
        emit_event(ctx, "error", "warn", f"bootstrap 输出为空/解析失败: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code="VALIDATION")
    try:
        payload = extract_json(text)
        data = validate_bootstrap_payload(payload)
    except Exception as exc:  # noqa: BLE001 —— 契约校验失败
        emit_event(ctx, "error", "warn", f"bootstrap 契约校验失败: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))
    emit_event(ctx, "output", "info", f"bootstrap: fact={data['fact'].get('description')!r}")
    emit_event(ctx, "step", "info", f"bootstrap: discoveries={len(data.get('discoveries', []))} 条")

    # discoveries 播种（best-effort；失败不阻断任务，记录 seeded.skipped）
    seeded: dict[str, Any] = {"targets_created": 0, "items_created": 0, "skipped": []}
    if data.get("discoveries"):
        writer = CoverageWriter(ctx.client, log=ctx.log, retries=writeback_retries(ctx))
        seeded = writer.seed_from_discovery(ctx.eid, data["discoveries"], scope=scope)
        emit_event(ctx, "command", "info", f"bootstrap 播种: targets={seeded.get('targets_created')} items={seeded.get('items_created')}")

    emit_event(ctx, "status", "info", "bootstrap: sweep_complete")
    return TaskResult(
        status="success",
        data={**data, "seeded": seeded},
        outcome_note=f"discoveries={len(data.get('discoveries', []))}, seeded_items={seeded.get('items_created')}",
    )


def run_bootstrap_conclude(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    session_id: Optional[str] = None,
    task_cfg: Optional[Any] = None,
) -> TaskResult:
    """bootstrap conclude 阶段（收尾，prompts §6）。"""
    timeout = getattr(task_cfg or ctx.config.tasks.bootstrap, "conclude_timeout", 90)
    prompt = build_bootstrap_conclude_prompt()
    emit_event(ctx, "step", "info", "bootstrap conclude: 收尾总结")
    try:
        text, _sid = run_conclude_phase(ctx, driver=driver, backend=backend, prompt=prompt, timeout=timeout, session_id=session_id, phase="bootstrap_conclude")
    except CancelledError as exc:
        emit_event(ctx, "status", "warn", f"bootstrap conclude 被取消: {exc}")
        return TaskResult(status="cancelled", error=str(exc), error_code="CANCELLED")
    try:
        data = validate_bootstrap_payload(extract_json(text))
    except Exception as exc:  # noqa: BLE001
        return TaskResult(status="failed", error=str(exc), error_code=getattr(exc, "error_code", "VALIDATION"))
    return TaskResult(status="success", data=data, outcome_note="bootstrap conclude done")
