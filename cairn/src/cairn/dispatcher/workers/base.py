"""WorkerDriver abstraction (owned by Agent 13).

v1 §8.4 method surface, v2 §8.4 semantics. A `WorkerDriver` knows how to *drive one LLM
CLI* (claude / codex / pi) for a single worker:

* build the command lines (`build_execute` / `build_conclude`),
* manage / parse the session id (`prepare_session` / `extract_session`),
* extract the assistant text (`extract_response_text`),
* health-check the underlying LLM endpoint (`check_health`).

Hard constraints:
* drivers never inject the Cairn API token (C5) and never touch the Cairn server;
* drivers carry no task logic (Agent 30 orchestrates);
* provider injection is **container-mode only** — local variants call the host CLI as-is
  (v1 §8.4 "local 变体"), and container mode must supply the full LLM env key set
  (graph §4-23: claudecode 3 / codex 3 / pi 4), otherwise construction raises
  `MissingEnvError`.
"""
from __future__ import annotations

import abc
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import ClassVar

import httpx


class WorkerDriverError(Exception):
    """Base error for driver construction/operation."""


class MissingEnvError(WorkerDriverError):
    """A required LLM env key is absent in container mode (graph §4-23)."""


class UnknownDriverError(WorkerDriverError):
    """Registry lookup by name failed."""


@dataclass(frozen=True)
class WorkerCommand:
    """A fully-specified command for the execution backend (Agent 11)."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)


class WorkerDriver(abc.ABC):
    """Base class shared by all worker drivers."""

    #: registry key (dispatch.yaml ``workers[].type``)
    driver_type: ClassVar[str] = ""
    #: LLM env keys that MUST be present in container mode (graph §4-23)
    required_env_keys: ClassVar[tuple[str, ...]] = ()
    #: CLI name used for the local-mode health probe (``<binary> --help``, graph §4-26)
    local_binary: ClassVar[str | None] = None
    #: env key holding the LLM gateway base URL for the HTTP health probe
    base_url_env: ClassVar[str] = ""
    #: HTTP path probed in container mode (2xx only, graph §4-26)
    health_path: ClassVar[str] = ""

    def __init__(
        self,
        *,
        execution: str = "container",
        common_env: dict[str, str] | None = None,
        worker_env: dict[str, str] | None = None,
        binary_path: str | None = None,
    ) -> None:
        if execution not in ("container", "local"):
            raise WorkerDriverError(f"invalid execution mode: {execution!r}")
        self.execution = execution
        # env merge (graph §4-22): container = {**common_env, **worker_env};
        # local = {**os.environ, **common_env, **worker_env}
        merged: dict[str, str] = dict(os.environ) if execution == "local" else {}
        if common_env:
            merged.update(common_env)
        if worker_env:
            merged.update(worker_env)
        self.env = merged
        self.binary_path = binary_path
        if execution == "container":
            missing = [
                k for k in self.required_env_keys if not (self.env.get(k) or "").strip()
            ]
            if missing:
                raise MissingEnvError(
                    f"{self.driver_type}: container mode requires LLM env keys "
                    f"{list(self.required_env_keys)}; missing: {missing}"
                )

    # ---- session lifecycle ----

    def prepare_session(self) -> str | None:
        """Return a pre-generated session id (seed drivers) or ``None`` (drivers whose
        CLI creates the session and emits it in output — codex/pi)."""
        return str(uuid.uuid4())

    def extract_session(self, stdout: str, stderr: str) -> str | None:
        """Extract the session id from CLI output, or ``None`` if the driver knows the
        session ahead of time (seed driver) or none is found."""
        return None

    # ---- command construction ----

    @abc.abstractmethod
    def build_execute(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        """Build the first-phase (execute) command for ``prompt``."""

    @abc.abstractmethod
    def build_conclude(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        """Build the second-phase (conclude/resume) command for ``prompt``."""

    def extract_response_text(self, stdout: str) -> str | None:
        """Extract the assistant response text from CLI output."""
        return stdout.strip() or None

    def supports_conclude(self) -> bool:
        """Whether the driver supports the two-phase conclude fallback (graph §4-12)."""
        return True

    # ---- health (v2 §11.1-10, graph §4-26) ----

    def check_health(self, *, timeout: float | None = None) -> bool:
        """Local: probe ``<local_binary> --help`` (rc==0 ⇒ runnable).
        Container: HTTP GET ``<base_url><health_path>`` accepting only 2xx (body not
        parsed)."""
        if self.execution == "local":
            return self._probe_local(timeout)
        return self._probe_http(timeout)

    def _probe_local(self, timeout: float | None) -> bool:
        binary = self.binary_path or self.local_binary
        if not binary:
            return False
        try:
            result = subprocess.run(
                [binary, "--help"],
                capture_output=True,
                timeout=timeout or 15.0,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    def _probe_http(self, timeout: float | None) -> bool:
        base = self.env.get(self.base_url_env)
        if not base:
            return False
        url = base.rstrip("/") + self.health_path
        try:
            resp = httpx.get(url, timeout=timeout or 10.0)
        except httpx.HTTPError:
            return False
        return 200 <= resp.status_code < 300

    # ---- helpers ----

    def _base(self, key: str) -> str:
        return (self.env.get(key) or "").strip()
