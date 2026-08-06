"""Worker driver tests — claude / codex / pi with fake CLI scripts (no real LLM).

Covers: session extraction, health checks (local `--help` probe + container HTTP 2xx),
execute/conclude command construction, and the local-variant "no provider injection"
rule. Env-key validation (graph §4-23) and registry lookups are covered too.
"""
from __future__ import annotations

import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cairn.dispatcher.workers import WorkerHealth
from cairn.dispatcher.workers.adapters.claude import ClaudeDriver
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.base import MissingEnvError
from cairn.dispatcher.workers.registry import (
    build_worker_driver,
    get_driver_class,
    normalize_type,
)
from cairn.dispatcher.workers.base import UnknownDriverError

SCRIPTS = os.path.join(os.path.dirname(__file__), "scripts")

CLAUDE_ENV = {
    "ANTHROPIC_MODEL": "deepseek-v4-pro",
    "ANTHROPIC_BASE_URL": "http://llm.test/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "sk-test",
}
CODEX_ENV = {
    "CODEX_MODEL": "qwen3.6-plus",
    "CODEX_BASE_URL": "http://llm.test/v1",
    "OPENAI_API_KEY": "sk-test",
}
PI_ENV = {
    "PI_MODEL": "qwen3.6-plus",
    "PI_BASE_URL": "http://llm.test/v1",
    "PI_API_KEY": "sk-test",
    "PI_PROVIDER_API": "openai-completions",
}


@pytest.fixture()
def fake_path(monkeypatch):
    """Prepend the fake CLI scripts dir to PATH so `claude`/`codex`/`pi` resolve here."""
    monkeypatch.setenv(
        "PATH", SCRIPTS + os.pathsep + os.environ.get("PATH", "")
    )


def _run(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd.argv, env=cmd.env, capture_output=True, text=True, timeout=15
    )


# ---------------------------------------------------------------------------
# claude (SeedSessionDriver)
# ---------------------------------------------------------------------------


def test_claude_execute_container_argv():
    d = ClaudeDriver(execution="container", worker_env=CLAUDE_ENV)
    cmd = d.build_execute("probe 10.0.0.1:80", session_id="s-abc123")
    assert cmd.argv == [
        "claude",
        "--session-id",
        "s-abc123",
        "--dangerously-skip-permissions",
        "-p",
        "--",
        "probe 10.0.0.1:80",
    ]
    assert cmd.env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"


def test_claude_conclude_resume():
    d = ClaudeDriver(execution="container", worker_env=CLAUDE_ENV)
    cmd = d.build_conclude("wrap up", session_id="s-abc123")
    assert cmd.argv[:3] == ["claude", "-r", "s-abc123"]
    assert cmd.argv[3] == "--dangerously-skip-permissions"
    assert cmd.argv[-1] == "wrap up"


def test_claude_prepare_session_uuid():
    d = ClaudeDriver(execution="container", worker_env=CLAUDE_ENV)
    sid = d.prepare_session()
    assert sid and len(sid) == 36  # uuid4


def test_claude_requires_session_for_execute():
    d = ClaudeDriver(execution="container", worker_env=CLAUDE_ENV)
    with pytest.raises(Exception):
        d.build_execute("hello", session_id=None)


def test_claude_local_roundtrip_fake_cli(fake_path):
    d = ClaudeDriver(execution="local", worker_env={})  # no env keys required locally
    assert d.check_health() is True  # `claude --help` → 0
    sid = d.prepare_session()
    r = _run(d.build_execute("hello", session_id=sid))
    assert r.returncode == 0
    assert d.extract_response_text(r.stdout) == "fake-claude: acknowledged prompt"
    r2 = _run(d.build_conclude("wrap up", session_id=sid))
    assert r2.returncode == 0


# ---------------------------------------------------------------------------
# codex (RegexSessionDriver)
# ---------------------------------------------------------------------------


