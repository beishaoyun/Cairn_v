"""HeartbeatLease（Agent 40 · runtime/heartbeat.py 保留改造）。

v1 §8.6 / v2 §8.6 的租约心跳语义保留：intent/reason 租约 + task_runs 心跳按
``runtime.interval`` 周期发送，保证 Dispatcher 宕机后 Server 侧可超时回收租约
（B1 释放语义：``coverage_items.current_intent_id`` 认领但 >2×interval 无心跳 →
reconcile 置 untested）。

实现为后台守护线程 + 租约注册表：
- ``register(key, beat)``：注册一个租约（``beat`` 为可调用，每次心跳调用一次，
  e.g. ``lambda: client.heartbeat_intent(pid, iid, worker=w)``）。
- ``unregister(key)``：任务完成/失败时注销（无租约时线程自动退出）。
- ``beat_once()``：同步打一轮心跳（测试 seam / 启动即时保活）。
- ``stop()``：shutdown 时停止线程。

心跳失败**只记日志不阻断**（租约超时回收由 Server 读时清理兜底）。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional


class HeartbeatLease:
    """多租约周期心跳器（线程安全）。"""

    def __init__(
        self,
        *,
        interval: float = 3.0,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.interval = float(interval)
        self.log = log or (lambda _m: None)
        self._lock = threading.Lock()
        self._leases: dict[str, Callable[[], None]] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- 租约控制 ----

    def register(self, key: str, beat: Callable[[], None]) -> None:
        """注册租约（幂等：同 key 覆盖 beat）。首次注册启动后台线程。"""
        with self._lock:
            self._leases[key] = beat
        self._ensure_thread()

    def unregister(self, key: str) -> None:
        """注销租约（幂等）。空租约时停止线程。"""
        with self._lock:
            self._leases.pop(key, None)
            if not self._leases:
                self._stop.set()

    def active(self) -> int:
        with self._lock:
            return len(self._leases)

    def clear(self) -> None:
        """清空全部租约并停止线程。"""
        with self._lock:
            self._leases.clear()
            self._stop.set()

    # ---- 心跳 ----

    def beat_once(self) -> None:
        """同步打一轮心跳（启动即时保活 / 测试 seam）。"""
        with self._lock:
            beats = list(self._leases.values())
        for b in beats:
            try:
                b()
            except Exception as exc:  # noqa: BLE001 —— 心跳失败不阻断任务
                self.log(f"heartbeat failed (ignored): {exc}")

    # ---- 后台线程 ----

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat-lease")
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.interval)
            if self._stop.is_set():
                return
            self.beat_once()

    def stop(self) -> None:
        """停止后台线程（不强制清租约）。"""
        self._stop.set()


__all__ = ["HeartbeatLease"]
