"""Dispatcher 确定性重放引擎（Agent 30 · F4）—— worker='replay-engine'，不依赖 LLM。"""

from .engine import REPLAY_RESULTS, ReplayEngine

__all__ = ["ReplayEngine", "REPLAY_RESULTS"]