def test_codex_execute_container_injects_provider():
    d = CodexDriver(execution="container", worker_env=CODEX_ENV)
    cmd = d.build_execute("do X", session_id=None)
    joined = " ".join(cmd.argv)
    assert cmd.argv[:2] == ["codex", "exec"]
    assert "--model" in cmd.argv
    assert 'model_provider="cairn"' in joined
    assert 'model_provider.cairn.base_url="http://llm.test/v1"' in joined
    assert 'model_provider.cairn.env_key="OPENAI_API_KEY"' in joined
    assert cmd.argv[-1] == "do X"


def test_codex_local_variant_no_provider(fake_path):
    d = CodexDriver(execution="local", worker_env={})
    cmd = d.build_execute("do X", session_id=None)
    joined = " ".join(cmd.argv)
    assert "model_provider" not in joined
    assert "--model" not in cmd.argv
    assert "-c" not in cmd.argv
    assert cmd.argv[-1] == "do X"
    assert d.check_health() is True  # `codex --help` → 0


def test_codex_extract_session_regex():
    d = CodexDriver(execution="container", worker_env=CODEX_ENV)
    stderr = "Setting up...\nsession id: 123e4567-e89b-12d3-a456-426614174000\n"
    assert d.extract_session("", stderr) == "123e4567-e89b-12d3-a456-426614174000"
    assert d.extract_session("no session here", "") is None


def test_codex_session_roundtrip_fake_cli(fake_path):
    d = CodexDriver(execution="local", worker_env={})
    r = _run(d.build_execute("do X", session_id=None))
    assert r.returncode == 0
    sid = d.extract_session(r.stdout, r.stderr)
    assert sid and len(sid) == 36
    r2 = _run(d.build_conclude("wrap up", session_id=sid))
    assert r2.returncode == 0
    assert d.extract_response_text(r2.stdout) == "fake-codex: resumed session"


def test_codex_conclude_requires_session():
    d = CodexDriver(execution="container", worker_env=CODEX_ENV)
    with pytest.raises(Exception):
        d.build_conclude("wrap up", session_id=None)


# ---------------------------------------------------------------------------
# pi (event-stream driver)
# ---------------------------------------------------------------------------


def test_pi_execute_container_injects_models_json():
    d = PiDriver(execution="container", worker_env=PI_ENV)
    cmd = d.build_execute("do Y", session_id=None)
    wrapper = cmd.argv[-1]
    assert cmd.argv[0] == "bash" and cmd.argv[1] == "-c"
    assert "PI_MODELS_FILE" in wrapper
    assert '"id": "cairn"' in wrapper
    assert "http://llm.test/v1" in wrapper
    assert "--session-dir" in wrapper
    assert cmd.env["PI_API_KEY"] == "sk-test"


def test_pi_local_variant_no_provider(fake_path):
    d = PiDriver(execution="local", worker_env={})
    cmd = d.build_execute("do Y", session_id=None)
    wrapper = cmd.argv[-1]
    assert "PI_MODELS_FILE" not in wrapper
    assert "models.json" not in wrapper
    assert "--session-dir" in wrapper
    assert d.check_health() is True  # `pi --help` → 0


def test_pi_extract_session_and_text_from_events():
    d = PiDriver(execution="local", worker_env={})
    stdout = (
        '{"type":"turn_start"}\n'
        '{"type":"session","session_id":"pi-1111-2222"}\n'
        '{"type":"turn_end","text":"first"}\n'
        '{"type":"agent_end","text":"final answer"}\n'
    )
    assert d.extract_session(stdout, "") == "pi-1111-2222"
    assert d.extract_response_text(stdout) == "final answer"


def test_pi_session_roundtrip_fake_cli(fake_path):
    d = PiDriver(execution="local", worker_env={})
    r = _run(d.build_execute("do Y", session_id=None))
    assert r.returncode == 0
    sid = d.extract_session(r.stdout, r.stderr)
    assert sid and len(sid) == 36
    text = d.extract_response_text(r.stdout)
    assert text == "fake-pi: acknowledged prompt"
    r2 = _run(d.build_conclude("wrap up", session_id=sid))
    assert r2.returncode == 0


