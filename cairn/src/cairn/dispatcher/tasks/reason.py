"""reason 任务（Agent 30）—— 缺口驱动收敛（gaps 输入 → intents / recommend_finalize）。

契约（coverage spec §3.1 / prompts §1 / skeleton §3）：
- 输入 ``{gaps}``：21 的 ``compute_gaps(exclude_in_progress=True)``（B1：in_progress 格
  已被认领，不得再派第二个 explore）。
- 输出 ``intents[]``（每条引用 ≥1 未覆盖项）**或** ``coverage.recommend_finalize=true +
  waivers[]``（**建议**，人工批准才生效，B4/C8）。
- 覆盖未满（高优先缺口存在）且两者都缺 → 任务失败 + ``escalate=True``（C8 空转保护，
  升级 needs_review，停止自动重试；计数落 scheduler_state 由 40 持久化）。
- ``complete`` 字段被拒。
- 本任务只产出 intents（纯函数），图持久化（create_intent/claim）由 40 编排。

返回 ``TaskResult``：``data`` = ``{"intents": [...], "coverage": {...}, "gaps": [...]}``。
失败时 ``error_code='REASON_CONVERGENCE'``、``escalate=True``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .common import (
    CancelledError,
    PayloadError,
    TaskContext,
    TaskResult,
    extract_json,
    emit_event,
    run_worker_phase,
    validate_reason_payload,
)


def build_reason_prompt(
    *,
    graph_yaml: str,
    gaps: list[dict],
    scope: str,
    min_priority_threshold: float = 0.30,
) -> str:
    """按 prompts §1 模板渲染 reason 提示（占位符 graph_yaml/gaps/scope）。"""
    gaps_text = json.dumps(gaps, ensure_ascii=False)
    return f"""# Task
You are the coverage accountant of an authorized penetration test engagement.

You receive:
1. A YAML snapshot of the fact graph — facts are confirmed objective findings,
   intents are exploration edges.
2. A coverage gap list `{{gaps}}` — every item is an (asset × test type) cell that is
   NOT yet tested. This is the ONLY set of things you may propose to explore.
3. The engagement's authorized scope `{{scope}}` — targets in scope, prohibited targets,
   and the authorization window.

# Your job
Decide the highest-value next exploration directions. You may ONLY propose intents
that reference uncovered coverage items. Do NOT re-test already covered cells.

# Output Requirements
Return only one raw JSON object:
- Propose new intents covering the most valuable gaps:
  {{"accepted": true, "data": {{"intents": [{{"from": ["f003"], "description": "...",
    "coverage_item_ids": ["c-013"]}}], "coverage": {{"recommend_finalize": false, "reason": ""}}}}}}
- If the remaining gaps are all low-value (below the priority threshold) and you
  believe further testing yields little, recommend finalize:
  {{"accepted": true, "data": {{"intents": [], "coverage": {{"recommend_finalize": true,
   "reason": "high-priority cells covered; remaining are low-value",
   "waivers": [{{"item_id": "c-099", "kind": "not_applicable", "reason": "..."}}]}}}}}}

# Rules
- NEVER output `complete`. In penetration testing there is no provable "goal reached".
- If high-priority gaps (priority >= {min_priority_threshold}) exist, you MUST propose
  intents OR recommend finalize with waivers.
- `from` must be valid fact ids from the graph (never `goal`).
- Each intent must reference ≥1 uncovered coverage item from `{{gaps}}`.
- Waivers are SUGGESTIONS only — a human must approve them.

