"""Dispatcher 任务层（Agent 30）—— 单任务执行纯函数/类 + 契约校验器。

任务（bootstrap/reason/explore/verify/audit）均为**单任务纯函数**，供 Agent 40 主循环
编排；replay 引擎（F4）为确定性重放（worker='replay-engine'，不走 LLM）。

模块映射：
- ``common``   任务上下文/结果 + 校验器（skeleton §4）+ 执行编排 + 写回重试 + 进度上报
- ``bootstrap`` 攻击面发现 + discoveries 播种 + sweep_complete
- ``reason``    缺口驱动收敛（intents / recommend_finalize，C8）
- ``explore``   覆盖项驱动 + findings + coverage_result 写回（B1/C2/C9）
- ``verify``    两阶段盲审（blind→comparison）→ verdict（F1/F7）
- ``audit``     覆盖抽样复核（独立重测高优先格子，F3）

任务模块（bootstrap/reason/explore/verify/audit）通过 PEP 562 惰性导入 ——
``findings/coverage`` 写回器在模块顶层 import ``tasks.common`` 时不会触发
``tasks.explore → findings.writer`` 的循环导入。

硬约束：不实现调度循环/worker 选择/心跳（40）；所有写回经 CairnClient（C5）。
"""

from __future__ import annotations

import importlib as _importlib
from typing import TYPE_CHECKING

from .common import (
    PayloadError,
    TaskContext,
    TaskError,
    TaskResult,
    WritebackError,
    emit_event,
    extract_json,
    now_iso,
    parse_accepted,
    run_conclude_phase,
    run_worker_phase,
    validate_bootstrap_payload,
    validate_coverage_result,
    validate_explore_payload,
    validate_findings_payload,
    validate_reason_payload,
    validate_replay_result,
    validate_verify_blind_payload,
    validate_verify_compare_payload,
)

#: 惰性导入的任务模块（避免 findings/coverage writer ↔ tasks 循环导入）
_LAZY_TASK_MODULES: dict[str, str] = {
    "run_bootstrap": ".bootstrap",
    "run_bootstrap_conclude": ".bootstrap",
    "run_reason": ".reason",
    "run_explore": ".explore",
    "run_explore_conclude": ".explore",
    "run_verify": ".verify",
    "select_verify_worker": ".verify",
    "run_audit": ".audit",
}


def __getattr__(name: str):
    if name in _LAZY_TASK_MODULES:
        mod = _importlib.import_module(_LAZY_TASK_MODULES[name], __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_LAZY_TASK_MODULES.keys()))


__all__ = [
    "TaskContext",
    "TaskResult",
    "TaskError",
    "PayloadError",
    "WritebackError",
    "emit_event",
    "extract_json",
    "now_iso",
    "parse_accepted",
    "run_worker_phase",
    "run_conclude_phase",
    "validate_bootstrap_payload",
    "validate_coverage_result",
    "validate_explore_payload",
    "validate_findings_payload",
    "validate_reason_payload",
    "validate_replay_result",
    "validate_verify_blind_payload",
    "validate_verify_compare_payload",
    "run_bootstrap",
    "run_bootstrap_conclude",
    "run_reason",
    "run_explore",
    "run_explore_conclude",
    "run_verify",
    "select_verify_worker",
    "run_audit",
]