def test_pi_conclude_requires_session():
    d = PiDriver(execution="container", worker_env=PI_ENV)
    with pytest.raises(Exception):
        d.build_conclude("wrap up", session_id=None)


def test_pi_context_window_optional():
    env = {**PI_ENV, "PI_MODEL_CONTEXT_WINDOW": "262144"}
    d = PiDriver(execution="container", worker_env=env)
    wrapper = d.build_execute("x", session_id=None).argv[-1]
    assert '"context_window": 262144' in wrapper


# ---------------------------------------------------------------------------
# env key validation (graph §4-23)
# ---------------------------------------------------------------------------


def test_container_missing_env_key_raises():
    with pytest.raises(MissingEnvError):
        ClaudeDriver(execution="container", worker_env={"ANTHROPIC_MODEL": "m"})
    with pytest.raises(MissingEnvError):
        CodexDriver(execution="container", worker_env={})
    with pytest.raises(MissingEnvError):
        PiDriver(execution="container", worker_env={})


def test_local_mode_requires_no_env_keys():
    ClaudeDriver(execution="local", worker_env={})
    CodexDriver(execution="local", worker_env={})
    PiDriver(execution="local", worker_env={})


# ---------------------------------------------------------------------------
# container-mode HTTP health (2xx only, graph §4-26)
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # silence
        pass


@pytest.fixture()
def http_stub():
    _StubHandler.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _stub_base(http_stub):
    return f"http://127.0.0.1:{http_stub.server_address[1]}"


def test_claude_health_container_http_2xx(http_stub):
    env = {**CLAUDE_ENV, "ANTHROPIC_BASE_URL": _stub_base(http_stub)}
    d = ClaudeDriver(execution="container", worker_env=env)
    assert d.check_health() is True


def test_codex_health_container_http_2xx(http_stub):
    env = {**CODEX_ENV, "CODEX_BASE_URL": _stub_base(http_stub)}
    d = CodexDriver(execution="container", worker_env=env)
    assert d.check_health() is True


def test_health_container_non_2xx_unhealthy(http_stub):
    _StubHandler.status = 500
    env = {**CLAUDE_ENV, "ANTHROPIC_BASE_URL": _stub_base(http_stub)}
    d = ClaudeDriver(execution="container", worker_env=env)
    assert d.check_health() is False


def test_health_container_unreachable_unhealthy():
    env = {**CLAUDE_ENV, "ANTHROPIC_BASE_URL": "http://127.0.0.1:1"}
    d = ClaudeDriver(execution="container", worker_env=env)
    assert d.check_health() is False


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_lookup_and_construction():
    assert get_driver_class("claudecode") is ClaudeDriver
    assert get_driver_class("codex") is CodexDriver
    assert get_driver_class("pi") is PiDriver
    with pytest.raises(UnknownDriverError):
        get_driver_class("nope")
    d = build_worker_driver("claudecode", execution="container", worker_env=CLAUDE_ENV)
    assert isinstance(d, ClaudeDriver)


def test_registry_local_variant_types():
    assert normalize_type("claudecode_local") == "claudecode"
    assert normalize_type("local_codex") == "codex"
    # a *_local / local_* type forces execution=local
    d = build_worker_driver("codex_local", execution="container", worker_env=CODEX_ENV)
    assert d.execution == "local"
    with pytest.raises(UnknownDriverError):
        get_driver_class("local_unknown")


# ---------------------------------------------------------------------------
# WorkerHealth cooldown
# ---------------------------------------------------------------------------


def test_worker_health_cooldown():
    h = WorkerHealth(mode="startup_and_task", cooldown_seconds=60.0, timeout=1.0)
    h.mark_unhealthy("w1", "boom")
    assert h.is_unhealthy("w1")
    assert h.unhealthy_until("w1") is not None
    assert h.unhealthy_reason("w1") == "boom"
    snap = h.snapshot()
    assert "w1" in snap
    h.mark_healthy("w1")
    assert not h.is_unhealthy("w1")
    assert h.snapshot() == {}
