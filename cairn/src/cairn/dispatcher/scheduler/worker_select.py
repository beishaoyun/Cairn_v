"""Worker 选择（Agent 40 · scheduler/worker_select.py）。

纯逻辑、无 I/O —— 供主循环 ``loop.py`` 在每轮派发前调用，且可被单元测试直接注入
不同并发/冷却快照验证选择结果。

选择规则（40 提示词 §C + v2 §8.2 + capture §4.1）：
- **优先级**：``WorkerConfig.priority`` 大者优先（与 30 ``select_verify_worker`` 同口径；
  同优先按 name 字典序，确定性）。
- **冷却**：worker 在 ``health`` 冷却期内（unhealthy）或 ``rejected_until`` 墙钟未到 →
  跳过（``tuning.worker_unhealthy_cooldown_seconds`` / ``worker_rejected_cooldown_seconds``）。
- **per-worker max_running**：``running_counts[worker] >= max_running`` → 跳过。
- **task_types**：worker 必须声明该 task_type。
- **verify 独立**（F1/F7）：排除创建者 + 仅 ``verify_eligible``；无独立 worker →
  返回 ``None``（由调用方决定降级 ``cross_run`` 或等待）。
- **replay-engine 特例**：``replay`` 是确定性引擎任务（worker='replay-engine'，不走
  worker 列表），不在本模块选择范围（由 30 的 ``ReplayEngine`` 直接执行）。

输入 ``workers`` 为 ``WorkerConfig``（12 config）或 duck-type 对象（31 mock harness
的 ``build_mock_workers`` 产出），读取 name/type/task_types/max_running/priority/
verify_eligible。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Mapping, Optional


def _name(w: Any) -> str:
    return getattr(w, "name", "")


def filter_eligible(
    workers: Iterable[Any],
    task_type: str,
) -> list[Any]:
    """只保留声明了 ``task_type`` 的 worker。"""
    return [
        w
        for w in workers
        if task_type in (getattr(w, "task_types", None) or ())
    ]


def filter_ready(
    workers: Iterable[Any],
    *,
    health: Any = None,
    rejected_until: Optional[Mapping[str, float]] = None,
    running_counts: Optional[Mapping[str, int]] = None,
    now: Optional[float] = None,
) -> list[Any]:
    """过滤掉不可用的 worker（健康冷却 / rejected 冷却 / 达到 per-worker max_running）。

    ``health`` 暴露 ``is_unhealthy(name) -> bool``（13 的 WorkerHealth）。
    ``rejected_until``：``{worker: wall_clock_deadline}``（40 loop 维护，落 scheduler_state）。
    ``running_counts``：``{worker: 当前运行数}``。
    """
    rejected = rejected_until or {}
    running = running_counts or {}
    now = now if now is not None else time.time()
    out: list[Any] = []
    for w in workers:
        name = _name(w)
        if health is not None and getattr(health, "is_unhealthy", lambda _n: False)(name):
            continue
        if rejected.get(name, 0.0) > now:
            continue
        max_running = int(getattr(w, "max_running", 1) or 1)
        if running.get(name, 0) >= max_running:
            continue
        out.append(w)
    return out


def sort_by_priority(workers: Iterable[Any]) -> list[Any]:
    """按 priority 降序（大者优先），同优先按 name 字典序。"""
    return sorted(workers, key=lambda w: (-int(getattr(w, "priority", 0) or 0), _name(w)))


def select_worker(
    workers: Iterable[Any],
    *,
    task_type: str,
    health: Any = None,
    rejected_until: Optional[Mapping[str, float]] = None,
    running_counts: Optional[Mapping[str, int]] = None,
    creator: Optional[str] = None,
    verify_eligible_only: bool = False,
) -> Optional[str]:
    """选择承担 ``task_type`` 的 worker 名；无可用 worker 返回 ``None``。

    - ``creator``（verify 用）：排除创建该 finding 的 worker（F1 独立性）；
    - ``verify_eligible_only``（verify 用）：仅选 ``verify_eligible=True`` 的 worker；
    - 其余任务（bootstrap/reason/explore/audit）无需这两项。
    """
    candidates = filter_eligible(workers, task_type)
    if verify_eligible_only:
        candidates = [w for w in candidates if getattr(w, "verify_eligible", True)]
    if creator:
        candidates = [w for w in candidates if _name(w) != creator]
    candidates = filter_ready(
        candidates,
        health=health,
        rejected_until=rejected_until,
        running_counts=running_counts,
    )
    if not candidates:
        return None
    return _name(sort_by_priority(candidates)[0])


def select_verify_worker(
    creator_worker: str,
    workers: Iterable[Any],
    *,
    health: Any = None,
    rejected_until: Optional[Mapping[str, float]] = None,
    running_counts: Optional[Mapping[str, int]] = None,
    independence: str = "cross_worker",
) -> Optional[str]:
    """verify 独立复核 worker 选择（F1/F7）。

    复用 30 ``tasks.verify.select_verify_worker`` 的语义（排除创建者 + verify_eligible +
    priority 排序），叠加 40 的冷却/并发过滤。返回可复核 worker 名或 ``None``
    （无独立 worker → 调用方降级 ``cross_run``）。
    """
    return select_worker(
        workers,
        task_type="verify",
        health=health,
        rejected_until=rejected_until,
        running_counts=running_counts,
        creator=creator_worker,
        verify_eligible_only=True,
    )


def is_replay_engine_task(task_type: str) -> bool:
    """``replay`` 是确定性引擎任务（worker='replay-engine'），不占 worker 并发。"""
    return task_type == "replay"


def _can_dispatch_global(
    *,
    running_projects: int,
    max_running_projects: int,
    running_tasks: int,
    max_workers: int,
) -> bool:
    """全局并发闸：项目数 / 任务总数。纯逻辑（供测试直接断言）。"""
    if running_projects >= max_running_projects:
        return False
    if running_tasks >= max_workers:
        return False
    return True


def can_dispatch(
    *,
    running_projects: int,
    max_running_projects: int,
    running_tasks: int,
    max_workers: int,
    eid_running: int = 0,
    max_project_workers: int = 0,
) -> bool:
    """组合并发闸（全局 + per-engagement per-project）。返回是否允许再派发一个任务。"""
    if running_projects >= max_running_projects:
        return False
    if running_tasks >= max_workers:
        return False
    if max_project_workers > 0 and eid_running >= max_project_workers:
        return False
    return True


__all__ = [
    "filter_eligible",
    "filter_ready",
    "sort_by_priority",
    "select_worker",
    "select_verify_worker",
    "is_replay_engine_task",
    "can_dispatch",
]