# Context
## Graph snapshot
{graph_yaml}
## Coverage gaps (uncovered cells, priority-sorted)
{gaps_text}
## Authorized scope
{scope}
"""


def high_priority_gap_exists(gaps: list[dict], *, threshold: float = 0.30) -> bool:
    """收敛硬约束前提：存在 ``priority >= threshold`` 的缺口。"""
    return any(float(g.get("priority", 0) or 0) >= threshold for g in gaps)


def run_reason(
    ctx: TaskContext,
    *,
    driver: Any,
    backend: Any,
    gaps: list[dict],
    graph_yaml: str,
    scope: str = "",
    task_cfg: Optional[Any] = None,
    min_priority_threshold: float = 0.30,
) -> TaskResult:
    """reason execute 阶段（收敛账目）。"""
    timeout = getattr(task_cfg or ctx.config.tasks.reason, "timeout", 300)
    max_intents = getattr(task_cfg or ctx.config.tasks.reason, "max_intents", None)
    gap_ids = [g["item_id"] for g in gaps if g.get("item_id")]
    prompt = build_reason_prompt(
        graph_yaml=graph_yaml,
        gaps=gaps,
        scope=scope,
        min_priority_threshold=min_priority_threshold,
    )
    emit_event(ctx, "step", "info", f"reason: 缺口 {len(gap_ids)} 项，开始收敛账目")
    try:
        text, _sid = run_worker_phase(ctx, driver=driver, backend=backend, prompt=prompt, timeout=timeout, phase="reason")
    except CancelledError as exc:
        emit_event(ctx, "status", "warn", f"reason 被取消: {exc}")
        return TaskResult(status="cancelled", error=str(exc), error_code="CANCELLED")
    except PayloadError as exc:
        emit_event(ctx, "error", "warn", f"reason 输出为空: {exc}")
        return TaskResult(status="failed", error=str(exc), error_code="VALIDATION")

    hp = high_priority_gap_exists(gaps, threshold=min_priority_threshold)
    try:
        payload = extract_json(text)
        # valid_fact_ids 由 graph_yaml 解析（from 引用校验）；无法解析时放行（facts=()）
        data = validate_reason_payload(
            payload,
            gap_item_ids=gap_ids,
            valid_fact_ids=_fact_ids_from_yaml(graph_yaml),
            max_intents=max_intents,
            high_priority_gaps=hp,
        )
    except Exception as exc:  # noqa: BLE001 —— 收敛约束失败 → 任务失败 + 升级（C8）
        code = getattr(exc, "error_code", "VALIDATION")
        emit_event(ctx, "error", "warn", f"reason 校验失败: {exc}")
        # 收敛硬约束失败 → escalate=True（C8 空转保护；计数持久化由 40 落 scheduler_state）
        escalate = "收敛约束" in str(exc) or code == "VALIDATION"
        return TaskResult(
            status="failed",
            error=str(exc),
            error_code="REASON_CONVERGENCE" if hp else code,
            escalate=hp and not data_is_nonempty(payload, "intents"),
        )

    intents = data.get("intents") or []
    coverage = data.get("coverage") or {}
    emit_event(ctx, "output", "info", f"reason: intents={len(intents)} finalize={coverage.get('recommend_finalize')}")
    return TaskResult(
        status="success",
        data={"intents": intents, "coverage": coverage, "gaps": gaps},
        outcome_note=f"intents={len(intents)}, finalize={coverage.get('recommend_finalize')}",
    )


def data_is_nonempty(payload: dict, key: str) -> bool:
    """辅助：payload.data.<key> 是否非空（用于判定 escalate 语义）。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return False
    return bool(data.get(key))


def _fact_ids_from_yaml(graph_yaml: str) -> tuple[str, ...]:
    """从图 YAML 快照解析合法 fact id（``id: f###``）。解析失败返回空（放行 from 校验）。"""
    ids: list[str] = []
    for line in (graph_yaml or "").splitlines():
        s = line.strip()
        if s.startswith("id:") and ":" in s:
            fid = s.split(":", 1)[1].strip().strip("'\"")
            if fid.startswith("f") and fid[1:].isdigit():
                ids.append(fid)
    return tuple(ids)


def escalation_state_key(eid: str) -> str:
    """scheduler_state key（C8；21 的 ``REASON_ESCALATION_KEY`` 同值）：``reason_escalation:{eid}``。"""
    return f"reason_escalation:{eid}"


@dataclass
class ReasonEscalation:
    """C8 reason 空转升级计数器（供 40 持久化到 ``scheduler_state:reason_escalation:{eid}``）。

    语义（coverage spec §2 DEFAULT_COVERAGE_POLICY.reason_escalation）：
    - ``record_failure`` 连续校验失败（无 intent 无 finalize）→ ``consecutive_failures+1``；
    - ``record_failure(finalize_rejected=True)`` 建议 finalize 被人工拒绝 → ``finalize_rejected+1``；
    - 超限（或显式置 ``escalated``）→ reason 升级 needs_review，停止自动重试，仅人工恢复。

    40 的 loop 在每次 reason 任务失败时调 ``record_failure``，并把 ``snapshot(eid)`` 写
    scheduler_state（重启不丢）；``run_reason`` 返回的 ``TaskResult.escalate=True`` 即信号。
    """

    max_consecutive_failures: int = 3
    max_finalize_rejected: int = 3
    _counts: dict[str, dict] = field(default_factory=dict)

    def record_failure(self, eid: str, *, finalize_rejected: bool = False) -> dict:
        st = self._counts.setdefault(
            eid, {"consecutive_failures": 0, "finalize_rejected": 0, "escalated": False}
        )
        if finalize_rejected:
            st["finalize_rejected"] += 1
        else:
            st["consecutive_failures"] += 1
        if (
            st["consecutive_failures"] >= self.max_consecutive_failures
            or st["finalize_rejected"] >= self.max_finalize_rejected
        ):
            st["escalated"] = True
        return dict(st)

    def reset(self, eid: str) -> None:
        self._counts.pop(eid, None)

    def snapshot(self, eid: str) -> Optional[dict]:
        st = self._counts.get(eid)
        return dict(st) if st else None

    def load(self, eid: str, state: dict) -> None:
        self._counts[eid] = {
            "consecutive_failures": int(state.get("consecutive_failures", 0)),
            "finalize_rejected": int(state.get("finalize_rejected", 0)),
            "escalated": bool(state.get("escalated", False)),
        }
