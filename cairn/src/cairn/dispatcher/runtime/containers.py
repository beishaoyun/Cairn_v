"""Container execution backend (Agent 11).

Runtime hardening per ``docs/worker-sandbox-hardening.md`` §4:

* non-root ``worker:worker`` user, ``cap_drop=["ALL"]`` + ``NET_RAW``/``NET_ADMIN``
  only when ``scope_policy.network_cap`` authorises them;
* read-only root, tmpfs ``/tmp`` (512m), ``security_opt=no-new-privileges``,
  mem/cpu/pids limits, nofile ulimits, labels ``cairn.project``/``cairn.engagement``;
* volumes: workspace ``{workspace_root}/{project_id}`` ↔ ``/home/worker/workspace``
  (rw, B6), evidence ``{evidence_root}/{engagement_id}`` ↔ ``/home/worker/evidence``
  (rw, B7 — engagement-scoped, not project-scoped), optional ``{tools_root}`` ↔
  ``/opt/tools`` (ro, per-engagement authorisation);
* capture-proxy env injection + CA trust chain (§4.1): HTTPS_PROXY/HTTP_PROXY,
  SSL_CERT_FILE, REQUESTS_CA_BUNDLE, NODE_EXTRA_CA_CERTS, NO_PROXY; the per-engagement
  CA is bind-mounted at ``/etc/cairn-capture/ca.pem`` (ro). Capture mode MUST use
  bridge networking (C12) — host is only for local/drill and is documented as having
  no network fallback (v2 §2.5);
* C1 kill switch: ``kill(sig=SIGKILL)`` signals the host ``docker exec`` client
  immediately and also attempts an in-container ``kill`` fallback.

Implements Agent 13's ``ExecutionBackend`` protocol. One backend serves all projects.
``ensure_running(project_id)`` records the *current* project on the calling thread
(thread-local); ``build_exec_process`` uses it to route ``docker exec`` to the right
per-project container (the protocol's ``build_exec_process`` has no ``project_id``).

The ``docker`` Python SDK is imported lazily — the module remains importable for tests
that inject a fake ``docker_client``. The SDK is used for container lifecycle only;
running commands uses the ``docker`` CLI so each command maps to a real host subprocess
(``ExecProcess``) that can be signalled and reaped.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .backend import ExecutionBackend
from .process import (
    CONTAINER_PID_MARKER,
    ContainerProcess,
    LocalProcess,
    resolve_workspace_path,
)


class ContainerBackendError(Exception):
    """Container execution backend error."""


@dataclass
class ContainerScope:
    """Resolved per-project runtime scope (subset of ``scope_policy``)."""

    engagement_id: str | None = None
    network_cap: list[str] = field(default_factory=list)
    mem_limit: str | None = None
    cpu_quota: int | None = None
    pids_limit: int | None = None
    tools: list[str] | None = None
    capture_proxy: dict[str, Any] | None = None


def _coerce_scope(value: Any) -> ContainerScope:
    """Normalise a scope_resolver result (None / dict / ContainerScope / object)."""
    if value is None:
        return ContainerScope()
    if isinstance(value, ContainerScope):
        return value
    if isinstance(value, Mapping):
        cap = value.get("capture_proxy")
        tools = value.get("tools")
        return ContainerScope(
            engagement_id=value.get("engagement_id"),
            network_cap=list(value.get("network_cap") or []),
            mem_limit=value.get("mem_limit"),
            cpu_quota=value.get("cpu_quota"),
            pids_limit=value.get("pids_limit"),
            tools=list(tools) if tools else None,
            capture_proxy=dict(cap) if cap else None,
        )
    tools = getattr(value, "tools", None)
    cap = getattr(value, "capture_proxy", None)
    return ContainerScope(
        engagement_id=getattr(value, "engagement_id", None),
        network_cap=list(getattr(value, "network_cap", None) or []),
        mem_limit=getattr(value, "mem_limit", None),
        cpu_quota=getattr(value, "cpu_quota", None),
        pids_limit=getattr(value, "pids_limit", None),
        tools=list(tools) if tools else None,
        capture_proxy=dict(cap) if cap else None,
    )


def _make_worker_writable(path: Path, *, private: bool) -> None:
    """Best-effort: make a host bind-source dir writable by container uid 1000."""
    try:
        os.chown(path, 1000, 1000)
    except (PermissionError, OSError):
        pass
    try:
        os.chmod(path, 0o700 if private else 0o770)
    except OSError:
        pass


class ContainerBackend(ExecutionBackend):
    """One backend for all projects; routes ``docker exec`` to per-project containers."""

    def __init__(
        self,
        config,
        *,
        scope_resolver: Callable[[str], Any] | None = None,
        docker_client: Any | None = None,
        docker_cli: str | Sequence[str] = "docker",
        workspace_root: str | os.PathLike | None = None,
        tools_root: str | os.PathLike | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._scope_resolver = scope_resolver
        self._log = log or (lambda msg: None)
        self._threadlocal = threading.local()

        security = getattr(config, "security", None) if config is not None else None
        container_cfg = getattr(config, "container", None) if config is not None else None

        self._workspace_root = Path(workspace_root or "/var/cairn/workspace").resolve()
        evidence_root = getattr(security, "evidence_root", None) if security is not None else None
        self._evidence_root = Path(evidence_root or "/var/cairn/evidence").resolve()
        ca_dir = getattr(security, "capture_ca_dir", None) if security is not None else None
        self._capture_ca_dir = Path(ca_dir or "/var/cairn/capture-ca").resolve()
        self._tools_root = Path(tools_root).resolve() if tools_root else None

        self._image = (
            getattr(container_cfg, "image", None) if container_cfg is not None else None
        ) or "ghcr.io/oritera/cairn-worker-container:latest"
        self._network_mode = (
            getattr(container_cfg, "network_mode", None) if container_cfg is not None else None
        ) or "bridge"
        self._completed_action = (
            getattr(container_cfg, "completed_action", None) if container_cfg is not None else None
        ) or "stop"
        self._base_cap_add = list(
            getattr(container_cfg, "cap_add", None) or [] if container_cfg is not None else []
        )

        self._docker_prefix = (
            list(docker_cli) if isinstance(docker_cli, (list, tuple)) else [str(docker_cli)]
        )
        self._owns_client = docker_client is None
        self._client = docker_client
        if self._client is None:
            try:
                import docker  # lazy: docker SDK is an optional runtime dependency
                self._client = docker.from_env()
            except Exception as exc:
                raise ContainerBackendError(
                    "无法连接 Docker：未安装 docker Python 包或 daemon 不可达。"
                    "container 模式需要 Docker（或注入 docker_client）。"
                ) from exc

    # ------------------------------------------------------------------ protocol

    def ensure_running(self, project_id: str) -> None:
        """Start (or reuse) the per-project worker container and record it thread-local."""
        name = self.container_name(project_id)
        scope = self._resolve_scope(project_id)
        self._ensure_host_dirs(project_id, scope)
        try:
            container = self._client.containers.get(name)
        except Exception as exc:
            if not self._is_notfound(exc):
                raise
            self._client.containers.run(**self._run_kwargs(project_id, scope))
        else:
            status = getattr(container, "status", None)
            if status != "running":
                container.start()
        self._threadlocal.current_project_id = project_id

    def build_exec_process(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        timeout: float | None = None,
    ):
        project_id = self._current_project_id()
        name = self.container_name(project_id)
        scope = self._resolve_scope(project_id)
        final_env = self._build_env(env or {}, scope)
        argv = self.build_docker_exec_command(
            self._docker_prefix, name, command, final_env, cwd=cwd
        )

        pid_holder: dict[str, int | None] = {"pid": None}

        def _capture_pid(raw: str) -> None:
            pid_holder["pid"] = int(raw)

        local = LocalProcess(
            argv,
            env=os.environ,  # docker CLI needs the dispatcher's env (DOCKER_HOST etc.)
            timeout=timeout,
            stderr_marker=re.compile(rf"^{re.escape(CONTAINER_PID_MARKER)}:(\d+)\s*$"),
            stderr_marker_cb=_capture_pid,
            on_line=None,  # progress package 24 may supply a task_events sink here
        )
        return ContainerProcess(
            local,
            container_name=name,
            container_pid_fn=lambda: pid_holder["pid"],
            docker_exec=self._docker_exec,
        )

    def write_text_file(self, project_id: str, rel_path: str, content: str) -> None:
        """Write ``content`` into the project workspace (host bind source == container)."""
        target = resolve_workspace_path(self._workspace_root, project_id, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def cleanup_managed_container(self, project_id: str, reason: str = "completed") -> None:
        """Stop/remove the project container per ``container.completed_action``."""
        name = self.container_name(project_id)
        try:
            container = self._client.containers.get(name)
        except Exception as exc:
            if self._is_notfound(exc):
                return
            raise
        if self._completed_action == "remove":
            try:
                container.remove(force=True)
            except Exception:
                pass
        else:
            try:
                container.stop()
            except Exception:
                pass

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    # ------------------------------------------------------------- hardening extras

    def managed_container_names(self) -> list[str]:
        """All containers labelled ``cairn.managed`` (v2 §8.2 orphan cleanup)."""
        names: list[str] = []
        try:
            containers = self._client.containers.list(
                all=True, filters={"label": "cairn.managed"}
            )
        except Exception:
            return names
        for c in containers:
            nm = getattr(c, "name", None)
            if nm:
                names.append(nm)
        return names

    def cleanup_orphan(self, known_project_ids: Sequence[str]) -> list[str]:
        """Remove managed containers whose project is no longer known; return removed."""
        known = {self.container_name(p) for p in known_project_ids}
        orphans: list[str] = []
        for name in self.managed_container_names():
            if name in known:
                continue
            try:
                c = self._client.containers.get(name)
                c.remove(force=True)
                orphans.append(name)
            except Exception:
                pass
        return orphans

    # ------------------------------------------------------------------ internals

    @staticmethod
    def container_name(project_id: str) -> str:
        return f"cairn-{project_id}"

    @staticmethod
    def build_docker_exec_command(
        docker_prefix: Sequence[str],
        container_name: str,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        *,
        cwd: str | None = None,
    ) -> list[str]:
        """Build the ``docker exec`` argv that runs ``command`` inside the container.

        Every command is wrapped in ``sh -c 'echo "__CAIRN_PID__:$$" >&2; exec "$@"'
        _ CMD...`` so the in-container PID is emitted on stderr as a marker line that
        ``LocalProcess`` strips (used for the in-container kill fallback).
        """
        argv: list[str] = [*docker_prefix, "exec", "-i"]
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={v}"]
        if cwd:
            argv += ["-w", cwd]
        argv += [
            container_name,
            "sh",
            "-c",
            f'echo "{CONTAINER_PID_MARKER}:$$" >&2; exec "$@"',
            "_",
            *command,
        ]
        return argv

    def _build_env(self, caller_env: Mapping[str, str], scope: ContainerScope) -> dict[str, str]:
        env = dict(caller_env)
        self._assert_no_cairn_secrets(env)
        cap = scope.capture_proxy or {}
        if cap.get("enabled"):
            host = cap.get("host")
            port = cap.get("port")
            if host and port:
                proxy = f"http://{host}:{port}"
                env["HTTPS_PROXY"] = proxy
                env["HTTP_PROXY"] = proxy
            env["SSL_CERT_FILE"] = "/etc/cairn-capture/ca.pem"
            env["REQUESTS_CA_BUNDLE"] = "/etc/cairn-capture/ca.pem"
            env["NODE_EXTRA_CA_CERTS"] = "/etc/cairn-capture/ca.pem"
            no_proxy = cap.get("no_capture_hosts")
            if no_proxy:
                env["NO_PROXY"] = ",".join(str(h) for h in no_proxy)
        return env

    def _assert_no_cairn_secrets(self, env: Mapping[str, str]) -> None:
        """C5 / worker-sandbox §4.2: the agent container must never hold Cairn tokens."""
        names = {
            "CAIRN_API_TOKEN",
            "CAIRN_CAPTURE_TOKEN",
            "CAIRN_SERVER_URL",
            "CAIRN_SERVER",
        }
        security = getattr(self._config, "security", None) if self._config is not None else None
        if security is not None:
            if getattr(security, "api_token_env", None):
                names.add(security.api_token_env)
            if getattr(security, "capture_token_env", None):
                names.add(security.capture_token_env)
        bad = sorted(n for n in env if n in names or n.startswith("CAIRN_SERVER"))
        if bad:
            raise ContainerBackendError(
                "拒绝向 Agent 容器注入 Cairn 控制面密钥（C5 / worker-sandbox §4.2）: "
                + ", ".join(bad)
            )

    def _run_kwargs(self, project_id: str, scope: ContainerScope) -> dict[str, Any]:
        caps = list(self._base_cap_add)
        caps += list(scope.network_cap or [])
        caps = list(dict.fromkeys(caps))  # dedupe, preserve order

        volumes = {
            f"{self._workspace_root}/{project_id}": {
                "bind": "/home/worker/workspace",
                "mode": "rw",
            },
            f"{self._evidence_root}/{scope.engagement_id or project_id}": {
                "bind": "/home/worker/evidence",
                "mode": "rw",
            },
        }
        if self._tools_root is not None and scope.tools:
            volumes[str(self._tools_root)] = {"bind": "/opt/tools", "mode": "ro"}

        cap = scope.capture_proxy or {}
        if cap.get("enabled"):
            ca_path = self._ca_path(scope.engagement_id)
            if ca_path.is_file():
                volumes[str(ca_path)] = {"bind": "/etc/cairn-capture/ca.pem", "mode": "ro"}

        return {
            "image": self._image,
            "command": ["sleep", "infinity"],
            "detach": True,
            "name": self.container_name(project_id),
            "network_mode": self._network_mode,
            "user": "worker:worker",
            "read_only": True,
            "tmpfs": {"/tmp": "size=512m"},
            "cap_drop": ["ALL"],
            "cap_add": caps or None,
            "security_opt": ["no-new-privileges"],
            "mem_limit": scope.mem_limit or "2g",
            "cpu_quota": scope.cpu_quota or 100_000,
            "pids_limit": scope.pids_limit or 512,
            "ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 2048}],
            "volumes": volumes,
            "labels": {
                "cairn.project": str(project_id),
                "cairn.engagement": str(scope.engagement_id or ""),
                "cairn.managed": "true",
            },
        }

    def _ca_path(self, engagement_id: str | None) -> Path:
        eid = str(engagement_id) if engagement_id else "_"
        return self._capture_ca_dir / eid / "ca.pem"

    def _ensure_host_dirs(self, project_id: str, scope: ContainerScope) -> None:
        ws = self._workspace_root / str(project_id)
        ws.mkdir(parents=True, exist_ok=True)
        _make_worker_writable(ws, private=False)
        eid = scope.engagement_id
        if eid:
            ev = self._evidence_root / eid
            ev.mkdir(parents=True, exist_ok=True)
            _make_worker_writable(ev, private=True)

    def _current_project_id(self) -> str:
        pid = getattr(self._threadlocal, "current_project_id", None)
        if pid is None:
            raise ContainerBackendError(
                "build_exec_process 前必须先调用 ensure_running(project_id)"
                "（后端按 project 路由容器）"
            )
        return pid

    def _resolve_scope(self, project_id: str) -> ContainerScope:
        if self._scope_resolver is not None:
            try:
                return _coerce_scope(self._scope_resolver(project_id))
            except Exception:
                pass
        return ContainerScope()

    def _docker_exec(
        self,
        container_name: str,
        argv: Sequence[str],
        timeout: float = 15.0,
    ) -> int:
        cmd = [*self._docker_prefix, "exec", container_name, *argv]
        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
            )
            return proc.returncode
        except Exception:
            return -1

    @staticmethod
    def _is_notfound(exc: Exception) -> bool:
        return exc.__class__.__name__ in ("NotFound", "NotFoundError")
