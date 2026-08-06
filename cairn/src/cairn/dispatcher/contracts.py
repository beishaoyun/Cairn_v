"""派发侧契约类型。

TaskType 的唯一事实来源。``replay`` 是**确定性引擎任务**
（worker='replay-engine'，不走 LLM），不属于 worker 驱动可声明的 task_type，
见 dispatch-config-spec.md §4/§9 与 backend-module-skeleton.md §3 TaskType 扩展。
"""

from __future__ import annotations

from typing import Literal

#: 全部任务类型（含引擎任务 replay）
TaskType = Literal["bootstrap", "reason", "explore", "verify", "audit", "replay"]

#: worker 驱动可承担的任务类型（replay 不在其中）
WorkerTaskType = Literal["bootstrap", "reason", "explore", "verify", "audit"]

#: worker 驱动枚举（+ 各 local 变体，spec §9）
WorkerType = Literal["claudecode", "codex", "pi", "mock"]

#: 执行后端（spec §3）
ExecutionMode = Literal["container", "local"]

#: 容器网络模式（spec §8；capture 模式必须 bridge）
NetworkMode = Literal["bridge", "host"]

#: worker 健康检查时机（spec §3）
HealthcheckMode = Literal["startup_and_task", "startup_only", "disabled"]

TASK_TYPES: tuple[str, ...] = ("bootstrap", "reason", "explore", "verify", "audit", "replay")
WORKER_TASK_TYPES: tuple[str, ...] = ("bootstrap", "reason", "explore", "verify", "audit")
WORKER_TYPES: tuple[str, ...] = ("claudecode", "codex", "pi", "mock")
EXECUTION_MODES: tuple[str, ...] = ("container", "local")
NETWORK_MODES: tuple[str, ...] = ("bridge", "host")
HEALTHCHECK_MODES: tuple[str, ...] = ("startup_and_task", "startup_only", "disabled")
CONTAINER_COMPLETED_ACTIONS: tuple[str, ...] = ("stop", "remove")
LOCAL_COMPLETED_ACTIONS: tuple[str, ...] = ("keep", "remove")
