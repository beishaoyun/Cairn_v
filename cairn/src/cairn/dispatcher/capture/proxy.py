"""Dispatcher 侧捕获代理编排（Agent 23 · dispatcher/capture/proxy.py）。

职责（capture-verify-progress-spec §2/§9；worker-sandbox-hardening §4.1；F5/C3/C12/C13）：

- 每 engagement 启动一个 mitmproxy 实例，监听 ``scope_policy.capture_proxy.port``
  （默认 8080），并挂载 ``dispatcher/capture/addon.py``（F8 回写索引）；
- 专属 CA：每 engagement 一份，证书写到 ``{ca_dir}/{eid}/ca.pem``、私钥
  ``{ca_dir}/{eid}/ca.key``（0700）。容器侧按 ``ContainerBackend._ca_path``
  （``{capture_ca_dir}/{eid}/ca.pem``）bind-mount 到 ``/etc/cairn-capture/ca.pem``；
- 引擎环境注入 addon 所需参数（``CAIRN_TRAFFIC_ROOT``/``CAIRN_EID``/``CAIRN_SERVER_URL``/
  ``CAIRN_CAPTURE_TOKEN``/``CAIRN_ALLOW_HOSTS``/``CAIRN_NO_HOSTS``）；
- C3：kill/归档联动 ``stop_engagement`` 停止进程；``stop_all`` 供 Dispatcher 关停。

**独立性约束**：本模块不 import ``cairn.server``（Dispatcher 不反向依赖 Server）。
真实引擎经 subprocess 拉起 ``mitmdump``；mitmproxy 缺失时 ``MitmProxyEngine.start``
抛 ``FileNotFoundError``，由调用方（调度循环）捕获并记录，不崩循环。测试注入
``engine_factory`` 用 ``FakeProxyEngine``。
"""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

#: 捕获默认豁免主机（与 server/services/capture.DEFAULT_NO_CAPTURE_HOSTS 口径一致）
DEFAULT_NO_CAPTURE_HOSTS = ("api.anthropic.com", "api.deepseek.com", "cairn-server")


# ---------------------------------------------------------------------------
# 引擎抽象（接口 + subprocess 实现 + 测试 fake）
# ---------------------------------------------------------------------------


class ProxyEngine(ABC):
    """捕获代理引擎接口。实现为 subprocess 包装（``MitmProxyEngine``）或内存 fake。"""

    @abstractmethod
    def start(self, *, port: int, addon: Optional[str] = None, env: Optional[dict] = None) -> None:
        """启动代理监听。``addon`` 为 mitmproxy addon 脚本路径；``env`` 注入 addon 参数。"""

    @abstractmethod
    def stop(self) -> None:
        """停止代理（C3：kill 即停抓包）。"""

    @abstractmethod
    def is_running(self) -> bool:
        ...

    @property
    @abstractmethod
    def kind(self) -> str:
        ...


class MitmProxyEngine(ProxyEngine):
    """真实 mitmproxy 引擎（subprocess 编排 ``mitmdump -q``）。

    要求 ``mitmdump`` 在 PATH 上。未安装 → ``start`` 抛 ``FileNotFoundError``
    （由调用方捕获并记录，不静默）。加 ``-s addon`` 挂载 F8 回写 addon。
    """

    def __init__(self, *, host: str = "0.0.0.0") -> None:
        self.host = host
        self.port: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None

    def start(self, *, port: int, addon: Optional[str] = None, env: Optional[dict] = None) -> None:
        cmd = ["mitmdump", "-q", "--listen-host", self.host, "--listen-port", str(port)]
        if addon:
            cmd += ["-s", addon]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(os.environ, **(env or {})),
        )
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
    """内存 fake（测试/降级）。记录 start/stop 与注入参数，不拉起真实进程。"""

    def __init__(self) -> None:
        self.port: Optional[int] = None
        self._running = False
        self.start_calls: list[dict] = []
        self.stop_calls = 0
        self.last_addon: Optional[str] = None
        self.last_env: Optional[dict] = None

    def start(self, *, port: int, addon: Optional[str] = None, env: Optional[dict] = None) -> None:
        self.port = port
        self._running = True
        self.last_addon = addon
        self.last_env = env
        self.start_calls.append({"port": port, "addon": addon, "env": env})

    def stop(self) -> None:
        self._running = False
        self.port = None
        self.stop_calls += 1

    def is_running(self) -> bool:
        return self._running

    @property
    def kind(self) -> str:
        return "fake"


# ---------------------------------------------------------------------------
# CA 生成（C13：私钥 at-rest 0700）
# ---------------------------------------------------------------------------


