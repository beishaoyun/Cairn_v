"""In-process worker health + cooldown (owned by Agent 13).

v2 §11.1-10 + graph §4-26 + dispatch-config-spec §7 (``tuning``).

Two check levels, controlled by ``runtime.worker_healthcheck``:
* ``startup`` — run once at dispatcher startup (mode != ``disabled``);
* ``task`` — run before dispatching a task (mode == ``startup_and_task``).

A failed check puts the worker on cooldown (``worker_unhealthy_until``, wall-clock so it
survives process restart via ``snapshot``/``load_snapshot``). Agent 40 consumes the
cooldown state and persists it to ``scheduler_state``.
"""
from __future__ import annotations

import threading
import time
from typing import Any

HEALTHCHECK_MODES = ("startup_and_task", "startup_only", "disabled")


class WorkerHealth:
    def __init__(
        self,
        *,
        mode: str = "startup_and_task",
        cooldown_seconds: float = 5.0,
        timeout: float = 15.0,
    ) -> None:
        if mode not in HEALTHCHECK_MODES:
            raise ValueError(f"invalid worker_healthcheck mode: {mode!r}")
        self.mode = mode
        self.cooldown_seconds = cooldown_seconds
        self.timeout = timeout
        self._lock = threading.Lock()
        self._unhealthy_until: dict[str, float] = {}
        self._reasons: dict[str, str] = {}

    # ---- check levels ----

    def startup_check(self, name: str, driver: Any) -> bool:
        """Startup-level check: probe once, mark unhealthy on failure (cooldown)."""
        ok = driver.check_health(timeout=self.timeout)
        if ok:
            self.mark_healthy(name)
        else:
            self.mark_unhealthy(name, "startup_healthcheck_failed")
        return ok

    def check(self, name: str, driver: Any) -> bool:
        """Task-level check (honours the cooldown window without re-probing)."""
        if self.is_unhealthy(name):
            return False
        ok = driver.check_health(timeout=self.timeout)
        if ok:
            self.mark_healthy(name)
        else:
            self.mark_unhealthy(name, "task_healthcheck_failed")
        return ok

    # ---- cooldown state ----

    def mark_unhealthy(self, name: str, reason: str = "healthcheck_failed") -> None:
        with self._lock:
            self._unhealthy_until[name] = time.time() + self.cooldown_seconds
            self._reasons[name] = reason

    def mark_healthy(self, name: str) -> None:
        with self._lock:
            self._unhealthy_until.pop(name, None)
            self._reasons.pop(name, None)

    def unhealthy_until(self, name: str) -> float | None:
        """Wall-clock deadline of the current cooldown window, or None if healthy."""
        with self._lock:
            return self._unhealthy_until.get(name)

    def unhealthy_reason(self, name: str) -> str | None:
        with self._lock:
            return self._reasons.get(name)

    def is_unhealthy(self, name: str) -> bool:
        with self._lock:
            until = self._unhealthy_until.get(name)
            return until is not None and until > time.time()

    # ---- persistence (Agent 40 → scheduler_state) ----

    def snapshot(self) -> dict[str, float]:
        """Wall-clock ``worker_unhealthy_until`` per worker, for scheduler_state."""
        with self._lock:
            return dict(self._unhealthy_until)

    def load_snapshot(self, data: dict[str, float]) -> None:
        """Reload cooldown deadlines from scheduler_state at dispatcher startup."""
        with self._lock:
            for name, deadline in data.items():
                if isinstance(deadline, (int, float)):
                    self._unhealthy_until[name] = float(deadline)
