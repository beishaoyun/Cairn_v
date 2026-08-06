"""`cairn dispatch` CLI assembly tests (owned by Agent 13).

Exercises `main_dispatch`: --help, config/driver error paths, SIGTERM graceful stop via
a fake loop runner, and the SIGTERM → grace → SIGKILL escalation (C1) when the loop
ignores the shutdown signal.
"""
from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from cairn.dispatcher.cli import build_drivers, build_health, main_dispatch
from cairn.dispatcher.config import load_dict
from cairn.dispatcher.workers.base import MissingEnvError


def _local_config(worker_type="claudecode", *, worker_env=None, healthcheck="disabled"):
    return load_dict(
        {
            "server": {"url": "http://127.0.0.1:8000", "api_token": "${CAIRN_API_TOKEN}"},
            "runtime": {"execution": "local", "worker_healthcheck": healthcheck},
            "workers": [
                {
                    "name": "w1",
                    "type": worker_type,
                    "task_types": ["explore"],
                    "env": worker_env or {},
                }
            ],
        },
        env={"CAIRN_API_TOKEN": "test-token"},
    )


@pytest.fixture()
def restore_signals():
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGTERM, old_term)
    signal.signal(signal.SIGINT, old_int)


def test_help_exits_zero():
    with pytest.raises(SystemExit) as exc_info:
        main_dispatch(["--help"])
    assert exc_info.value.code == 0


def test_config_load_error_returns_1():
    def loader(path):
        raise ValueError("boom")

    rc = main_dispatch(["--config", "nope.yaml"], config_loader=loader, loop_runner=lambda ctx: 0)
    assert rc == 1


def test_container_missing_env_key_aborts_startup(restore_signals):
    cfg = _local_config(worker_type="claudecode", healthcheck="disabled")
    cfg.runtime.execution = "container"  # now the claudecode worker is missing LLM keys
    rc = main_dispatch(
        ["--config", "x"],
        config_loader=lambda p: cfg,
        loop_runner=lambda ctx: 0,
    )
    assert rc == 1


def test_loop_runner_wired_by_default(restore_signals):
    """Agent 40 delivered the scheduler loop: ``main_dispatch`` without an explicit
    ``loop_runner`` lazily imports ``run_dispatch_loop`` (previously the import failed
    and it returned 2 as 'not wired'). Now the loop is wired; asserting the import
    resolves is the observable contract."""
    from cairn.dispatcher.cli import _load_loop_runner

    fn = _load_loop_runner()
    assert callable(fn)
    assert fn.__module__ == "cairn.dispatcher.scheduler.loop"


def test_build_drivers_and_health_ok():
    cfg = _local_config()
    drivers = build_drivers(cfg)
    assert list(drivers) == ["w1"]
    health = build_health(cfg)
    assert health.mode == "disabled"


def test_build_drivers_container_missing_key_raises():
    cfg = _local_config(worker_type="codex", healthcheck="disabled")
    cfg.runtime.execution = "container"
    with pytest.raises(MissingEnvError):
        build_drivers(cfg)


def test_sigterm_graceful_stop(restore_signals):
    """SIGTERM → graceful stop: the loop observes ctx.shutdown and returns cleanly."""
    cfg = _local_config(healthcheck="disabled")
    seen = {}

    def fake_loop(ctx):
        while not ctx.shutdown.is_set():
            time.sleep(0.02)
        seen["shutdown_seen"] = True
        return 0

    def fire():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=fire, daemon=True)
    t.start()
    rc = main_dispatch(["--config", "x"], config_loader=lambda p: cfg, loop_runner=fake_loop)
    assert rc == 0
    assert seen["shutdown_seen"]


def test_sigterm_escalates_to_force_kill_after_grace(restore_signals, monkeypatch):
    """If the loop ignores shutdown beyond the grace window, force_kill (SIGKILL) fires."""
    monkeypatch.setenv("CAIRN_DISPATCH_GRACE", "0.2")
    cfg = _local_config(healthcheck="disabled")
    calls = []
    release = threading.Event()

    def fake_loop(ctx):
        ctx.force_kill = lambda reason: (calls.append(reason), release.set())
        release.wait(timeout=5)  # ignores ctx.shutdown
        return 0

    def fire():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    t = threading.Thread(target=fire, daemon=True)
    t.start()

    rc = main_dispatch(
        ["--config", "x"],
        config_loader=lambda p: cfg,
        loop_runner=fake_loop,
    )
    assert rc == 0
    assert calls == ["grace_exceeded"]
