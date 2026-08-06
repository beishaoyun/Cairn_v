"""CLI 结构化流解析 + 自由文本兜底分类（Agent 30 · F9）+ task_events 摘要上报。

契约（capture-verify-progress-spec §7.2）：
- 首选 CLI 结构化输出（``--output-format stream-json`` 等）→ 事件天然结构化
  （step/tool/command/output 直接映射）；
- 兜底自由文本**严格模式**分类：``$ `` 命令前缀 / 工具调用行 / Dispatcher 注入 ``⚑``
  前缀 / stderr 流 / traceback → 对应 kind；其余非空行一律 ``output``；
- **F9 防噪声**：stdout 里含 "error"/"failed" 字样**不算 error**（scanner 输出常含）；
  仅 stderr 流或严格错误签名（traceback / command not found / exit≠0 标记）才产生 error 事件；
- 摘要 ≤512B 落 ``append_event``；原始流分片写文件（由 40 的 drain 线程接线，本模块
  提供分片文件名生成器）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

#: task_events.kind 白名单（DDL CHECK；capture spec §7.1）
EVENT_KINDS = ("step", "tool", "command", "output", "status", "error")
#: task_events.level 白名单
EVENT_LEVELS = ("debug", "info", "warn", "error")

#: 结构化流行的 type → (kind, level)
_STRUCT_TYPE_MAP = {
    "step": ("step", "info"),
    "tool": ("tool", "info"),
    "tool_use": ("tool", "info"),
    "command": ("command", "info"),
    "output": ("output", "info"),
    "status": ("status", "info"),
    "error": ("error", "error"),
    "result": ("output", "info"),
    "assistant": ("output", "info"),
    "session": ("status", "info"),
    "init": ("status", "info"),
}

#: 严格错误签名（stdout 里出现即 error；即使不含 "error" 字样）
_ERROR_SIGNATURES = (
    "Traceback (most recent call last)",
    "command not found",
    "No such file or directory",
    "Permission denied",
    "exit code: 1",
    "exit code 1",
    "segmentation fault",
)

_STRUCTURED_LINE_RE = re.compile(r'^\s*\{.*"type"\s*:\s*"[^"]+"', re.DOTALL)
_TOOL_CALL_RE = re.compile(r"<(tool|invoke|use)[\s_>]|tool_use|tool_use_start|\"name\"\s*:\s*\"[^\"]*\",\s*\"input\"", re.IGNORECASE)
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")


def _truncate(message: str, *, max_bytes: int = 512) -> str:
    if message is None:
        return ""
    data = message.encode("utf-8", "replace")
    if len(data) <= max_bytes:
        return message
    marker = "…".encode("utf-8")
    keep = max(0, max_bytes - len(marker))
    return data[:keep].decode("utf-8", "replace") + "…"


def classify_line(line: str, *, stream: str = "stdout") -> tuple[str, str]:
    """分类单行 → ``(kind, level)``（F9 结构化优先 + 严格兜底）。

    ``stream`` ∈ stdout | stderr。stderr 流整体视为 error（除非空行）；
    stdout 里含 "error"/"failed" 字样**不算 error**（仅严格签名触发）。
    """
    line = (line or "").rstrip("\n")
    if not line.strip():
        return ("output", "info")
    if stream == "stderr":
        return ("error", "error")
    stripped = line.lstrip()
    # Dispatcher 注入前缀（`⚑ `）→ status
    if stripped.startswith("⚑ "):
        return ("status", "info")
    # 命令前缀 → command
    if stripped.startswith("$ "):
        return ("command", "info")
    # 结构化 JSON（stream-json / tool JSON 行）
    if _STRUCTURED_LINE_RE.match(line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get("type"), str):
            kind, level = _STRUCT_TYPE_MAP.get(obj["type"].lower(), ("output", "info"))
            # 结构化 error type 也可能只是扫描输出，仍按结构化映射
            return (kind, level)
    # 工具调用行 → tool
    if _TOOL_CALL_RE.search(line):
        return ("tool", "info")
    # 严格错误签名 → error
    if _TRACEBACK_RE.search(line) or any(sig in line for sig in _ERROR_SIGNATURES):
        return ("error", "error")
    # stdout 里的 "error"/"failed" 字样 → 不算 error（F9 防噪声）
    return ("output", "info")


def classify_stream(
    lines: Iterable[str],
    *,
    stream: str = "stdout",
) -> Iterator[tuple[str, str, str]]:
    """逐行分类，产出 ``(kind, level, message)`` 元组（过滤空行）。"""
    for ln in lines:
        if not ln or not ln.strip():
            continue
        kind, level = classify_line(ln, stream=stream)
        yield kind, level, ln.rstrip("\n")


def summarize_event(message: str, *, max_bytes: int = 512) -> str:
    """事件摘要（≤512B，tuning.event_summary_max_bytes；落 append_event.message）。"""
    return _truncate(message, max_bytes=max_bytes)


@dataclass
class EventStream:
    """事件流采集器：分片写原始文件 + 生成结构化事件（供 40 的 drain 线程接线）。

    - ``raw_path``：原始 stdout/stderr 分片文件（``logs/{task_run_id}/{seq}.chunk``）；
    - ``seq``：事件序号（append_event 由客户端携带，这里仅用于分片文件命名）。
    """

    task_run_id: str
    raw_dir: Optional[str] = None
    seq: int = 0

    def next_chunk_path(self, *, stream: str = "stdout") -> str:
        self.seq += 1
        if self.raw_dir:
            return f"{self.raw_dir.rstrip('/')}/{self.task_run_id}/{self.seq}.chunk"
        return f"logs/{self.task_run_id}/{self.seq}.chunk"

    def emit(self, kind: str, level: str, message: str) -> dict:
        """构造一条 append_event 入参（摘要 ≤512B；raw_path 可选）。"""
        if kind not in EVENT_KINDS:
            kind = "output"
        if level not in EVENT_LEVELS:
            level = "info"
        return {
            "kind": kind,
            "level": level,
            "message": summarize_event(message),
        }


def now_iso() -> str:
    """ISO8601 UTC（事件时间戳；黄金不变量 8）。"""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
