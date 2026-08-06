"""``cairn dispatch`` CLI assembly (owned by Agent 13).

Agent 10 owns ``src/cairn/cli.py`` and lazy-forwards the ``dispatch`` subcommand to
``main_dispatch`` here. This module:

1. parses ``cairn dispatch`` args (``--config``);
2. loads dispatch.yaml via Agent 12's ``dispatcher.config.load``;
3. builds the worker drivers through the registry — container-mode LLM env keys are
   validated up-front, a missing key aborts startup (graph §4-23);
4. builds ``WorkerHealth`` (startup/task two-level checks + cooldown);
5. installs SIGTERM/SIGINT handling: graceful stop (set ``ctx.shutdown``), escalating to
   ``ctx.force_kill`` (SIGKILL, C1) after ``grace`` seconds if the loop hasn't finished;
6. runs Agent 40's loop entry ``run_dispatch_loop(ctx) -> int``.

Loop entry contract (frozen, Agent 40 implements):
    from cairn.dispatcher.scheduler.loop import run_dispatch_loop
    rc = run_dispatch_loop(ctx: cairn.dispatcher.runtime.context.DispatcherContext) -> int
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from .runtime.context import DispatcherContext
from .workers.base import WorkerDriverError
from .workers.health import WorkerHealth
from .workers.registry import build_worker_driver

#: SIGTERM → SIGKILL escalation window (override with env CAIRN_DISPATCH_GRACE seconds).
DEFAULT_SHUTDOWN_GRACE_SECONDS = 10.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn dispatch",
        description="Run the Cairn dispatcher (scheduling executor).",
    )
    parser.add_argument(
        "--config",
        default="dispatch.yaml",
        help="path to dispatch.yaml (default: dispatch.yaml)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="cairn dispatch 0.2.0",
    )
    return parser


def main_dispatch(
    argv: list[str] | None = None,
    *,
    loop_runner=None,
    config_loader=None,
    driver_factory=None,
    health_factory=None,
) -> int:
    """CLI entry for ``cairn dispatch``.

    ``loop_runner`` — Agent 40's ``run_dispatch_loop(ctx)``; when None, imported lazily
    from ``cairn.dispatcher.scheduler.loop`` (returns 2 if not wired yet).
    ``config_loader`` / ``driver_factory`` / ``health_factory`` — test seams.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    loader = config_loader or _default_config_loader()
    try:
        config = loader(args.config)
    except Exception as exc:  # ConfigError / OSError from Agent 12 — surface as-is
        print(f"dispatch: failed to load config {args.config!r}: {exc}", file=sys.stderr)
        return 1

    try:
        drivers = driver_factory(config) if driver_factory else build_drivers(config)
    except WorkerDriverError as exc:
        print(f"dispatch: worker driver error: {exc}", file=sys.stderr)
        return 1

    health = health_factory(config) if health_factory else build_health(config)

    shutdown = threading.Event()
    grace = _grace_seconds()
    ctx = DispatcherContext(
        config=config,
        drivers=drivers,
        health=health,
        shutdown=shutdown,
        grace_seconds=grace,
    )

    runner = loop_runner or _load_loop_runner()
    if runner is None:
        print(
            "dispatch: scheduler loop not wired yet (Agent 40); nothing to run",
            file=sys.stderr,
        )
        return 2

    loop_done = threading.Event()
    _install_signal_handlers(shutdown, ctx, loop_done)

    _run_startup_healthchecks(health, drivers, config)

    print(
        f"dispatch: starting loop "
        f"(execution={config.runtime.execution}, workers={sorted(drivers)})",
        file=sys.stderr,
    )
    try:
        rc = runner(ctx)
    except KeyboardInterrupt:
        rc = 130
    except Exception as exc:  # loop crashed
        print(f"dispatch: loop failed: {exc!r}", file=sys.stderr)
        rc = 1
    finally:
        loop_done.set()
        print("dispatch: shutdown complete", file=sys.stderr)
    return rc


