"""claude-code SeedSessionDriver (owned by Agent 13).

v1 §8.4: first phase ``claude --session-id <s> --dangerously-skip-permissions -p -- <p>``;
second phase ``claude -r <s> --dangerously-skip-permissions -p -- <p>``. Provider config
travels via env (``ANTHROPIC_*``), so the local variant is the same argv — the host CLI
supplies its own credentials. Health probe hits ``<base_url>/v1/messages`` (2xx only).

The ``--dangerously-skip-permissions`` flag is only safe inside an authorized, sandboxed
worker container (C5/C1); the container image/hardening is Agent 11's responsibility.
"""
from __future__ import annotations

from ..base import WorkerCommand, WorkerDriver, WorkerDriverError

__all__ = ["ClaudeDriver"]


class ClaudeDriver(WorkerDriver):
    driver_type = "claudecode"
    required_env_keys = ("ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
    local_binary = "claude"
    base_url_env = "ANTHROPIC_BASE_URL"
    health_path = "/v1/messages"

    # Seed driver: session is pre-generated, not extracted from output.
    prepare_session = WorkerDriver.prepare_session
    extract_session = WorkerDriver.extract_session

    def build_execute(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        if not session_id:
            raise WorkerDriverError(
                "claudecode execute requires a pre-generated session_id (prepare_session)"
            )
        argv = [
            "claude",
            "--session-id",
            session_id,
            "--dangerously-skip-permissions",
            "-p",
            "--",
            prompt,
        ]
        return WorkerCommand(argv, dict(self.env))

    def build_conclude(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        if not session_id:
            raise WorkerDriverError("claudecode conclude requires a session_id")
        argv = [
            "claude",
            "-r",
            session_id,
            "--dangerously-skip-permissions",
            "-p",
            "--",
            prompt,
        ]
        return WorkerCommand(argv, dict(self.env))
