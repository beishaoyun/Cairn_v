"""Dispatcher findings 写回（Agent 30）—— 落库 + 去重 + 证据挂载（带重试）。"""

from .writer import FindingsWriter

__all__ = ["FindingsWriter"]