# ---------------------------------------------------------------------------
# Config / driver / health assembly
# ---------------------------------------------------------------------------


def _default_config_loader():
    """Agent 12's config loader: ``dispatcher.config.load(path, *, env=None)``."""
    from cairn.dispatcher.config import load as load_dispatch_config

    return load_dispatch_config


def build_drivers(config):
    """Build ``{worker_name: WorkerDriver}`` from a DispatcherConfig (Agent 12).

    Container mode raises `MissingEnvError` when a worker's required LLM env keys are
    absent (graph §4-23). Local mode never requires keys.
    """
    execution = config.runtime.execution
    common_env = dict(config.common_env or {})
    drivers: dict[str, object] = {}
    for worker in config.workers:
        if worker.name in drivers:
            raise WorkerDriverError(f"duplicate worker name: {worker.name!r}")
        drivers[worker.name] = build_worker_driver(
            worker.type,
            execution=execution,
            common_env=common_env,
            worker_env=dict(worker.env or {}),
        )
    if not drivers:
        raise WorkerDriverError("no workers configured")
    return drivers


def build_health(config) -> WorkerHealth:
    return WorkerHealth(
        mode=config.runtime.worker_healthcheck,
        cooldown_seconds=float(config.tuning.worker_unhealthy_cooldown_seconds),
        timeout=float(config.runtime.healthcheck_timeout),
    )


def _grace_seconds() -> float:
    raw = os.environ.get("CAIRN_DISPATCH_GRACE")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_SHUTDOWN_GRACE_SECONDS


# ---------------------------------------------------------------------------
# Signal handling: SIGTERM → grace → SIGKILL
# ---------------------------------------------------------------------------


def _install_signal_handlers(shutdown: threading.Event, ctx: DispatcherContext, loop_done: threading.Event) -> None:
    """Set ``shutdown`` on SIGTERM/SIGINT; after ``grace`` seconds escalate to
    ``ctx.force_kill`` (SIGKILL path) if the loop has not finished."""
    try:
        current_thread = threading.current_thread()
    except Exception:  # pragma: no cover
        current_thread = None
    if current_thread is None or current_thread is not threading.main_thread():
        return  # signal.signal is main-thread-only; loop handles shutdown via the event

    def _handler(signum: int, _frame) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:  # pragma: no cover
            name = f"signal {signum}"
        print(
            f"dispatch: received {name}; graceful shutdown (grace={ctx.grace_seconds}s)",
            file=sys.stderr,
        )
        shutdown.set()

        def _escalate() -> None:
            time.sleep(ctx.grace_seconds)
            if shutdown.is_set() and not loop_done.is_set():
                print("dispatch: grace exceeded; force-killing workers (SIGKILL)", file=sys.stderr)
                try:
                    ctx.force_kill("grace_exceeded")
                except Exception as exc:  # pragma: no cover
                    print(f"dispatch: force_kill failed: {exc!r}", file=sys.stderr)

        threading.Thread(target=_escalate, daemon=True).start()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _run_startup_healthchecks(health: WorkerHealth, drivers: dict[str, object], config) -> None:
    if health.mode == "disabled":
        print("dispatch: worker healthcheck disabled; skipping startup checks", file=sys.stderr)
        return
    for name, driver in drivers.items():
        ok = health.startup_check(name, driver)
        print(
            f"dispatch: startup healthcheck {name}: {'ok' if ok else 'UNHEALTHY'}",
            file=sys.stderr,
        )
        if not ok and health.mode == "startup_only":
            print(
                f"dispatch: worker {name} unhealthy at startup; "
                f"cooldown {health.cooldown_seconds}s (worker_unhealthy_until)",
                file=sys.stderr,
            )


def _load_loop_runner():
    """Lazily import Agent 40's loop entry. Returns None when not yet wired."""
    try:
        from cairn.dispatcher.scheduler.loop import run_dispatch_loop

        return run_dispatch_loop
    except Exception:
        return None
