"""Execution backend abstraction (owned by Agent 13).

Defines the `ExecProcess` / `ExecutionBackend` protocols that **Agent 11 implements**
(container backend ``runtime/containers.py``, local backend ``runtime/local_backend.py``,
process models ``runtime/process.py``). Agent 13 defines the protocol and assembles the
worker drivers against it; Agent 30 (tasks) drives a command through the backend.

Protocol freeze — these signatures are the contract for Agents 11/30/40. Any change must
be mirrored in `dev-agents/notes/13-dispatcher-runtime.md`.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ExecProcess(Protocol):
    """A running (or finished) worker command process."""

    @property
    def pid(self) -> int | None:
        """Host/container pid if known, else None."""
        ...

    @property
    def timed_out(self) -> bool:
        """True once the process was terminated because its timeout elapsed."""
        ...

    def poll(self) -> int | None:
        """Return the exit code if the process has finished, else None."""
        ...

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        """Write ``input`` to stdin (optional), drain stdout+stderr, return (stdout, stderr).

        Blocks until the process completes (or ``timeout`` elapses, in which case the
        implementation must terminate the process and set ``timed_out``).
        """
        ...

    def kill(self, sig: int | None = None) -> None:
        """Terminate the process.

        * ``sig is None`` — default graceful termination (local: SIGTERM → grace →
          SIGKILL; managed container exec: kill inside the container, with a fallback).
        * a concrete signal (e.g. ``signal.SIGKILL``) is sent immediately — this is the
          C1 kill-switch path (v2 §12 rule 2, worker-sandbox-hardening §4.3).
        """
        ...


@runtime_checkable
class ExecutionBackend(Protocol):
    """Container/local execution backend. One backend serves all projects/workers."""

    def ensure_running(self, project_id: str) -> None:
        """Ensure the execution context for ``project_id`` exists (start the container
        for container mode; create the workspace dir for local mode)."""
        ...

    def build_exec_process(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> ExecProcess:
        """Build an executable process for ``command``.

        * container mode — ``docker exec`` inside the project container (session-bound);
        * local mode — host process in an isolated process group
          (``start_new_session=True``), inheriting the host environment.
        """
        ...

    def write_text_file(self, project_id: str, rel_path: str, content: str) -> None:
        """Write ``content`` to ``rel_path`` inside the project workspace/container.

        Path must be absolute inside the container and must not contain ``..``/``.``
        segments (graph §4-15 path-traversal guard).
        """
        ...

    def cleanup_managed_container(
        self, project_id: str, reason: str = "completed"
    ) -> None:
        """Stop/remove the project container per ``container.completed_action``
        (graph §4-16). No-op in local mode."""
        ...

    def close(self) -> None:
        """Release backend resources (docker client, thread-local sessions)."""
        ...
