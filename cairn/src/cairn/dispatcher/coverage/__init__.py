"""Dispatcher 覆盖写回（Agent 30）—— B1 格子互斥 / C9 幂等写回 / A5 复测 / 播种。"""

from .writer import CoverageWriter, SERVICE_TO_TEST_TYPES

__all__ = ["CoverageWriter", "SERVICE_TO_TEST_TYPES"]
