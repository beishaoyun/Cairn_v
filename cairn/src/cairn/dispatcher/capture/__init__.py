"""Dispatcher 捕获子域（Agent 23）。

- ``client.py``  白名单刷新拉取（C11）/ capture_gap 判定 / C12 归属辅助（纯逻辑）；
- ``proxy.py``   捕获代理编排（mitmdump 进程 + addon 环境 + 专属 CA 落盘 + kill 联动）；
- ``addon.py``   mitmdump addon（纯 stdlib 独立可加载）：fail-closed 白名单 +
  写回 traffic_root + F8 索引回写。
"""

from .client import (
    CaptureClient,
    CaptureWhitelist,
    assert_capture_allowed,
    derive_whitelist,
    reconcile_gap,
    resolve_client,
)
from .proxy import (
    CaptureProxyManager,
    CaptureProxyState,
    FakeProxyEngine,
    MitmProxyEngine,
    ProxyEngine,
    generate_ca,
)

__all__ = [
    "CaptureClient",
    "CaptureWhitelist",
    "CaptureProxyManager",
    "CaptureProxyState",
    "FakeProxyEngine",
    "MitmProxyEngine",
    "ProxyEngine",
    "assert_capture_allowed",
    "derive_whitelist",
    "generate_ca",
    "reconcile_gap",
    "resolve_client",
]
