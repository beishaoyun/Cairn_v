"""pi event-stream driver (owned by Agent 13).

v1 §8.4: a shell wrapper injects ``models.json`` (provider ``cairn``) + ``--session-dir``;
the session id and the last assistant text are parsed from stdout JSONL events
(``type:session`` / ``turn_end`` / ``agent_end``). Three wire APIs are supported via
``PI_PROVIDER_API`` (openai-completions | responses | anthropic-messages). Optional
``PI_MODEL_CONTEXT_WINDOW`` is folded into the models.json.

Local variant omits the models.json provider injection and calls the host ``pi`` CLI
as-is (v1 §8.4).
"""
from __future__ import annotations

import json
import shlex
import uuid

from ..base import WorkerCommand, WorkerDriver, WorkerDriverError

__all__ = ["PiDriver"]

_WIRE_API_HEALTH_PATH = {
    "openai-completions": "/v1/models",
    "responses": "/v1/models",
    "anthropic-messages": "/v1/messages",
}
_SUPPORTED_WIRE_APIS = frozenset(_WIRE_API_HEALTH_PATH)


class PiDriver(WorkerDriver):
    driver_type = "pi"
    required_env_keys = ("PI_MODEL", "PI_BASE_URL", "PI_API_KEY", "PI_PROVIDER_API")
    local_binary = "pi"
    base_url_env = "PI_BASE_URL"

    def __init__(
        self,
        *,
        execution: str = "container",
        common_env: dict[str, str] | None = None,
        worker_env: dict[str, str] | None = None,
        binary_path: str | None = None,
        wrapper_bin: str = "bash",
    ) -> None:
        super().__init__(
            execution=execution,
            common_env=common_env,
            worker_env=worker_env,
            binary_path=binary_path,
        )
        self.wrapper_bin = wrapper_bin

    @property
    def health_path(self) -> str:
        api = self._base("PI_PROVIDER_API") or "openai-completions"
        return _WIRE_API_HEALTH_PATH.get(api, "/v1/models")

    def prepare_session(self) -> str | None:
        return None  # pi creates the session; we extract it from stdout events

    def build_execute(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        return self._build(prompt, session_id=session_id)

    def build_conclude(
        self, prompt: str, *, session_id: str | None = None, **kw
    ) -> WorkerCommand:
        if not session_id:
            raise WorkerDriverError("pi conclude requires an extracted session_id")
        return self._build(prompt, session_id=session_id)

    # ---- command construction ----

    def _build(self, prompt: str, *, session_id: str | None) -> WorkerCommand:
        session_key = session_id or str(uuid.uuid4())
        wrapper = self._wrapper(session_key, session_id, prompt)
        return WorkerCommand([self.wrapper_bin, "-c", wrapper], dict(self.env))

    def _wrapper(self, session_key: str, session_id: str | None, prompt: str) -> str:
        lines = ["set -euo pipefail"]
        if self.execution == "container":
            models_path = f"/tmp/cairn-pi/models-{session_key}.json"
            lines += [
                "mkdir -p /tmp/cairn-pi",
                f"cat > {shlex.quote(models_path)} <<'CAIRN_EOF'",
                self._models_json(),
                "CAIRN_EOF",
                f"export PI_MODELS_FILE={shlex.quote(models_path)}",
            ]
        argv = ["pi", "--json"]
        if session_id:
            argv += ["--session-id", session_id]
        else:
            argv += ["--session-dir", shlex.quote(f"/tmp/cairn-pi/sessions/{session_key}")]
        argv.append(shlex.quote(prompt))
        lines.append("exec " + " ".join(argv))
        return "\n".join(lines)

    def _models_json(self) -> str:
        api = self._base("PI_PROVIDER_API") or "openai-completions"
        if api not in _SUPPORTED_WIRE_APIS:
            raise WorkerDriverError(
                f"pi: unsupported PI_PROVIDER_API {api!r} "
                f"(supported: {sorted(_SUPPORTED_WIRE_APIS)})"
            )
        provider: dict = {
            "id": "cairn",
            "api_type": api,
            "base_url": self._base("PI_BASE_URL"),
            "api_key_env_var": "PI_API_KEY",
            "model": self._base("PI_MODEL"),
        }
        context_window = self._base("PI_MODEL_CONTEXT_WINDOW")
        if context_window:
            try:
                provider["context_window"] = int(context_window)
            except ValueError:
                raise WorkerDriverError(
                    f"pi: PI_MODEL_CONTEXT_WINDOW must be an integer, got {context_window!r}"
                ) from None
        return json.dumps({"provider_configs": [provider]}, indent=2)

    # ---- output parsing (stdout JSONL events) ----

    def extract_session(self, stdout: str, stderr: str) -> str | None:
        for event in self._iter_events(stdout):
            if event.get("type") == "session":
                sid = event.get("session_id") or event.get("id")
                if sid:
                    return str(sid)
        return None

    def extract_response_text(self, stdout: str) -> str | None:
        text: str | None = None
        for event in self._iter_events(stdout):
            if event.get("type") in ("turn_end", "agent_end"):
                candidate = event.get("text") or event.get("response") or event.get("message")
                if candidate:
                    text = str(candidate)
        return text

    @staticmethod
    def _iter_events(stdout: str):
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event
