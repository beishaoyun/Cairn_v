"""Worker driver registry (owned by Agent 13).

Registry maps dispatch.yaml ``workers[].type`` → driver class. Container-mode workers
are validated at construction: a missing LLM env key aborts startup with
`MissingEnvError` (graph §4-23). Local mode never requires keys (graph §4-23).

Agent 31 extends the registry with the mock driver via `register_driver("mock", ...)`.
"""
from __future__ import annotations

from .adapters.claude import ClaudeDriver
from .adapters.codex import CodexDriver
from .adapters.pi import PiDriver
from .base import UnknownDriverError, WorkerDriver, WorkerDriverError

DRIVER_CLASSES: dict[str, type[WorkerDriver]] = {
    "claudecode": ClaudeDriver,
    "codex": CodexDriver,
    "pi": PiDriver,
}


_LOCAL_SUFFIX = "_local"
_LOCAL_PREFIX = "local_"


def normalize_type(name: str) -> str:
    """Map local-variant type strings (``claudecode_local`` / ``local_claudecode``) to
    the base driver type (``claudecode``). Agent 12's config validation accepts these."""
    if name.endswith(_LOCAL_SUFFIX):
        return name[: -len(_LOCAL_SUFFIX)]
    if name.startswith(_LOCAL_PREFIX):
        return name[len(_LOCAL_PREFIX):]
    return name


def register_driver(name: str, cls: type[WorkerDriver]) -> None:
    """Register (or override) a driver class by name. Used by Agent 31's mock adapter."""
    if not (isinstance(cls, type) and issubclass(cls, WorkerDriver)):
        raise WorkerDriverError(f"registered driver {cls!r} is not a WorkerDriver")
    DRIVER_CLASSES[name] = cls


def get_driver_class(name: str) -> type[WorkerDriver]:
    norm = normalize_type(name)
    if norm == "mock" and "mock" not in DRIVER_CLASSES:
        # Agent 31 registration point: the mock adapter cannot import this module
        # at load time (the adapters package is imported FROM here, which would be
        # circular), so the mock driver is registered lazily on first lookup.
        from .adapters.mock import MockDriver

        DRIVER_CLASSES["mock"] = MockDriver
    try:
        return DRIVER_CLASSES[norm]
    except KeyError:
        raise UnknownDriverError(
            f"unknown worker driver type: {name!r} (known: {sorted(DRIVER_CLASSES)})"
        ) from None


def is_local_variant(name: str) -> bool:
    """True when the type string itself is a local variant marker (``*_local``/``local_*``)."""
    return name.endswith(_LOCAL_SUFFIX) or name.startswith(_LOCAL_PREFIX)


def build_worker_driver(
    name: str,
    *,
    execution: str,
    common_env: dict[str, str] | None = None,
    worker_env: dict[str, str] | None = None,
    binary_path: str | None = None,
) -> WorkerDriver:
    """Construct a driver by type name. Container mode raises `MissingEnvError` if the
    required LLM env keys are absent. A local-variant type string forces ``execution``
    to ``local`` regardless of the runtime-level value."""
    cls = get_driver_class(name)
    if is_local_variant(name):
        execution = "local"
    return cls(
        execution=execution,
        common_env=common_env,
        worker_env=worker_env,
        binary_path=binary_path,
    )
