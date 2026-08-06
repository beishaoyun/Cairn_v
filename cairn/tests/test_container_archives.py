"""Container backend tests (Agent 11).

No real Docker daemon: container lifecycle is exercised against a fake docker client
(verifying the hardening kwargs of worker-sandbox-hardening §4) and the full
``docker exec`` exec path is run against a fake ``docker`` CLI
(``tests/scripts/fake_docker.py``) so ``ContainerProcess`` semantics are real.

Covers: hardening kwargs, workspace/evidence mounts (B6/B7), capture-proxy env + CA
trust injection (§4.1), C5 token rejection, orphan cleanup, path-traversal guard, and
``ExecutionBackend``/``ExecProcess`` protocol conformance.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from cairn.dispatcher.config import (
    ContainerConfig,
    DispatcherConfig,
    LocalConfig,
    RuntimeConfig,
    SecurityConfig,
    ServerConfig,
    WorkerConfig,
)
from cairn.dispatcher.runtime.backend import ExecProcess, ExecutionBackend
from cairn.dispatcher.runtime.containers import (
    ContainerBackend,
    ContainerBackendError,
    ContainerScope,
)
from cairn.dispatcher.runtime.process import resolve_workspace_path

TESTS_DIR = Path(__file__).resolve().parent
FAKE_DOCKER = [sys.executable, str(TESTS_DIR / "scripts" / "fake_docker.py")]


class NotFoundError(Exception):
    pass


class FakeContainer:
    def __init__(self, name, status="running"):
        self.name = name
        self.status = status
        self.started = 0
        self.stopped = 0
        self.removed = 0

    def start(self):
        self.started += 1
        self.status = "running"

    def stop(self):
        self.stopped += 1
        self.status = "exited"

    def remove(self, force=False):
        self.removed += 1
        self.status = "removed"


class FakeContainers:
    def __init__(self):
        self.store: dict[str, FakeContainer] = {}
        self.run_calls: list[dict] = []

    def get(self, name):
        if name not in self.store:
            raise NotFoundError(name)
        return self.store[name]

    def run(self, image, command=None, **kwargs):
        name = kwargs.get("name") or f"auto-{len(self.run_calls)}"
        kwargs = dict(kwargs)
        kwargs["image"] = image
        kwargs["command"] = command
        self.run_calls.append(kwargs)
        c = FakeContainer(name)
        self.store[name] = c
        return c

    def list(self, all=None, filters=None):
        return list(self.store.values())


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeContainers()
        self.closed = False

    def close(self):
        self.closed = True


def make_config(tmp_path, *, completed_action="stop") -> DispatcherConfig:
    return DispatcherConfig(
        server=ServerConfig(url="http://cairn-server:8000", api_token="tok"),
        runtime=RuntimeConfig(execution="container"),
        workers=[WorkerConfig(name="w1", type="mock", task_types=["explore"])],
        container=ContainerConfig(
            image="cairn-worker:test",
            network_mode="bridge",
            completed_action=completed_action,
        ),
        security=SecurityConfig(
            capture_ca_dir=str(tmp_path / "capture-ca"),
            evidence_root=str(tmp_path / "evidence"),
        ),
        local=LocalConfig(),
    )


def make_backend(tmp_path, *, client=None, scope_resolver=None, completed_action="stop"):
    return ContainerBackend(
        make_config(tmp_path, completed_action=completed_action),
        docker_client=client or FakeDockerClient(),
        docker_cli=FAKE_DOCKER,
        workspace_root=str(tmp_path / "workspace"),
        tools_root=str(tmp_path / "tools"),
        scope_resolver=scope_resolver,
    )


SCOPE = {
    "engagement_id": "eng-001",
    "network_cap": ["NET_RAW"],
    "mem_limit": "1g",
    "cpu_quota": 50000,
    "pids_limit": 256,
    "tools": ["nuclei"],
    "capture_proxy": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8080,
        "no_capture_hosts": ["api.anthropic.com", "cairn-server"],
    },
}


# --------------------------------------------------------------------------- protocol


def test_container_backend_implements_execution_backend_protocol(tmp_path):
    backend = make_backend(tmp_path)
    assert isinstance(backend, ExecutionBackend)


def test_container_name():
    assert ContainerBackend.container_name("proj_001") == "cairn-proj_001"


# --------------------------------------------------------------------------- exec argv


def test_build_docker_exec_command_env_and_wrapper():
    argv = ContainerBackend.build_docker_exec_command(
        ["docker"],
        "cairn-proj_001",
        ["claude", "-p", "hi"],
        {"A": "1", "B": "two words"},
        cwd="/home/worker/workspace",
    )
    assert argv[:2] == ["docker", "exec"]
    assert "-i" in argv
    assert "-e" in argv
    assert "A=1" in argv
    assert "B=two words" in argv
    assert "-w" in argv
    assert "/home/worker/workspace" in argv
    assert "cairn-proj_001" in argv
    idx = argv.index("sh")
    assert argv[idx + 1] == "-c"
    assert "__CAIRN_PID__" in argv[idx + 2]
    assert argv[idx + 3] == "_"
    assert argv[idx + 4:] == ["claude", "-p", "hi"]


def test_build_docker_exec_command_no_env_no_cwd():
    argv = ContainerBackend.build_docker_exec_command(["docker"], "c", ["echo", "x"])
    assert "-e" not in argv
    assert "-w" not in argv


# --------------------------------------------------------------------------- ensure_running / hardening


def test_ensure_running_creates_hardened_container(tmp_path):
    backend = make_backend(tmp_path, scope_resolver=lambda pid: SCOPE)
    backend.ensure_running("p1")

    calls = backend._client.containers.run_calls
    assert len(calls) == 1
    kw = calls[0]
    assert kw["image"] == "cairn-worker:test"
    assert kw["name"] == "cairn-p1"
    assert kw["user"] == "worker:worker"
    assert kw["cap_drop"] == ["ALL"]
    assert set(kw["cap_add"]) == {"NET_RAW"}  # from scope.network_cap
    assert kw["read_only"] is True
    assert kw["tmpfs"] == {"/tmp": "size=512m"}
    assert kw["security_opt"] == ["no-new-privileges"]
    assert kw["mem_limit"] == "1g"
    assert kw["cpu_quota"] == 50000
    assert kw["pids_limit"] == 256
    assert kw["ulimits"] == [{"Name": "nofile", "Soft": 1024, "Hard": 2048}]
    assert kw["network_mode"] == "bridge"
    # labels (orphan cleanup wiring, v2 §8.2)
    assert kw["labels"]["cairn.project"] == "p1"
    assert kw["labels"]["cairn.engagement"] == "eng-001"
    assert kw["labels"]["cairn.managed"] == "true"


def test_ensure_running_mounts_workspace_evidence_and_tools(tmp_path):
    backend = make_backend(tmp_path, scope_resolver=lambda pid: SCOPE)
    backend.ensure_running("p1")
    volumes = backend._client.containers.run_calls[0]["volumes"]

    # B6: workspace volume rw
    ws_host = str((Path(tmp_path) / "workspace" / "p1").resolve())
    assert volumes[ws_host] == {"bind": "/home/worker/workspace", "mode": "rw"}

    # B7: evidence is engagement-scoped (NOT project-scoped), rw
    ev_host = str((Path(tmp_path) / "evidence" / "eng-001").resolve())
    assert volumes[ev_host] == {"bind": "/home/worker/evidence", "mode": "rw"}
    assert volumes.get(str((Path(tmp_path) / "evidence" / "p1").resolve())) is None

    # tools: ro mount per engagement authorisation
    tools_host = str((Path(tmp_path) / "tools").resolve())
    assert volumes[tools_host] == {"bind": "/opt/tools", "mode": "ro"}

    # CA cert not mounted (file does not exist yet)
    assert all(v["bind"] != "/etc/cairn-capture/ca.pem" for v in volumes.values())


def test_ensure_running_mounts_ca_when_file_exists(tmp_path):
    ca_dir = tmp_path / "capture-ca" / "eng-001"
    ca_dir.mkdir(parents=True)
    (ca_dir / "ca.pem").write_text("fake-cert")
    backend = make_backend(tmp_path, scope_resolver=lambda pid: SCOPE)
    backend.ensure_running("p1")
    volumes = backend._client.containers.run_calls[0]["volumes"]
    ca_mounts = [v for v in volumes.values() if v["bind"] == "/etc/cairn-capture/ca.pem"]
    assert len(ca_mounts) == 1
    assert ca_mounts[0]["mode"] == "ro"


def test_ensure_running_reuses_running_container(tmp_path):
    backend = make_backend(tmp_path, scope_resolver=lambda pid: SCOPE)
    backend.ensure_running("p1")
    backend.ensure_running("p1")
    assert len(backend._client.containers.run_calls) == 1
    assert backend._client.containers.store["cairn-p1"].started == 0


def test_ensure_running_starts_stopped_container(tmp_path):
    backend = make_backend(tmp_path)
    backend._client.containers.store["cairn-p1"] = FakeContainer("cairn-p1", status="exited")
    backend.ensure_running("p1")
    assert backend._client.containers.store["cairn-p1"].started == 1
    assert len(backend._client.containers.run_calls) == 0


def test_ensure_running_no_network_cap_by_default(tmp_path):
    backend = make_backend(tmp_path, scope_resolver=lambda pid: None)
    backend.ensure_running("p1")
    kw = backend._client.containers.run_calls[0]
    assert kw["cap_add"] is None  # all dropped


def test_ensure_running_host_network_allowed_only_explicitly(tmp_path):
    cfg = make_config(tmp_path)
    cfg.container.network_mode = "host"
    backend = ContainerBackend(
        cfg,
        docker_client=FakeDockerClient(),
        docker_cli=FAKE_DOCKER,
        workspace_root=str(tmp_path / "workspace"),
    )
    backend.ensure_running("p1")
    assert backend._client.containers.run_calls[0]["network_mode"] == "host"


# --------------------------------------------------------------------------- capture env (C6 / §4.1)


def test_capture_proxy_env_injected():
    backend = make_backend(Path("/tmp/unused"))
    scope = ContainerScope(
        engagement_id="eng-001",
        capture_proxy={
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8080,
            "no_capture_hosts": ["api.anthropic.com", "cairn-server"],
        },
    )
    env = backend._build_env({"X": "1"}, scope)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:8080"
    assert env["SSL_CERT_FILE"] == "/etc/cairn-capture/ca.pem"
    assert env["REQUESTS_CA_BUNDLE"] == "/etc/cairn-capture/ca.pem"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/cairn-capture/ca.pem"
    assert env["NO_PROXY"] == "api.anthropic.com,cairn-server"
    assert env["X"] == "1"


def test_capture_proxy_disabled_no_injection():
    backend = make_backend(Path("/tmp/unused"))
    env = backend._build_env({"HTTPS_PROXY": "http://keep-me"}, ContainerScope())
    assert env["HTTPS_PROXY"] == "http://keep-me"
    assert "NODE_EXTRA_CA_CERTS" not in env


# --------------------------------------------------------------------------- C5 token rejection


def test_build_exec_rejects_cairn_token(tmp_path):
    backend = make_backend(tmp_path)
    backend.ensure_running("p1")
    with pytest.raises(ContainerBackendError):
        backend.build_exec_process(["echo", "hi"], env={"CAIRN_API_TOKEN": "secret"})
    with pytest.raises(ContainerBackendError):
        backend.build_exec_process(["echo", "hi"], env={"CAIRN_SERVER_URL": "http://x"})


def test_custom_token_env_name_rejected(tmp_path):
    cfg = make_config(tmp_path)
    cfg.security.api_token_env = "CAIRN_SINGLE_TOKEN"
    backend = ContainerBackend(
        cfg,
        docker_client=FakeDockerClient(),
        docker_cli=FAKE_DOCKER,
        workspace_root=str(tmp_path / "workspace"),
    )
    backend.ensure_running("p1")
    with pytest.raises(ContainerBackendError):
        backend.build_exec_process(["echo", "hi"], env={"CAIRN_SINGLE_TOKEN": "x"})


# --------------------------------------------------------------------------- full exec path (fake docker CLI)


def test_build_exec_process_runs_in_container(tmp_path):
    backend = make_backend(tmp_path, scope_resolver=lambda pid: SCOPE)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["echo", "from-container"])
    assert isinstance(proc, ExecProcess)
    assert proc.pid is not None  # host pid of the docker-exec client while running
    out, err = proc.communicate()
    assert "from-container" in out
    assert proc.poll() == 0


def test_build_exec_process_without_ensure_running_raises(tmp_path):
    backend = make_backend(tmp_path)
    with pytest.raises(ContainerBackendError):
        backend.build_exec_process(["echo", "hi"])


def test_build_exec_process_timeout_sets_timed_out(tmp_path):
    backend = make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["sleep", "60"], timeout=0.5)
    out, err = proc.communicate()
    assert proc.timed_out is True
    assert proc.poll() is not None


def test_container_process_kill_sigkill_immediate(tmp_path):
    backend = make_backend(tmp_path)
    backend.ensure_running("p1")
    proc = backend.build_exec_process(["sleep", "60"])
    proc.kill(9)  # C1 immediate path — must not hang
    deadline = time.monotonic() + 3
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert proc.poll() is not None


# --------------------------------------------------------------------------- write_text_file


def test_write_text_file_writes_to_host_workspace(tmp_path):
    backend = make_backend(tmp_path)
    backend.ensure_running("p1")
    backend.write_text_file("p1", "notes/a.txt", "c")
    assert (Path(tmp_path) / "workspace" / "p1" / "notes" / "a.txt").read_text() == "c"


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "a/./b", ""])
def test_write_text_file_rejects_traversal(tmp_path, bad):
    backend = make_backend(tmp_path)
    backend.ensure_running("p1")
    with pytest.raises(ValueError):
        backend.write_text_file("p1", bad, "c")


def test_resolve_workspace_path_container_absolute():
    base = Path("/var/cairn/workspace")
    target = resolve_workspace_path(base, "p1", "/home/worker/workspace/a/b.txt")
    assert target == (base / "p1" / "a" / "b.txt").resolve()


# --------------------------------------------------------------------------- cleanup / orphan


def test_cleanup_managed_container_stop(tmp_path):
    backend = make_backend(tmp_path, completed_action="stop")
    backend._client.containers.store["cairn-p1"] = FakeContainer("cairn-p1")
    backend.cleanup_managed_container("p1")
    c = backend._client.containers.store["cairn-p1"]
    assert c.stopped == 1
    assert c.removed == 0


def test_cleanup_managed_container_remove(tmp_path):
    backend = make_backend(tmp_path, completed_action="remove")
    backend._client.containers.store["cairn-p1"] = FakeContainer("cairn-p1")
    backend.cleanup_managed_container("p1")
    c = backend._client.containers.store["cairn-p1"]
    assert c.removed == 1
    assert c.stopped == 0


def test_cleanup_managed_container_missing_is_noop(tmp_path):
    backend = make_backend(tmp_path)
    backend.cleanup_managed_container("p1")  # must not raise


def test_managed_container_names(tmp_path):
    backend = make_backend(tmp_path)
    backend._client.containers.store["cairn-p1"] = FakeContainer("cairn-p1")
    backend._client.containers.store["cairn-p2"] = FakeContainer("cairn-p2")
    assert sorted(backend.managed_container_names()) == ["cairn-p1", "cairn-p2"]


def test_cleanup_orphan_removes_unknown(tmp_path):
    backend = make_backend(tmp_path)
    backend._client.containers.store["cairn-p1"] = FakeContainer("cairn-p1")
    backend._client.containers.store["cairn-p2"] = FakeContainer("cairn-p2")
    orphans = backend.cleanup_orphan(["p1"])  # p1 known, p2 orphan
    assert orphans == ["cairn-p2"]
    assert backend._client.containers.store["cairn-p2"].removed == 1
    assert backend._client.containers.store["cairn-p1"].removed == 0


def test_close_closes_owned_client(tmp_path):
    backend = make_backend(tmp_path)
    # simulate a self-created docker client: owned → close() must release it
    fake = FakeDockerClient()
    backend._client = fake
    backend._owns_client = True
    backend.close()
    assert fake.closed is True


def test_close_does_not_close_injected_client(tmp_path):
    client = FakeDockerClient()
    backend = make_backend(tmp_path, client=client)
    backend.close()
    assert client.closed is False
