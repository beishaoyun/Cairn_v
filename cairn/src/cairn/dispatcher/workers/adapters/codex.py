"""codex RegexSessionDriver (owned by Agent 13).

v1 §8.4: new session ``codex exec --dangerously-bypass-approvals-and-sandbox --model ... -c model_provider="cairn" ...``
(session id is extracted from stderr via ``session id:\\s*([0-9a-fA-F-]+)``); resume
``codex exec resume <s>``. Provider config is injected through CLI ``-c`` flags pointing
at a Cairn-managed OpenAI-compatible base URL (container mode only). The local variant
drops all provider injection and uses the host codex CLI as-is (v1 §8.4).

``--dangerously-bypass-approvals-and-sandbox`` is only safe inside an authorized,
sandboxed worker container (C5/C1); container hardening is Agent 11's responsibility.
"""
from __future__ import annotations

import re

from ..base import WorkerCommand, WorkerDriver, WorkerDriverError

__all__ = ["CodexDriver"]

_SESSION_RE = re.compile(r"session id:\s*([0-9a-fA-F-]+)")


class CodexDriver(WorkerDriver):
    driver_type = "codex"
    required_env_keys = ("CODEX_MODEL", "CODEX_BASE_URL", "OPENAI_API_KEY")
    local_binary = "codex"
    base_url_env = "CODEX_BASE_URL"
    health_path = "/v1/models"

    def prepare_session(self) -> str | None:
        return None  # codex creates the session; we extract it from stderr

    def build_execute(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        argv = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--full-auto",
        ]
        if self.execution == "container":
            argv += self._provider_flags()
        argv.append(prompt)
        return WorkerCommand(argv, dict(self.env))

    def build_conclude(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        if not session_id:
            raise WorkerDriverError("codex conclude requires an extracted session_id")
        argv = [
            "codex",
            "exec",
            "resume",
            session_id,
            "--dangerously-bypass-approvals-and-sandbox",
            "--full-auto",
        ]
        if self.execution == "container":
            argv += self._provider_flags()
        argv.append(prompt)
        return WorkerCommand(argv, dict(self.env))

    def extract_session(self, stdout: str, stderr: str) -> str | None:
        match = _SESSION_RE.search(stderr or "")
        if not match:
            match = _SESSION_RE.search(stdout or "")
        return match.group(1) if match else None

    def _provider_flags(self) -> list[str]:
        model = self._base("CODEX_MODEL")
        base_url = self._base("CODEX_BASE_URL")
        return [
            "--model",
            model,
            "-c",
            'model_provider="cairn"',
            "-c",
            f'model_provider.cairn.name="{model}"',
            "-c",
            f'model_provider.cairn.base_url="{base_url}"',
            "-c",
            'model_provider.cairn.env_key="OPENAI_API_KEY"',
        ]
