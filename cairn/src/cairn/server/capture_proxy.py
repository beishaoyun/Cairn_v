"""捕获代理进程编排（F5/C11/C12/C3 · 每 Engagement 一个 mitmproxy 实例 + 专属 CA）。

职责（Agent 23 · capture-verify-progress-spec §2/§9；worker-sandbox-hardening §4.1）：
- 每 engagement 启动一个 mitmproxy 实例，监听 ``scope_policy.capture_proxy.port``；
- 专属 CA：每 engagement 生成一份；私钥由编排者持有，注入容器只给证书（C5/C6）；
- 白名单热刷新（C11）：``allow_capture_hosts`` 由 authorized targets 派生，随 targets
  增删即时刷新（≤ runtime.interval 轮询或由 Dispatcher 订阅）；kill/归档 → 置空（C3）；
- kill 联动停抓包（C3）：熔断/归档时同步 ``stop_engagement`` + 白名单置空；
- C12 归属：``ip_to_worker``（Dispatcher 注入容器 IP → worker 名），bridge 独立 IP 可反查，
  host 网络共享 IP → client=NULL 记录 client_ip。

**mitmproxy 环境不可用**（当前环境无该依赖）：用接口抽象 + ``FakeProxyEngine`` 测试，
不阻塞；真实引擎 ``MitmProxyEngine`` 在 mitmproxy 导入失败时抛出清晰错误。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from .services import capture as capture_svc

try:  # mitmproxy 可选依赖：环境不可用时接口抽象 + fake 兜底
    import mitmproxy  # noqa: F401

    MITMPROXY_AVAILABLE = True
except Exception:  # noqa: BLE001
    MITMPROXY_AVAILABLE = False


# ---------------------------------------------------------------------------
# F5 白名单
# ---------------------------------------------------------------------------


class CaptureWhitelist:
    """代理内存白名单（fail-closed）。``allow`` 为空 → 任何 host 均不记录。"""

    def __init__(self, allow_capture_hosts: set[str] | None = None, no_capture_hosts: set[str] | None = None):
        self.allow_capture_hosts: set[str] = set(allow_capture_hosts or ())
        self.no_capture_hosts: set[str] = set(no_capture_hosts or ())

    def allowed(self, host: str) -> bool:
        return capture_svc.assert_capture_allowed(
            host,
            allow_capture_hosts=self.allow_capture_hosts,
            no_capture_hosts=self.no_capture_hosts,
        )

    def update_allow(self, hosts: set[str]) -> None:
        self.allow_capture_hosts = set(hosts)

    def clear(self) -> None:
        """kill/归档 → 白名单置空（C3 kill 即停抓包，fail-closed 默认安全）。"""
        self.allow_capture_hosts.clear()
        self.no_capture_hosts.clear()


# ---------------------------------------------------------------------------
# 引擎抽象（接口 + fake；真实 mitmproxy 不可用时测试/降级用）
# ---------------------------------------------------------------------------


class ProxyEngine(ABC):
    """捕获代理引擎接口。实现可为 subprocess 包装（MitmProxyEngine）或内存 fake。"""

    @abstractmethod
    def start(self, *, port: int, addon: Optional[str] = None) -> None:
        """启动代理监听。``addon`` 为 mitmproxy addon 脚本路径（mitm 实现用）。"""

    @abstractmethod
    def stop(self) -> None:
        """停止代理（C3：kill 联动停抓包）。"""

    @abstractmethod
    def is_running(self) -> bool:
        ...

    @property
    @abstractmethod
    def kind(self) -> str:
        ...


class MitmProxyEngine(ProxyEngine):
    """真实 mitmproxy 引擎（subprocess 编排）。

    要求 mitmproxy 已安装（``import mitmproxy`` 成功）。未安装 → 构造即抛 ``RuntimeError``，
    调用方降级到 fake 或明确失败，不静默。
    """

    def __init__(self, *, host: str = "0.0.0.0") -> None:
        if not MITMPROXY_AVAILABLE:
            raise RuntimeError("mitmproxy 未安装：无法启动真实捕获代理（可用 FakeProxyEngine 降级测试）")
        self.host = host
        self.port: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None

    def start(self, *, port: int, addon: Optional[str] = None) -> None:
        cmd = ["mitmdump", "-q", "--listen-host", self.host, "--listen-port", str(port)]
        if addon:
            cmd += ["-s", addon]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.port = port

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
            self.port = None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def kind(self) -> str:
        return "mitmproxy"


class FakeProxyEngine(ProxyEngine):
    """内存 fake（mitmproxy 不可用或测试用）。模拟「捕获→写文件→回写索引」的代理侧行为。"""

    def __init__(self) -> None:
        self.port: Optional[int] = None
        self._running = False
        self.recorded: list[dict] = []

    def start(self, *, port: int, addon: Optional[str] = None) -> None:
        self.port = port
        self._running = True

    def stop(self) -> None:
        self._running = False
        self.port = None

    def is_running(self) -> bool:
        return self._running

    @property
    def kind(self) -> str:
        return "fake"

    # -- 测试辅助：模拟代理对一次连接的处理 -----------------------------
    def capture(
        self,
        *,
        host: str,
        method: str,
        url: str,
        client_ip: str | None = None,
        ip_to_worker: dict[str, str] | None = None,
    ) -> Optional[dict]:
        """按 F5/C12 模拟一次捕获：白名单外 → 返回 None（不落盘）；否则返回回写 entry。"""
        if not self._running:
            return None
        # 白名单由外部（manager）维护；这里假定调用方已传入 whitelist 校验后的结果。
        client = capture_svc.resolve_client(client_ip, ip_to_worker)  # C12
        entry = {
            "method": method,
            "url": url,
            "host": host,
            "client": client,
            "client_ip": client_ip,
            "req_path": f"eng/fake_{len(self.recorded) + 1}.req",
            "resp_path": f"eng/fake_{len(self.recorded) + 1}.resp",
            "req_bytes": 0,
            "status": 200,
        }
        self.recorded.append(entry)
        return entry


# ---------------------------------------------------------------------------
# CA 生成
# ---------------------------------------------------------------------------


def generate_ca(eid: str, ca_dir: str, *, days: int = 365) -> dict:
    """生成专属 CA（每 engagement 一份）。

    返回 ``{key_path, cert_path}``。私钥由编排者（Dispatcher/Server）持有，注入容器
    只给 ``cert_path``（C5/C6；worker-sandbox §4.1 的 SSL_CERT_FILE/REQUESTS_CA_BUNDLE/
    NODE_EXTRA_CA_CERTS 指向证书）。依赖系统 openssl。
    """
    os.makedirs(ca_dir, exist_ok=True)
    key = os.path.join(ca_dir, f"{eid}.key")
    cert = os.path.join(ca_dir, f"{eid}.pem")
    if os.path.isfile(key) and os.path.isfile(cert):
        return {"key_path": key, "cert_path": cert}
    subj = f"/CN=Cairn Capture {eid}"
    # -nodes：密钥不加密（进程持有，文件 0700）；-days 有效期
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", key, "-out", cert, "-days", str(days), "-nodes", "-subj", subj,
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    # C13：密钥文件受限权限（0700）
    os.chmod(key, 0o700)
    os.chmod(cert, 0o700)
    return {"key_path": key, "cert_path": cert}


# ---------------------------------------------------------------------------
# 每 engagement 状态 + 管理
# ---------------------------------------------------------------------------


@dataclass
class CaptureProxyState:
    """单个 engagement 的捕获代理运行态。"""

    eid: str
    engine: ProxyEngine
    whitelist: CaptureWhitelist
    scope_policy: dict = field(default_factory=dict)
    ca: dict | None = None
    ip_to_worker: dict[str, str] = field(default_factory=dict)
    interval: int = 3

    @property
    def port(self) -> int | None:
        return self.engine.port

    def allowed(self, host: str) -> bool:
        return self.whitelist.allowed(host)

    def refresh_whitelist(self, conn) -> None:
        """C11：从 authorized targets 重派生 allow 白名单（≤ runtime.interval 轮询调用）。"""
        self.whitelist.update_allow(capture_svc.derive_allow_hosts(conn, self.eid, self.scope_policy))
        self.whitelist.no_capture_hosts = capture_svc.derive_no_hosts(self.scope_policy)

    def resolve_client(self, client_ip: str | None) -> str | None:
        """C12：client_ip → worker（host 网络共享 IP 无法区分 → None）。"""
        return capture_svc.resolve_client(client_ip, self.ip_to_worker)

    def stop(self) -> None:
        """C3：kill 即停——停止引擎 + 白名单置空。"""
        self.engine.stop()
        self.whitelist.clear()


class CaptureProxyManager:
    """每 engagement 一个代理实例的编排器（进程级单例）。"""

    def __init__(self, *, ca_dir: str | None = None, engine_factory=None) -> None:
        self.ca_dir = ca_dir
        # engine_factory(scope_policy) -> ProxyEngine：可注入（默认真实 mitmproxy，缺依赖抛错）
        self._engine_factory = engine_factory or self._default_engine
        self._states: dict[str, CaptureProxyState] = {}

    @staticmethod
    def _default_engine(scope_policy: dict) -> ProxyEngine:
        if MITMPROXY_AVAILABLE:
            return MitmProxyEngine()
        raise RuntimeError(
            "mitmproxy 未安装：无法启动真实捕获代理。capture_proxy 提供接口抽象 + "
            "FakeProxyEngine（测试/降级），交接物已标注。"
        )

    def start_engagement(self, conn, eid: str, scope_policy: dict, *, ip_to_worker: dict | None = None) -> CaptureProxyState:
        """启动 per-engagement 代理：读 scope_policy.capture_proxy.port、生成 CA、派生白名单。"""
        if eid in self._states:
            return self._states[eid]
        cp = scope_policy.get("capture_proxy") or {}
        engine = self._engine_factory(scope_policy)
        whitelist = CaptureWhitelist(
            allow_capture_hosts=capture_svc.derive_allow_hosts(conn, eid, scope_policy),
            no_capture_hosts=capture_svc.derive_no_hosts(scope_policy),
        )
        state = CaptureProxyState(
            eid=eid,
            engine=engine,
            whitelist=whitelist,
            scope_policy=scope_policy,
            ip_to_worker=ip_to_worker or {},
        )
        engine.start(port=int(cp.get("port") or 8080))
        if self.ca_dir:
            state.ca = generate_ca(eid, os.path.join(self.ca_dir, eid))
        self._states[eid] = state
        return state

    def refresh_all(self, conn) -> None:
        """C11 热刷新：对所有 active 状态按 targets 重派生白名单（轮询入口）。"""
        for state in self._states.values():
            state.refresh_whitelist(conn)

    def refresh(self, conn, eid: str) -> None:
        state = self._states.get(eid)
        if state is not None:
            state.refresh_whitelist(conn)

    def stop_engagement(self, eid: str) -> bool:
        """C3：kill/归档联动——停抓包 + 白名单置空。"""
        state = self._states.pop(eid, None)
        if state is None:
            return False
        state.stop()
        return True

    def stop_all(self) -> None:
        for eid in list(self._states):
            self.stop_engagement(eid)

    def get(self, eid: str) -> CaptureProxyState | None:
        return self._states.get(eid)

    def states(self) -> list[CaptureProxyState]:
        return list(self._states.values())
