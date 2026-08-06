"""Dispatcher 调度子域（Agent 40）。

- ``loop``           主循环（guards / 任务触发 / 状态落库 / reconcile / periodic）
- ``worker_select``  worker 选择（优先级 / 冷却 / verify 排除创建者 / 并发闸）
"""

from .loop import DispatcherLoop, run_dispatch_loop
from .worker_select import (
    can_dispatch,
    filter_eligible,
    filter_ready,
    is_replay_engine_task,
    select_verify_worker,
    select_worker,
    sort_by_priority,
)

__all__ = [
    "DispatcherLoop",
    "run_dispatch_loop",
    "select_worker",
    "select_verify_worker",
    "filter_eligible",
    "filter_ready",
    "sort_by_priority",
    "is_replay_engine_task",
    "can_dispatch",
]
