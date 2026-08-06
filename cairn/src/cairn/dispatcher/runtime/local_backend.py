"""Local execution backend (Agent 11).

Host-process execution for **authorized environments only** — no sandbox declaration
(worker-sandbox-hardening §1 / v2 §2.5). Workers run as host subprocesses in their own
process group (``start_new_session=True``) so cancellation / kill-switch (C1) can
terminate the whole group, including spawned children.

Implements Agent 13's ``ExecutionBackend`` protocol. Unlike the container backend there
is no per-project container: ``ensure_running`` creates the project workspace directory,
``build_exec_process`` spawns a plain host process (inheriting the host environment),
and ``cleanup_managed_container`` is a no-op unless ``completed_action: remove``.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .backend import ExecutionBackend
from .process import LocalProcess, resolve_workspace_path


class LocalBackendError(Exception):
    """Local execution backend error."""


class LocalBackend(ExecutionBackend):
    """Host-process execution backend (no sandbox; authorized environments only)."""

    def __init__(
        self,
        config=None,
        *,
        workspace_root: str | os.PathLike | None = None,
        completed_action: str | None = None,
        log=None,
    ) -> None:
        self._config = config
        if config is not None:
            local = getattr(config, "local", None)
            if local is not None:
                workspace_root = workspace_root or getattr(local, "workspace_root", None)
                completed_action = completed_action or getattr(local, "completed_action", None)
        self._workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self._completed_action = completed_action or "keep"
        self._log = log or (lambda msg: None)

    # ------------------------------------------------------------------ protocol

    def ensure_running(self, project_id: str) -> None:
        self._workspace_dir(project_id).mkdir(parents=True, exist_ok=True)

    def build_exec_process(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        workdir = cwd or str(self._workspace_root)
        return LocalProcess(command, env=full_env, cwd=workdir, timeout=timeout)

    def write_text_file(self, project_id: str, rel_path: str, content: str) -> None:
        target = resolve_workspace_path(self._workspace_root, project_id, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def cleanup_managed_container(self, project_id: str, reason: str = "completed") -> None:
        if self._completed_action == "remove":
            shutil.rmtree(self._workspace_dir(project_id), ignore_errors=True)

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------ internals

    def _workspace_dir(self, project_id: str) -> Path:
        return self._workspace_root / str(project_id)
