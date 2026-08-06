"""Dispatcher ↔ Cairn Server 协议客户端。

客户端**不缓存**任何 Server 数据，每次调用走 HTTP（进度/心跳例外见 Agent 40）。
"""

from .client import CairnClient

__all__ = ["CairnClient"]
