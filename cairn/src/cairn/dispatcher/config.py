"""dispatch.yaml 加载 / 校验 / ${ENV_VAR} 展开 / 默认值合并。

权威 schema：``docs/dispatch-config-spec.md`` §1-§9。

要点：
- 顶层段：server / common_env / runtime / tasks / security / scope / tuning /
  container / workers（+ 本地示例的 ``local`` 段）。必填：server / runtime / workers。
- 所有字符串值在加载时做 ``${ENV_VAR}`` 展开，未设置的变量直接报错并指明变量名。
- ``server.api_token`` 必须通过 ``${ENV_VAR}`` 引用（仓库禁明文 token），
  且展开后非空。
- ``common_env`` 与 per-worker ``env`` 合并，per-worker 优先（见
  ``WorkerConfig.effective_env``）。
- 校验失败抛 ``ConfigError``（明确信息，不静默 fallback）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Union

import yaml

from .contracts import (
    CONTAINER_COMPLETED_ACTIONS,
    EXECUTION_MODES,
    HEALTHCHECK_MODES,
    LOCAL_COMPLETED_ACTIONS,
    NETWORK_MODES,
    WORKER_TASK_TYPES,
    WORKER_TYPES,
)

#: ${VAR} 占位符（VAR 需以字母/下划线开头）
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: dispatch.yaml 允许的顶层段
KNOWN_TOP_LEVEL_SECTIONS = frozenset(
    {
        "server",
        "common_env",
        "runtime",
        "tasks",
        "security",
        "scope",
        "tuning",
        "container",
        "workers",
        "local",
    }
)


class ConfigError(Exception):
    """dispatch.yaml 加载 / 校验失败。"""


# ---------------------------------------------------------------------------
# 各段 dataclass（默认值对齐 dispatch-config-spec.md §1-§9）
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    """§1 ``server``：Cairn Server 地址 + 唯一 Bearer Token。"""

    url: str
    api_token: str


@dataclass
class RuntimeConfig:
    """§3 ``runtime``：调度节拍 / 并发 / 健康检查 / prompt_group。"""

    execution: str = "container"  # container | local
    interval: int = 3
    max_workers: int = 8
    max_running_projects: int = 3
    max_project_workers: int = 4
    healthcheck_timeout: int = 20
    worker_healthcheck: str = "startup_only"  # startup_and_task | startup_only | disabled
    prompt_group: str = "default"  # default | mock


@dataclass
class TaskConfig:
    """单个任务类型的超时配置（spec §4）。"""

    timeout: int
    conclude_timeout: Optional[int] = None
    max_intents: Optional[int] = None


@dataclass
class TasksConfig:
    """§4 ``tasks``：各任务类型超时。"""

    bootstrap: TaskConfig = field(default_factory=lambda: TaskConfig(timeout=300, conclude_timeout=90))
    reason: TaskConfig = field(default_factory=lambda: TaskConfig(timeout=300, max_intents=2))
    explore: TaskConfig = field(default_factory=lambda: TaskConfig(timeout=300, conclude_timeout=90))
    verify: TaskConfig = field(default_factory=lambda: TaskConfig(timeout=300))
    audit: TaskConfig = field(default_factory=lambda: TaskConfig(timeout=300))
    replay: TaskConfig = field(default_factory=lambda: TaskConfig(timeout=60))


@dataclass
class SecurityConfig:
    """§5 ``security``：Dispatcher 全局安全（token/CA/存储/加密/executor）。"""

    api_token_env: str = "CAIRN_API_TOKEN"
    capture_token_env: str = "CAIRN_CAPTURE_TOKEN"
    capture_ca_dir: str = "/var/cairn/capture-ca"
    evidence_root: str = "/var/cairn/evidence"
    traffic_root: str = "/var/cairn/traffic"
    archive_root: str = "/var/cairn/archive"
    static_encryption: bool = True
    archive_encryption: bool = True
    executor_url: str = ""


@dataclass
class ScopeConfig:
    """§6 ``scope``：运行时守卫开关。"""

    enforce_scope_guard: bool = True
    enforce_auth_window: bool = True
    enforce_kill_switch: bool = True
    default_scope_policy: str = "{}"


@dataclass
class TuningConfig:
    """§7 ``tuning``：原硬编码魔数收敛。"""

    writeback_retries: int = 1
    reconcile_intent_timeout_multiplier: int = 2
    min_capture_ratio: float = 2.0
    min_capture_abs_diff: int = 3
    event_summary_max_bytes: int = 512
    command_evidence_max_bytes: int = 1048576
    event_raw_retain_days: int = 7
    sse_heartbeat_seconds: int = 15
    longpoll_hold_seconds: int = 20
    worker_rejected_cooldown_seconds: int = 5
    worker_unhealthy_cooldown_seconds: int = 5


@dataclass
class ContainerConfig:
    """§8 ``container``：worker 容器运行参数（capture 模式必须 bridge）。"""

    image: str = "ghcr.io/oritera/cairn-worker-container:latest"
    network_mode: str = "bridge"  # bridge | host
    completed_action: str = "stop"  # stop | remove
    cap_add: list[str] = field(default_factory=list)


@dataclass
class WorkerConfig:
    """§9 ``workers``：驱动列表。"""

    name: str
    type: str
    task_types: list[str]
    max_running: int = 1
    priority: int = 0
    verify_eligible: bool = True
    env: dict[str, str] = field(default_factory=dict)

    def effective_env(self, common_env: Mapping[str, str]) -> dict[str, str]:
        """common_env 与 per-worker env 合并，per-worker 优先。"""
        merged = dict(common_env)
        merged.update(self.env)
        return merged


@dataclass
class LocalConfig:
    """本地执行扩展（dispatch.local.example.yaml）。不在 spec §0 顶层列表内，按示例支持。"""

    workspace_root: Optional[str] = None
    completed_action: str = "keep"  # keep | remove


@dataclass
class DispatcherConfig:
    """dispatch.yaml 完整解析结果。"""

    server: ServerConfig
    runtime: RuntimeConfig
    workers: list[WorkerConfig]
    common_env: dict[str, str] = field(default_factory=dict)
    tasks: TasksConfig = field(default_factory=TasksConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    tuning: TuningConfig = field(default_factory=TuningConfig)
    container: ContainerConfig = field(default_factory=ContainerConfig)
    local: LocalConfig = field(default_factory=LocalConfig)
    source: str = "<config>"


# ---------------------------------------------------------------------------
# ${ENV_VAR} 展开
# ---------------------------------------------------------------------------


def _expand_str(value: str, env: Mapping[str, str], *, where: str) -> str:
    def _repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in env:
            raise ConfigError(
                f"环境变量未设置: {name}（位于 {where}；配置内 ${{{name}}} 必须由环境提供）"
            )
        return env[name]

    return _ENV_VAR_RE.sub(_repl, value)


def expand_env(raw: Any, env: Mapping[str, str], *, where: str = "dispatch.yaml") -> Any:
    """递归展开所有字符串值中的 ${VAR}（key 不做展开）。"""
    if isinstance(raw, str):
        return _expand_str(raw, env, where=where)
    if isinstance(raw, dict):
        return {k: expand_env(v, env, where=where) for k, v in raw.items()}
    if isinstance(raw, list):
        return [expand_env(v, env, where=where) for v in raw]
    return raw


def _check_api_token_is_env_ref(raw: Mapping[str, Any]) -> None:
    """api_token 必须通过 ${ENV_VAR} 引用 —— 在展开前检查原始值。"""
    server = raw.get("server")
    if not isinstance(server, Mapping):
        raise ConfigError("缺少必填段: server（或类型错误）")
    token = server.get("api_token")
    if not isinstance(token, str) or not _ENV_VAR_RE.search(token):
        raise ConfigError("server.api_token 必须通过 ${ENV_VAR} 引用（仓库禁明文 token）")


# ---------------------------------------------------------------------------
# 各段构建（严格校验，不静默 fallback）
# ---------------------------------------------------------------------------


def _require_section(raw: Mapping[str, Any], key: str) -> dict[str, Any]:
    val = raw.get(key)
    if not isinstance(val, Mapping):
        raise ConfigError(f"缺少必填段或类型错误: {key}")
    return dict(val)


def _build_server(raw: Mapping[str, Any]) -> ServerConfig:
    section = _require_section(raw, "server")
    url = section.get("url")
    if not isinstance(url, str) or not url:
        raise ConfigError("server.url 必填（如 http://cairn-server:8000）")
    api_token = section.get("api_token")
    if not isinstance(api_token, str) or not api_token:
        raise ConfigError("server.api_token 展开后为空（请检查对应环境变量）")
    return ServerConfig(url=url, api_token=api_token)


def _build_common_env(raw: Mapping[str, Any]) -> dict[str, str]:
    section = raw.get("common_env") or {}
    if not isinstance(section, Mapping):
        raise ConfigError("common_env 必须是映射")
    for k, v in section.items():
        if not isinstance(v, str):
            raise ConfigError(f"common_env[{k}] 必须为字符串")
    return dict(section)


def _build_runtime(raw: Mapping[str, Any]) -> RuntimeConfig:
    section = _require_section(raw, "runtime")
    execution = section.get("execution", "container")
    if execution not in EXECUTION_MODES:
        raise ConfigError(f"runtime.execution 非法: {execution!r}（可选: container|local）")

    def _pos_int(key: str, default: int) -> int:
        val = section.get(key, default)
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            raise ConfigError(f"runtime.{key} 必须为正整数")
        return val

    interval = _pos_int("interval", 3)
    max_workers = _pos_int("max_workers", 8)
    max_running_projects = _pos_int("max_running_projects", 3)
    max_project_workers = _pos_int("max_project_workers", 4)

    hc = section.get("healthcheck_timeout", 20)
    if not isinstance(hc, int) or isinstance(hc, bool) or hc < 0:
        raise ConfigError("runtime.healthcheck_timeout 必须为非负整数")

    worker_healthcheck = section.get("worker_healthcheck", "startup_only")
    if worker_healthcheck not in HEALTHCHECK_MODES:
        raise ConfigError(f"runtime.worker_healthcheck 非法: {worker_healthcheck!r}")

    prompt_group = section.get("prompt_group", "default")
    if not isinstance(prompt_group, str) or not prompt_group:
        raise ConfigError("runtime.prompt_group 必填")

    return RuntimeConfig(
        execution=execution,
        interval=interval,
        max_workers=max_workers,
        max_running_projects=max_running_projects,
        max_project_workers=max_project_workers,
        healthcheck_timeout=hc,
        worker_healthcheck=worker_healthcheck,
        prompt_group=prompt_group,
    )


_TASK_FIELDS = ("timeout", "conclude_timeout", "max_intents")


def _build_tasks(raw: Mapping[str, Any]) -> TasksConfig:
    section = raw.get("tasks")
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ConfigError("tasks 必须是映射")
    section = dict(section)
    for key in section:
        if key not in WORKER_TASK_TYPES and key != "replay":
            raise ConfigError(f"tasks 含未知任务类型: {key!r}")

    defaults = {
        "bootstrap": {"timeout": 300, "conclude_timeout": 90},
        "reason": {"timeout": 300, "max_intents": 2},
        "explore": {"timeout": 300, "conclude_timeout": 90},
        "verify": {"timeout": 300},
        "audit": {"timeout": 300},
        "replay": {"timeout": 60},
    }
    out: dict[str, TaskConfig] = {}
    for name, dflt in defaults.items():
        sub = section.get(name)
        if sub is None:
            merged = dict(dflt)
        else:
            if not isinstance(sub, Mapping):
                raise ConfigError(f"tasks.{name} 必须是映射")
            merged = dict(dflt)
            merged.update(dict(sub))
        for f in merged:
            if f not in _TASK_FIELDS:
                raise ConfigError(f"tasks.{name} 含未知字段: {f!r}")
        timeout = merged["timeout"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise ConfigError(f"tasks.{name}.timeout 必须为正整数")
        conclude_timeout = merged.get("conclude_timeout")
        if conclude_timeout is not None and (
            not isinstance(conclude_timeout, int) or isinstance(conclude_timeout, bool) or conclude_timeout < 0
        ):
            raise ConfigError(f"tasks.{name}.conclude_timeout 必须为非负整数")
        max_intents = merged.get("max_intents")
        if max_intents is not None and (
            not isinstance(max_intents, int) or isinstance(max_intents, bool) or max_intents <= 0
        ):
            raise ConfigError(f"tasks.{name}.max_intents 必须为正整数")
        out[name] = TaskConfig(
            timeout=timeout,
            conclude_timeout=conclude_timeout,
            max_intents=max_intents,
        )
    return TasksConfig(
        bootstrap=out["bootstrap"],
        reason=out["reason"],
        explore=out["explore"],
        verify=out["verify"],
        audit=out["audit"],
        replay=out["replay"],
    )


def _build_security(raw: Mapping[str, Any]) -> SecurityConfig:
    section = raw.get("security") or {}
    if not isinstance(section, Mapping):
        raise ConfigError("security 必须是映射")
    section = dict(section)
    return SecurityConfig(
        api_token_env=section.get("api_token_env", "CAIRN_API_TOKEN"),
        capture_token_env=section.get("capture_token_env", "CAIRN_CAPTURE_TOKEN"),
        capture_ca_dir=section.get("capture_ca_dir", "/var/cairn/capture-ca"),
        evidence_root=section.get("evidence_root", "/var/cairn/evidence"),
        traffic_root=section.get("traffic_root", "/var/cairn/traffic"),
        archive_root=section.get("archive_root", "/var/cairn/archive"),
        static_encryption=bool(section.get("static_encryption", True)),
        archive_encryption=bool(section.get("archive_encryption", True)),
        executor_url=section.get("executor_url", ""),
    )


def _build_scope(raw: Mapping[str, Any]) -> ScopeConfig:
    section = raw.get("scope") or {}
    if not isinstance(section, Mapping):
        raise ConfigError("scope 必须是映射")
    section = dict(section)
    return ScopeConfig(
        enforce_scope_guard=bool(section.get("enforce_scope_guard", True)),
        enforce_auth_window=bool(section.get("enforce_auth_window", True)),
        enforce_kill_switch=bool(section.get("enforce_kill_switch", True)),
        default_scope_policy=section.get("default_scope_policy", "{}"),
    )


def _build_tuning(raw: Mapping[str, Any]) -> TuningConfig:
    section = raw.get("tuning") or {}
    if not isinstance(section, Mapping):
        raise ConfigError("tuning 必须是映射")
    section = dict(section)
    return TuningConfig(
        writeback_retries=section.get("writeback_retries", 1),
        reconcile_intent_timeout_multiplier=section.get("reconcile_intent_timeout_multiplier", 2),
        min_capture_ratio=float(section.get("min_capture_ratio", 2.0)),
        min_capture_abs_diff=section.get("min_capture_abs_diff", 3),
        event_summary_max_bytes=section.get("event_summary_max_bytes", 512),
        command_evidence_max_bytes=section.get("command_evidence_max_bytes", 1048576),
        event_raw_retain_days=section.get("event_raw_retain_days", 7),
        sse_heartbeat_seconds=section.get("sse_heartbeat_seconds", 15),
        longpoll_hold_seconds=section.get("longpoll_hold_seconds", 20),
        worker_rejected_cooldown_seconds=section.get("worker_rejected_cooldown_seconds", 5),
        worker_unhealthy_cooldown_seconds=section.get("worker_unhealthy_cooldown_seconds", 5),
    )


def _build_container(raw: Mapping[str, Any]) -> ContainerConfig:
    section = raw.get("container") or {}
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ConfigError("container 必须是映射")
    section = dict(section)
    image = section.get("image", "ghcr.io/oritera/cairn-worker-container:latest")
    if not isinstance(image, str) or not image:
        raise ConfigError("container.image 必填")
    network_mode = section.get("network_mode", "bridge")
    if network_mode not in NETWORK_MODES:
        raise ConfigError(
            f"container.network_mode 非法: {network_mode!r}（可选: bridge|host；capture 模式必须 bridge）"
        )
    completed_action = section.get("completed_action", "stop")
    if completed_action not in CONTAINER_COMPLETED_ACTIONS:
        raise ConfigError(f"container.completed_action 非法: {completed_action!r}（可选: stop|remove）")
    cap_add = section.get("cap_add") or []
    if not isinstance(cap_add, list) or not all(isinstance(c, str) for c in cap_add):
        raise ConfigError("container.cap_add 必须为字符串列表")
    return ContainerConfig(
        image=image,
        network_mode=network_mode,
        completed_action=completed_action,
        cap_add=list(cap_add),
    )


def _build_workers(raw: Mapping[str, Any]) -> list[WorkerConfig]:
    if "workers" not in raw:
        raise ConfigError("缺少必填段: workers")
    workers_raw = raw["workers"]
    if not isinstance(workers_raw, list) or not workers_raw:
        raise ConfigError("workers 必须为非空列表")
    seen: set[str] = set()
    out: list[WorkerConfig] = []
    for i, w in enumerate(workers_raw):
        if not isinstance(w, Mapping):
            raise ConfigError(f"workers[{i}] 必须是映射")
        w = dict(w)
        name = w.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"workers[{i}] 缺少 name")
        if name in seen:
            raise ConfigError(f"workers 重名: {name!r}")
        seen.add(name)

        wtype = w.get("type")
        if not isinstance(wtype, str) or not wtype:
            raise ConfigError(f"workers[{name}].type 必填")
        if wtype not in WORKER_TYPES and not wtype.endswith("_local") and not wtype.startswith("local_"):
            raise ConfigError(
                f"workers[{name}].type 非法: {wtype!r}（可选: claudecode|codex|pi|mock 及 local 变体）"
            )

        task_types = w.get("task_types")
        if not isinstance(task_types, list) or not task_types:
            raise ConfigError(f"workers[{name}].task_types 必须为非空列表")
        for tt in task_types:
            if tt not in WORKER_TASK_TYPES:
                if tt == "replay":
                    raise ConfigError(
                        f"workers[{name}].task_types 含 replay：replay 是确定性引擎任务"
                        "（worker='replay-engine'），不在 worker 声明范围（dispatch-config-spec §4/§9）"
                    )
                raise ConfigError(f"workers[{name}].task_types 非法: {tt!r}")

        max_running = w.get("max_running", 1)
        if not isinstance(max_running, int) or isinstance(max_running, bool) or max_running < 1:
            raise ConfigError(f"workers[{name}].max_running 必须为正整数")
        priority = w.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise ConfigError(f"workers[{name}].priority 必须为整数")
        verify_eligible = w.get("verify_eligible", True)
        if not isinstance(verify_eligible, bool):
            raise ConfigError(f"workers[{name}].verify_eligible 必须为布尔")

        env = w.get("env") or {}
        if not isinstance(env, Mapping):
            raise ConfigError(f"workers[{name}].env 必须是映射")
        for k, v in env.items():
            if not isinstance(v, str):
                raise ConfigError(f"workers[{name}].env[{k}] 必须为字符串")

        out.append(
            WorkerConfig(
                name=name,
                type=wtype,
                task_types=[str(t) for t in task_types],
                max_running=max_running,
                priority=priority,
                verify_eligible=verify_eligible,
                env=dict(env),
            )
        )
    return out


def _build_local(raw: Mapping[str, Any]) -> LocalConfig:
    section = raw.get("local") or {}
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise ConfigError("local 必须是映射")
    section = dict(section)
    workspace_root = section.get("workspace_root")
    if workspace_root is not None and (not isinstance(workspace_root, str) or not workspace_root):
        raise ConfigError("local.workspace_root 必须为非空字符串")
    completed_action = section.get("completed_action", "keep")
    if completed_action not in LOCAL_COMPLETED_ACTIONS:
        raise ConfigError(f"local.completed_action 非法: {completed_action!r}（可选: keep|remove）")
    return LocalConfig(workspace_root=workspace_root, completed_action=completed_action)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def load_dict(
    raw: Mapping[str, Any],
    *,
    env: Optional[Mapping[str, str]] = None,
    source: str = "<dict>",
) -> DispatcherConfig:
    """从已解析的 dict 构建 DispatcherConfig（做 ${ENV_VAR} 展开与严格校验）。"""
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{source}: dispatch.yaml 顶层必须是映射")
    raw = dict(raw)
    for key in raw:
        if key not in KNOWN_TOP_LEVEL_SECTIONS:
            raise ConfigError(f"{source}: 未知顶层段: {key!r}")

    if env is None:
        env = os.environ

    _check_api_token_is_env_ref(raw)  # api_token 必须 ${ENV_VAR}（展开前检查）
    expanded = expand_env(raw, env, where=source)

    return DispatcherConfig(
        server=_build_server(expanded),
        common_env=_build_common_env(expanded),
        runtime=_build_runtime(expanded),
        tasks=_build_tasks(expanded),
        security=_build_security(expanded),
        scope=_build_scope(expanded),
        tuning=_build_tuning(expanded),
        container=_build_container(expanded),
        workers=_build_workers(expanded),
        local=_build_local(expanded),
        source=source,
    )


def loads(
    text: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    source: str = "<string>",
) -> DispatcherConfig:
    """从 YAML 字符串加载 dispatch.yaml。"""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover
        raise ConfigError(f"{source}: YAML 解析失败: {exc}") from exc
    return load_dict(raw, env=env, source=source)


def load(
    path: Union[str, Path],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> DispatcherConfig:
    """从文件加载 dispatch.yaml（示例/生产配置均可）。"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件: {path}: {exc}") from exc
    return loads(text, env=env, source=str(path))
