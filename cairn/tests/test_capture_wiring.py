"""真实抓包接线验收测试（缺口 1-3 · 让「真实 mitmproxy 抓包」成为平台正式能力）。

覆盖：
- **缺口 1** scope_resolver 接入 ContainerBackend：
  ``resolve_scope_policy``（scope_policy JSON → ContainerScope）+ 带 resolver 的
  ContainerBackend 在 capture 时 ``_build_env`` 注入 HTTPS_PROXY/SSL_CERT_FILE、
  ``_run_kwargs`` 挂载专属 CA；capture + host 网络 → 抛 ``ContainerBackendError``。
- **缺口 2** Dispatcher 侧 CaptureProxyManager：FakeProxyEngine start/stop 幂等、
  CA 落盘 ``{ca_dir}/{eid}/ca.pem``、addon env 契约；loop 集成（active 起 / kill 停 /
  periodic 停非 active）。
- **缺口 3** mitm addon 写回 traffic_root：fake flow 落盘路径/分片/sha256/F8 回写
  （entry 与 ``TrafficIndexRequest`` 契约一致）。
- 真实 mitmdump 集成测试：环境无 mitmproxy → ``skip`` 并注明。

运行：``uv run --project cairn pytest cairn/tests/test_capture_wiring.py -q``
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time

import pytest

from cairn.config import ServerConfig
from cairn.dispatcher.config import (
    ContainerConfig,
    DispatcherConfig,
    LocalConfig,
    RuntimeConfig,
    SecurityConfig,
    ServerConfig as DispatchServerConfig,
    WorkerConfig,
    load_dict,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.containers import (
    ContainerBackend,
    ContainerBackendError,
    ContainerScope,
    resolve_scope_policy,
)
from cairn.dispatcher.runtime.context import DispatcherContext
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.dispatcher.workers.health import WorkerHealth
from cairn.server.app import create_app
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Gap 1 helpers（复用 test_container_archives 的 fake docker 模式）
# ---------------------------------------------------------------------------


class _NotFound(Exception):
    pass


class _FakeContainer:
    def __init__(self, name, status="running"):
        self.name = name
        self.status = status

    def start(self):
        self.status = "running"

    def stop(self):
        self.status = "exited"

    def remove(self, force=False):
        self.status = "removed"


class _FakeContainers:
    def __init__(self):
        self.store: dict[str, _FakeContainer] = {}
        self.run_calls: list[dict] = []

    def get(self, name):
        if name not in self.store:
            raise _NotFound(name)
        return self.store[name]

    def run(self, image, command=None, **kwargs):
        name = kwargs.get("name") or f"auto-{len(self.run_calls)}"
        kwargs = dict(kwargs)
        kwargs["image"] = image
        kwargs["command"] = command
        self.run_calls.append(kwargs)
        c = _FakeContainer(name)
        self.store[name] = c
        return c

    def list(self, all=None, filters=None):
        return list(self.store.values())


class _FakeDockerClient:
    def __init__(self):
        self.containers = _FakeContainers()
        self.closed = False

    def close(self):
        self.closed = True


def _make_config(tmp_path, *, network_mode="bridge") -> DispatcherConfig:
    return DispatcherConfig(
        server=DispatchServerConfig(url="http://cairn-server:8000", api_token="tok"),
        runtime=RuntimeConfig(execution="container"),
        workers=[WorkerConfig(name="w1", type="mock", task_types=["explore"])],
        container=ContainerConfig(image="cairn-worker:test", network_mode=network_mode),
        security=SecurityConfig(
            capture_ca_dir=str(tmp_path / "capture-ca"),
            evidence_root=str(tmp_path / "evidence"),
            traffic_root=str(tmp_path / "traffic"),
        ),
        local=LocalConfig(),
    )


def _make_container_backend(tmp_path, *, resolver=None, network_mode="bridge") -> ContainerBackend:
    return ContainerBackend(
        _make_config(tmp_path, network_mode=network_mode),
        docker_client=_FakeDockerClient(),
        docker_cli=["docker"],
        workspace_root=str(tmp_path / "workspace"),
        tools_root=str(tmp_path / "tools"),
        scope_resolver=resolver,
    )


# ---------------------------------------------------------------------------
# Gap 1：resolve_scope_policy / ContainerBackend 接线
# ---------------------------------------------------------------------------


class TestResolveScopePolicy:
    def test_maps_fields(self):
        scope = resolve_scope_policy("eng-001", {
            "network_cap": True,
            "resources": {"mem_limit": "1g", "cpu_quota": 50000, "pids_limit": 256},
            "tools": ["nuclei", "nmap"],
            "capture_proxy": {
                "enabled": True, "host": "127.0.0.1", "port": 8080,
                "no_capture_hosts": ["api.anthropic.com"],
            },
        })
        assert scope.engagement_id == "eng-001"
        assert scope.network_cap == ["NET_RAW", "NET_ADMIN"]  # bool True → NET_RAW+NET_ADMIN
        assert scope.mem_limit == "1g"
        assert scope.cpu_quota == 50000
        assert scope.pids_limit == 256
        assert scope.tools == ["nuclei", "nmap"]
        assert scope.capture_proxy == {
            "enabled": True, "host": "127.0.0.1", "port": 8080,
            "no_capture_hosts": ["api.anthropic.com"],
        }

    def test_network_cap_list_and_false(self):
        scope = resolve_scope_policy("e", {"network_cap": ["NET_ADMIN"]})
        assert scope.network_cap == ["NET_ADMIN"]
        assert resolve_scope_policy("e", {"network_cap": False}).network_cap == []
        assert resolve_scope_policy("e", {}).network_cap == []

    def test_defaults_when_scope_policy_empty(self):
        scope = resolve_scope_policy("eng-001", None)
        assert scope.engagement_id == "eng-001"
        assert scope.network_cap == []
        assert scope.mem_limit is None
        assert scope.cpu_quota is None
        assert scope.pids_limit is None
        assert scope.tools is None
        assert scope.capture_proxy is None

    def test_capture_host_and_no_hosts_defaults(self, monkeypatch):
        monkeypatch.delenv("CAIRN_CAPTURE_HOST", raising=False)
        scope = resolve_scope_policy("e", {"capture_proxy": {"enabled": True}})
        assert scope.capture_proxy["enabled"] is True
        assert scope.capture_proxy["host"] == "172.17.0.1"  # Docker 默认 bridge 网关
        assert scope.capture_proxy["port"] == 8080
        assert set(scope.capture_proxy["no_capture_hosts"]) == {
            "api.anthropic.com", "api.deepseek.com", "cairn-server",
        }
        monkeypatch.setenv("CAIRN_CAPTURE_HOST", "host.docker.internal")
        assert resolve_scope_policy("e", {"capture_proxy": {"enabled": True}}).capture_proxy["host"] == "host.docker.internal"


class TestContainerBackendScopeResolver:
    def test_resolver_injects_capture_env(self, tmp_path):
        scope_policy = {
            "capture_proxy": {
                "enabled": True, "host": "127.0.0.1", "port": 8080,
                "no_capture_hosts": ["api.anthropic.com"],
            }
        }
        backend = _make_container_backend(
            tmp_path, resolver=lambda pid: resolve_scope_policy("eng-001", scope_policy)
        )
        env = backend._build_env({"X": "1"}, backend._resolve_scope("proj-001"))
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:8080"
        assert env["HTTP_PROXY"] == "http://127.0.0.1:8080"
        assert env["SSL_CERT_FILE"] == "/etc/cairn-capture/ca.pem"
        assert env["REQUESTS_CA_BUNDLE"] == "/etc/cairn-capture/ca.pem"
        assert env["NODE_EXTRA_CA_CERTS"] == "/etc/cairn-capture/ca.pem"
        assert env["NO_PROXY"] == "api.anthropic.com"
        assert env["X"] == "1"

    def test_resolver_disabled_no_injection(self, tmp_path):
        backend = _make_container_backend(tmp_path, resolver=lambda pid: ContainerScope())
        env = backend._build_env({"HTTPS_PROXY": "http://keep-me"}, backend._resolve_scope("p"))
        assert env["HTTPS_PROXY"] == "http://keep-me"
        assert "NODE_EXTRA_CA_CERTS" not in env

    def test_resolver_mounts_ca_and_engagement_evidence(self, tmp_path):
        ca_dir = tmp_path / "capture-ca" / "eng-001"
        ca_dir.mkdir(parents=True)
        (ca_dir / "ca.pem").write_text("CERT")
        scope_policy = {"capture_proxy": {"enabled": True, "host": "127.0.0.1", "port": 8080}}
        backend = _make_container_backend(
            tmp_path, resolver=lambda pid: resolve_scope_policy("eng-001", scope_policy)
        )
        kwargs = backend._run_kwargs("proj-001", backend._resolve_scope("proj-001"))
        vols = kwargs["volumes"]
        assert str(ca_dir / "ca.pem") in vols
        assert vols[str(ca_dir / "ca.pem")] == {"bind": "/etc/cairn-capture/ca.pem", "mode": "ro"}
        # B7：evidence 按 engagement 挂载（非 project）
        assert str(tmp_path / "evidence" / "eng-001") in vols
        assert kwargs["labels"]["cairn.engagement"] == "eng-001"

    def test_capture_plus_host_network_raises(self, tmp_path):
        scope_policy = {"capture_proxy": {"enabled": True, "host": "127.0.0.1", "port": 8080}}
        backend = _make_container_backend(
            tmp_path, network_mode="host",
            resolver=lambda pid: resolve_scope_policy("eng-001", scope_policy),
        )
        with pytest.raises(ContainerBackendError):
            backend._run_kwargs("proj-001", backend._resolve_scope("proj-001"))

    def test_resolver_missing_engagement_safe_default(self, tmp_path):
        # 反查不到 eid → 空 ContainerScope（安全降级，不注入任何 capture env）
        backend = _make_container_backend(tmp_path, resolver=lambda pid: ContainerScope())
        assert backend._resolve_scope("nope") == ContainerScope()


# ---------------------------------------------------------------------------
# Gap 2：Dispatcher 侧 CaptureProxyManager
# ---------------------------------------------------------------------------

from cairn.dispatcher.capture.proxy import (  # noqa: E402
    CaptureProxyManager,
    FakeProxyEngine,
)


class TestCaptureProxyManager:
    def test_start_stop_idempotent(self, tmp_path):
        ca_dir = str(tmp_path / "ca")
        manager = CaptureProxyManager(ca_dir=ca_dir, engine_factory=lambda: FakeProxyEngine())
        state = manager.start_engagement(
            "eng-001", {"capture_proxy": {"enabled": True, "port": 8080}},
            server_url="http://cairn-server:8000", capture_token="captok",
            traffic_root="/var/cairn/traffic", allow_hosts=["10.0.0.5"],
        )
        assert state is not None
        assert manager.is_running("eng-001")
        assert manager.running_ports() == {"eng-001": 8080}
        assert manager.running_eids() == ["eng-001"]
        # CA 落盘（容器侧挂载路径一致）
        assert os.path.isfile(os.path.join(ca_dir, "eng-001", "ca.pem"))
        assert os.path.isfile(os.path.join(ca_dir, "eng-001", "ca.key"))
        assert oct(os.stat(os.path.join(ca_dir, "eng-001", "ca.key")).st_mode & 0o777) == "0o700"
        # 幂等 start：再次 start 返回同一 state，不再启动新引擎
        engine = state.engine
        n = len(engine.start_calls)
        assert manager.start_engagement(
            "eng-001", {"capture_proxy": {"enabled": True}},
            server_url="http://cairn-server:8000", capture_token="captok",
            traffic_root="/var/cairn/traffic", allow_hosts=["10.0.0.5"],
        ) is state
        assert len(engine.start_calls) == n
        # stop 幂等
        assert manager.stop_engagement("eng-001") is True
        assert not manager.is_running("eng-001")
        assert manager.stop_engagement("eng-001") is False

    def test_disabled_no_start(self, tmp_path):
        manager = CaptureProxyManager(ca_dir=str(tmp_path / "ca"), engine_factory=lambda: FakeProxyEngine())
        state = manager.start_engagement(
            "eng-001", {"capture_proxy": {"enabled": False}},
            server_url="http://x", capture_token="t", traffic_root="/tmp",
        )
        assert state is None
        assert manager.running_eids() == []

    def test_addon_env_contract(self, tmp_path):
        manager = CaptureProxyManager(ca_dir=str(tmp_path / "ca"), engine_factory=lambda: FakeProxyEngine())
        state = manager.start_engagement(
            "eng-001", {"capture_proxy": {"enabled": True, "port": 9000}},
            server_url="http://cairn-server:8000", capture_token="captok",
            traffic_root="/var/cairn/traffic",
            allow_hosts=["10.0.0.5", "10.0.0.6"], no_hosts=["cairn-server"],
        )
        env = state.engine.last_env
        assert env["CAIRN_EID"] == "eng-001"
        assert env["CAIRN_SERVER_URL"] == "http://cairn-server:8000"
        assert env["CAIRN_CAPTURE_TOKEN"] == "captok"
        assert env["CAIRN_TRAFFIC_ROOT"] == "/var/cairn/traffic"
        assert set(env["CAIRN_ALLOW_HOSTS"].split(",")) == {"10.0.0.5", "10.0.0.6"}
        assert env["CAIRN_NO_HOSTS"].split(",") == ["cairn-server"]
        assert state.engine.last_addon is not None and state.engine.last_addon.endswith("addon.py")


# ---------------------------------------------------------------------------
# Gap 2 集成：DispatcherLoop 生命周期接线
# ---------------------------------------------------------------------------


def _make_server(tmp_path):
    os.environ["CAIRN_API_TOKEN"] = "test-token"
    os.environ["CAIRN_CAPTURE_TOKEN"] = "captok"
    cfg = ServerConfig(
        db_path=str(tmp_path / "test.db"),
        api_token="test-token",
        evidence_root=str(tmp_path / "evidence"),
        traffic_root=str(tmp_path / "traffic"),
        archive_root=str(tmp_path / "archive"),
        logs_root=str(tmp_path / "logs"),
    )
    app = create_app(cfg)
    tc = TestClient(app)
    client = CairnClient("http://test", "test-token", client=tc)
    return client, cfg


def _make_loop_config():
    raw = {
        "server": {"url": "http://test", "api_token": "${CAIRN_API_TOKEN}"},
        "runtime": {
            "execution": "local", "interval": 1, "max_workers": 8,
            "max_running_projects": 3, "max_project_workers": 4,
            "worker_healthcheck": "disabled",
        },
        "workers": [
            {"name": "mock-A", "type": "mock",
             "task_types": ["bootstrap", "reason", "explore", "verify", "audit"],
             "max_running": 2, "priority": 0, "verify_eligible": True},
        ],
    }
    return load_dict(raw, env={"CAIRN_API_TOKEN": "test-token"})


def _make_ctx(config, *, log=None):
    shutdown = threading.Event()
    return DispatcherContext(
        config=config,
        drivers={},
        health=WorkerHealth(mode="disabled"),
        shutdown=shutdown,
        log=log or (lambda m: None),
    )


def _create_active_engagement(client, *, scope_policy=None):
    eng = client._request(
        "POST", "/engagements",
        json={
            "title": "e2e",
            "authorized_start_at": "2026-01-01T00:00:00Z",
            "authorized_end_at": "2026-12-31T00:00:00Z",
            "scope_policy": scope_policy or {},
        },
    )
    eid = eng["id"]
    client._request("POST", f"/engagements/{eid}/targets",
                    json={"value": "10.0.0.5", "scope": "authorized"})
    client._request("PUT", f"/engagements/{eid}/status", json={"status": "active"})
    return eid


def _make_loop(tmp_path, client):
    config = _make_loop_config()
    ctx = _make_ctx(config)
    backend = LocalBackend(config, workspace_root=str(tmp_path / "ws"))
    manager = CaptureProxyManager(ca_dir=str(tmp_path / "ca"), engine_factory=lambda: FakeProxyEngine())
    loop = DispatcherLoop(ctx, client=client, backend=backend, interval=0.01, capture_manager=manager)
    return loop, manager


class TestLoopCaptureLifecycle:
    def test_ensure_capture_starts_proxy(self, tmp_path):
        client, _cfg = _make_server(tmp_path)
        eid = _create_active_engagement(
            client, scope_policy={"capture_proxy": {"enabled": True, "port": 8080,
                                                   "no_capture_hosts": ["cairn-server"]}}
        )
        loop, manager = _make_loop(tmp_path, client)
        loop._ensure_capture({"id": eid})
        assert manager.is_running(eid)
        state = manager.get(eid)
        assert state is not None
        assert state.engine.last_env["CAIRN_EID"] == eid
        # C11：allow 白名单由 authorized targets 派生（10.0.0.5）
        assert "10.0.0.5" in state.engine.last_env["CAIRN_ALLOW_HOSTS"].split(",")
        assert "cairn-server" in state.engine.last_env["CAIRN_NO_HOSTS"].split(",")
        # 幂等：再次 ensure 不再重启
        n = len(state.engine.start_calls)
        loop._ensure_capture({"id": eid})
        assert len(state.engine.start_calls) == n

    def test_ensure_capture_disabled_no_start(self, tmp_path):
        client, _cfg = _make_server(tmp_path)
        eid = _create_active_engagement(client)  # 无 scope_policy → capture 未启用
        loop, manager = _make_loop(tmp_path, client)
        loop._ensure_capture({"id": eid})
        assert not manager.is_running(eid)

    def test_handle_kill_stops_capture(self, tmp_path):
        client, _cfg = _make_server(tmp_path)
        eid = _create_active_engagement(
            client, scope_policy={"capture_proxy": {"enabled": True, "port": 8080}}
        )
        loop, manager = _make_loop(tmp_path, client)
        loop._ensure_capture({"id": eid})
        assert manager.is_running(eid)
        loop._handle_kill(eid)  # C3：熔断 → 停抓包
        assert not manager.is_running(eid)

    def test_reconcile_stops_inactive(self, tmp_path):
        client, _cfg = _make_server(tmp_path)
        eid = _create_active_engagement(
            client, scope_policy={"capture_proxy": {"enabled": True, "port": 8080}}
        )
        loop, manager = _make_loop(tmp_path, client)
        loop._ensure_capture({"id": eid})
        assert manager.is_running(eid)
        # active → paused：离开 active 集合 → periodic reconcile 停止代理
        client._request("PUT", f"/engagements/{eid}/status", json={"status": "paused"})
        loop._reconcile_capture_proxies()
        assert not manager.is_running(eid)


# ---------------------------------------------------------------------------
# Gap 3：mitm addon 写回 traffic_root
# ---------------------------------------------------------------------------

from cairn.dispatcher.capture.addon import CairnCaptureAddon  # noqa: E402


class _FakeConn:
    peername = ("172.17.0.3", 50000)


class _FakeRequest:
    method = "GET"
    host = "10.0.0.5"
    path = "/login?x=1"
    http_version = "1.1"
    headers = {"Host": "10.0.0.5", "User-Agent": "curl/8.0"}
    content = b""

    @property
    def pretty_url(self):
        return f"http://{self.host}{self.path}"


class _FakeResponse:
    status_code = 200
    reason = "OK"
    http_version = "1.1"
    headers = {"Content-Type": "text/html"}
    content = b"ok!"


class _FakeFlow:
    def __init__(self):
        self.request = _FakeRequest()
        self.response = _FakeResponse()
        self.client_conn = _FakeConn()
        self.metadata = {}


def _make_addon(traffic_root, eid="eng-001", *, index_fn=None):
    addon = CairnCaptureAddon(index_fn=index_fn or (lambda e: None))
    addon.traffic_root = str(traffic_root)
    addon.eid = eid
    addon.allow_hosts = {"10.0.0.5"}
    addon.no_hosts = {"cairn-server"}
    addon._seq = 1
    return addon


class TestAddon:
    def test_capture_flow_writes_files_and_index(self, tmp_path):
        traffic_root = tmp_path / "traffic"
        traffic_root.mkdir()
        indexed: list[dict] = []
        addon = _make_addon(traffic_root, index_fn=indexed.append)
        entry = addon.capture_flow(_FakeFlow())
        assert entry is not None
        assert entry["host"] == "10.0.0.5"
        assert entry["method"] == "GET"
        assert entry["seq"] == 1
        assert entry["chunk_count"] == 1
        assert entry["client_ip"] == "172.17.0.3"  # C12：bridge 独立 IP
        # 文件落盘：{traffic_root}/{eid}/{seq}/req.bin|resp.bin
        assert entry["req_path"] == "eng-001/1/req.bin"
        assert entry["resp_path"] == "eng-001/1/resp.bin"
        req_full = traffic_root / entry["req_path"]
        resp_full = traffic_root / entry["resp_path"]
        assert req_full.is_file() and resp_full.is_file()
        # sha256 = sha256(req ‖ resp)（server.services.capture._package_sha256 语义）
        req = req_full.read_bytes()
        resp = resp_full.read_bytes()
        assert entry["sha256"] == hashlib.sha256(req + resp).hexdigest()
        # F8 回写调用
        assert indexed == [entry]

    def test_fail_closed_skips_non_allow_host(self, tmp_path):
        traffic_root = tmp_path / "traffic"
        traffic_root.mkdir()
        addon = _make_addon(traffic_root)
        flow = _FakeFlow()
        assert addon.capture_flow(flow) is not None
        flow.request.host = "evil.example.org"  # 白名单外 → 不落盘
        assert addon.capture_flow(flow) is None
        assert not (traffic_root / "eng-001" / "2").exists()

    def test_chunking_large_payload(self, tmp_path):
        traffic_root = tmp_path / "traffic"
        traffic_root.mkdir()
        addon = _make_addon(traffic_root)
        import cairn.dispatcher.capture.addon as addon_mod

        old = addon_mod.CHUNK_THRESHOLD_BYTES
        addon_mod.CHUNK_THRESHOLD_BYTES = 100
        try:
            flow = _FakeFlow()
            flow.request.content = b"A" * 250   # 原始字节（含头）> 阈值 → 分片
            flow.response.content = b"B" * 50
            entry = addon.capture_flow(flow)
        finally:
            addon_mod.CHUNK_THRESHOLD_BYTES = old
        assert entry is not None
        # 统一用 max(req, resp) 的 ceil(字节/阈值) 分片数（读取侧同一 chunk_count 拼回）
        import math

        assert entry["chunk_count"] == max(
            math.ceil(entry["req_bytes"] / 100), math.ceil(entry["resp_bytes"] / 100)
        )
        assert entry["chunk_count"] > 1
        base = traffic_root / "eng-001" / "1"
        assert (base / "req.bin.0").is_file()
        assert (base / f"req.bin.{entry['chunk_count'] - 1}").is_file()
        assert (base / "resp.bin.0").is_file()

        # 与 server.services.capture._read_payload 同 chunk_count 拼回 = 原始字节
        def read_all(rel, count):
            if count <= 1:
                return (traffic_root / rel).read_bytes()
            out = b""
            for i in range(count):
                out += (traffic_root / f"{rel}.{i}").read_bytes()
            return out

        req_raw = read_all("eng-001/1/req.bin", entry["chunk_count"])
        resp_raw = read_all("eng-001/1/resp.bin", entry["chunk_count"])
        assert req_raw.endswith(b"A" * 250)
        assert resp_raw.endswith(b"B" * 50)
        assert entry["sha256"] == hashlib.sha256(req_raw + resp_raw).hexdigest()

    def test_entry_matches_server_index_contract(self, tmp_path):
        """F8 body 与 routers/traffic.py TrafficIndexRequest 完全一致（extra=forbid）。"""
        from cairn.server.routers.traffic import TrafficIndexRequest

        traffic_root = tmp_path / "traffic"
        traffic_root.mkdir()
        addon = _make_addon(traffic_root)
        entry = addon.capture_flow(_FakeFlow())
        assert entry is not None
        parsed = TrafficIndexRequest(**entry)  # 字段超集/缺失都会在此抛 422
        assert parsed.method == "GET"
        assert parsed.host == "10.0.0.5"
        assert parsed.chunk_count == 1
        assert parsed.seq == 1


# ---------------------------------------------------------------------------
# 真实 mitmdump 集成（环境缺 mitmproxy → skip）
# ---------------------------------------------------------------------------


def _integration_ready() -> bool:
    return shutil.which("mitmdump") is not None


@pytest.mark.skipif(not _integration_ready(), reason="需要真实 mitmdump（当前环境无 mitmproxy）")
def test_addon_integration_real_mitmdump(tmp_path):
    """真实 mitmdump + addon → 本地 http 测试服 → 断言流量文件落盘。

    仅当 ``mitmdump`` 在 PATH 上才运行；否则 skip。addon 索引回写指向不可达地址
    （``http://127.0.0.1:1``），回写失败只记 stderr，不阻塞文件落盘。
    """
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    traffic_root = tmp_path / "traffic"
    traffic_root.mkdir()
    ca_dir = tmp_path / "ca"
    manager = CaptureProxyManager(ca_dir=str(ca_dir), engine_factory=lambda: FakeProxyEngine())
    state = manager.start_engagement(
        "eng-001",
        {"capture_proxy": {"enabled": True, "port": 0}},
        server_url="http://127.0.0.1:1",
        capture_token="captok",
        traffic_root=str(traffic_root),
        allow_hosts=["localhost", "127.0.0.1"],
        no_hosts=["cairn-server"],
    )
    if state is None:
        pytest.skip("manager 未能启动引擎")
    engine = state.engine
    # mitmdump 进程手动拉起（engine 是 fake，仅持有参数）
    proxy_port = 18099
    addon = manager._addon_path
    env = dict(os.environ)
    env.update(state.engine.last_env)
    proc = subprocess.Popen(
        ["mitmdump", "-q", "--listen-host", "127.0.0.1", "--listen-port", str(proxy_port), "-s", addon],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 18098), SimpleHTTPRequestHandler)
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    try:
        # 经代理请求本地测试服
        import urllib.request

        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}",
                                         "https": f"http://127.0.0.1:{proxy_port}"})
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with opener.open("http://127.0.0.1:18098/hello", timeout=5) as resp:
                    resp.read()
                break
            except Exception:
                time.sleep(0.3)
        req_file = traffic_root / "eng-001" / "1" / "req.bin"
        assert req_file.is_file(), f"流量未落盘: {req_file}"
        assert b"GET /hello" in req_file.read_bytes()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        httpd.shutdown()
        http_thread.join(timeout=5)
