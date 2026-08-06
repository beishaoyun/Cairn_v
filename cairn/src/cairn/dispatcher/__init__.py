"""Cairn v2 Dispatcher —— 调度执行器（本包：配置层 + Cairn Server 协议客户端）。

本包归 Agent 12（12-dispatcher-config）所有，仅包含：
- ``config.py``        dispatch.yaml 加载 / 校验 / ${ENV_VAR} 展开 / 默认值合并
- ``protocol/client``  CairnClient（Bearer + v2 §7.3 错误码解析）
- ``errors.py``        派发侧错误码（映射服务端 error_code）
- ``contracts.py``     TaskType 等契约类型

任务逻辑（bootstrap/reason/explore/verify/audit/replay）与调度主循环
分别由 Agent 30（dispatcher-tasks）与 Agent 40（dispatcher-loop）实现。
"""

from .config import (
    ConfigError,
    ContainerConfig,
    DispatcherConfig,
    LocalConfig,
    RuntimeConfig,
    ScopeConfig,
    SecurityConfig,
    ServerConfig,
    TaskConfig,
    TasksConfig,
    TuningConfig,
    WorkerConfig,
    load,
    load_dict,
    loads,
)
from .contracts import TASK_TYPES, WORKER_TASK_TYPES, TaskType, WorkerTaskType
from .errors import CairnClientError
from .protocol.client import CairnClient
from .scheduler import DispatcherLoop, run_dispatch_loop

__all__ = [
    "ConfigError",
    "CairnClient",
    "CairnClientError",
    "ContainerConfig",
    "DispatcherConfig",
    "DispatcherLoop",
    "LocalConfig",
    "RuntimeConfig",
    "ScopeConfig",
    "SecurityConfig",
    "ServerConfig",
    "TaskConfig",
    "TasksConfig",
    "TaskType",
    "TuningConfig",
    "WorkerConfig",
    "WorkerTaskType",
    "TASK_TYPES",
    "WORKER_TASK_TYPES",
    "load",
    "load_dict",
    "loads",
    "run_dispatch_loop",
]
