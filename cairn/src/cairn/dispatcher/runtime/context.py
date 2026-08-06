"""Dispatcher runtime context (owned by Agent 13).

The object Agent 13's CLI assembly hands to Agent 40's scheduler loop entry.
Loop contract (frozen):
    def run_dispatch_loop(ctx: DispatcherContext) -> int
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class DispatcherContext:
    """Everything the scheduler loop needs that the CLI has already prepared.

    Attributes
    ----------
    config:
        The loaded dispatch config (Agent 12 ``DispatcherConfig``).
    drivers:
        ``{worker_name: WorkerDriver}`` built by Agent 13's registry (container-mode
        LLM env keys already validated — missing keys abort startup, graph §4-23).
    health:
        ``WorkerHealth`` (startup/task two-level checks + cooldown, v2 §11.1-10).
    shutdown:
        A ``threading.Event`` set on SIGTERM/SIGINT. The loop should poll it between
        operations and stop cleanly when set.
    grace_seconds:
        SIGTERM → graceful-stop window before Agent 13 escalates to ``force_kill``.
    force_kill:
        Callable ``force_kill(reason: str)``. Agent 40 wires it to SIGKILL every running
        worker process (C1 immediate path). Default is a no-op until wired.
    """

    config: Any
    drivers: dict[str, Any]
    health: Any
    shutdown: threading.Event
    grace_seconds: float = 10.0
    force_kill: Callable[[str], None] = None  # type: ignore[assignment]
    log: Callable[[str], None] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.force_kill is None:
            self.force_kill = lambda reason: None
        if self.log is None:
            self.log = print