def generate_ca(eid: str, ca_dir: str, *, days: int = 365) -> dict:
    """生成专属 CA（每 engagement 一份）。

    返回 ``{key_path, cert_path}``，文件名 ``ca.key``/``ca.pem``（对齐
    ``ContainerBackend._ca_path`` 的 ``{capture_ca_dir}/{eid}/ca.pem`` 挂载约定）。
    私钥由 Dispatcher 持有（0700），注入容器只给证书（C5/C6）。依赖系统 openssl。
    """
    os.makedirs(ca_dir, exist_ok=True)
    key = os.path.join(ca_dir, "ca.key")
    cert = os.path.join(ca_dir, "ca.pem")
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
    """单个 engagement 的捕获代理运行态（Dispatcher 侧）。"""

    eid: str
    engine: ProxyEngine
    scope_policy: dict = field(default_factory=dict)
    ca: dict | None = None
    port: int | None = None

    @property
    def is_running(self) -> bool:
        return self.engine.is_running()


def _default_addon_path() -> str:
    """addon 脚本绝对路径（与本模块同目录 ``addon.py``）。"""
    return str(Path(__file__).resolve().with_name("addon.py"))


class CaptureProxyManager:
    """每 engagement 一个代理实例的编排器（进程级单例，Dispatcher 侧）。

    与 server/capture_proxy.py 的职责区分：Server 侧管「白名单/归属/CA 数据库」，
    本模块管「mitmproxy 进程 + addon 环境 + CA 文件落盘」。接口契约参照 Server 侧
    （``start_engagement``/``stop_engagement``/``is_running``/``running_ports``）。
    """

    def __init__(
        self,
        *,
        ca_dir: str | None = None,
        engine_factory: Any | None = None,
        addon_path: str | None = None,
    ) -> None:
        self.ca_dir = ca_dir
        # engine_factory() -> ProxyEngine：可注入（默认真实 MitmProxyEngine；缺依赖 start 抛错）
        self._engine_factory = engine_factory or self._default_engine
        self._addon_path = addon_path or _default_addon_path()
        self._states: dict[str, CaptureProxyState] = {}

    @staticmethod
    def _default_engine() -> ProxyEngine:
        return MitmProxyEngine()

    def start_engagement(
        self,
        eid: str,
        scope_policy: dict,
        *,
        server_url: str,
        capture_token: str,
        traffic_root: str,
        allow_hosts: list[str] | set[str] | tuple[str, ...] | None = None,
        no_hosts: list[str] | set[str] | tuple[str, ...] | None = None,
    ) -> CaptureProxyState | None:
        """启动 per-engagement 代理（幂等：已运行直接返回）。

        - 读 ``scope_policy.capture_proxy.{enabled,port,no_capture_hosts}``；未启用 → 返回 None；
        - 生成 CA（``{ca_dir}/{eid}/ca.pem`` + ``ca.key`` 0700）；
        - ``mitmdump -q --listen-host 0.0.0.0 --listen-port {port} -s addon.py``，
          环境注入 addon 所需参数（F8 回写用）。
        """
        if eid in self._states and self._states[eid].is_running:
            return self._states[eid]
        cp = scope_policy.get("capture_proxy") or {}
        if not cp.get("enabled"):
            return None
        port = int(cp.get("port") or 8080)
        ca: dict | None = None
        if self.ca_dir:
            ca = generate_ca(eid, os.path.join(self.ca_dir, eid))
        no = no_hosts
        if not no:
            no = list(cp.get("no_capture_hosts") or DEFAULT_NO_CAPTURE_HOSTS)
        addon_env = {
            "CAIRN_TRAFFIC_ROOT": str(traffic_root),
            "CAIRN_EID": str(eid),
            "CAIRN_SERVER_URL": (server_url or "").rstrip("/"),
            "CAIRN_CAPTURE_TOKEN": str(capture_token or ""),
            "CAIRN_ALLOW_HOSTS": ",".join(str(h) for h in (allow_hosts or ())),
            "CAIRN_NO_HOSTS": ",".join(str(h) for h in no),
        }
        engine = self._engine_factory()
        engine.start(port=port, addon=self._addon_path, env=addon_env)
        state = CaptureProxyState(
            eid=eid, engine=engine, scope_policy=scope_policy, ca=ca, port=port
        )
        self._states[eid] = state
        return state

    def stop_engagement(self, eid: str) -> bool:
        """C3：kill/归档联动——停止代理进程。"""
        state = self._states.pop(eid, None)
        if state is None:
            return False
        state.engine.stop()
        return True

    def stop_all(self) -> None:
        for eid in list(self._states):
            self.stop_engagement(eid)

    def is_running(self, eid: str) -> bool:
        state = self._states.get(eid)
        return state is not None and state.engine.is_running()

    def get(self, eid: str) -> CaptureProxyState | None:
        return self._states.get(eid)

    def states(self) -> list[CaptureProxyState]:
        return list(self._states.values())

    def running_ports(self) -> dict[str, int | None]:
        """运行中 eid → 监听端口（跨 engagement 唯一性检查/运维看板用）。"""
        return {
            eid: state.port for eid, state in self._states.items() if state.engine.is_running()
        }

    def running_eids(self) -> list[str]:
        return [eid for eid, state in self._states.items() if state.engine.is_running()]


__all__ = [
    "CaptureProxyManager",
    "CaptureProxyState",
    "ProxyEngine",
    "MitmProxyEngine",
    "FakeProxyEngine",
    "generate_ca",
    "DEFAULT_NO_CAPTURE_HOSTS",
]
