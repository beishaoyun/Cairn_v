"""Thread-safe task cancellation (owned by Agent 13).

`TaskCancellation` semantics (v1 §8.6, v2 §8.6, graph §4-11):
* first ``cancel()`` records the reason; later cancels are idempotent no-ops;
* attaching a process while already cancelled kills it immediately;
* cancelling while processes are attached kills every attached process;
* the C1 kill switch uses an **immediate SIGKILL** (no SIGTERM→grace path).

Kill-switch ordering: whichever of ``cancel``/``kill_switch`` fires first wins; the
other becomes a no-op (the reason is preserved).
"""
from __future__ import annotations

import signal
import threading
from typing import Iterable

from .backend import ExecProcess


def _safe_kill(proc: ExecProcess, sig: int | None = None) -> None:
    try:
        proc.kill(sig)
    except Exception:  # process may have already exited
        pass


class TaskCancellation:
    """Cancellation flag + attached-process kill switch for a single task."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._processes: list[ExecProcess] = []

    # ---- cancellation control ----

    def cancel(self, reason: str) -> None:
        """Record the reason (first call wins) and kill all attached processes."""
        with self._lock:
            if self._reason is not None:
                return  # idempotent
            self._reason = reason
            procs = list(self._processes)
        for proc in procs:
            _safe_kill(proc)

    def kill_switch(self, reason: str) -> None:
        """C1 immediate cancel — SIGKILL attached processes (no grace)."""
        with self._lock:
            if self._reason is not None:
                return  # idempotent
            self._reason = reason
            procs = list(self._processes)
        for proc in procs:
            _safe_kill(proc, signal.SIGKILL)

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._reason is not None

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    # ---- process attachment ----

    def attach_process(self, proc: ExecProcess) -> None:
        """Register a process; if already cancelled, kill it immediately."""
        with self._lock:
            if self._reason is not None:
                already_cancelled = True
            else:
                already_cancelled = False
                self._processes.append(proc)
        if already_cancelled:
            _safe_kill(proc)

    def detach_process(self, proc: ExecProcess) -> None:
        """Stop tracking a finished process (safe to call multiple times)."""
        with self._lock:
            try:
                self._processes.remove(proc)
            except ValueError:
                pass

    def clear(self) -> None:
        """Drop all attached-process references without killing them."""
        with self._lock:
            self._processes = []

    def attached(self) -> list[ExecProcess]:
        with self._lock:
            return list(self._processes)
