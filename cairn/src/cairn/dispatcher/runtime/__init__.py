"""Dispatcher runtime — execution backend abstraction + cancellation + CLI context.

Owned by Agent 13. Agent 11 implements `ExecutionBackend`/`ExecProcess` (container
backend, local backend, process models) against the protocols defined in `backend.py`.
"""

from .backend import ExecProcess, ExecutionBackend
from .cancellation import TaskCancellation
from .containers import (
    ContainerBackend,
    ContainerBackendError,
    ContainerScope,
    resolve_scope_policy,
)
from .context import DispatcherContext
from .local_backend import LocalBackend, LocalBackendError
from .process import (
    CONTAINER_PID_MARKER,
    ContainerProcess,
    LocalProcess,
    ProcessError,
    resolve_workspace_path,
)

__all__ = [
    "ExecProcess",
    "ExecutionBackend",
    "TaskCancellation",
    "DispatcherContext",
    "ContainerBackend",
    "ContainerBackendError",
    "ContainerScope",
    "resolve_scope_policy",
    "LocalBackend",
    "LocalBackendError",
    "LocalProcess",
    "ContainerProcess",
    "ProcessError",
    "CONTAINER_PID_MARKER",
    "resolve_workspace_path",
]
