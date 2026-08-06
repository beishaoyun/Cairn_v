"""Dispatcher 调度主循环（Agent 40 · scheduler/loop.py）。

契约（40 提示词 + v2 §8.2/§9.1 + 30/12/13 交接物）：

- **CLI 入口**：``run_dispatch_loop(ctx: DispatcherContext) -> int``（13 的 cli.py 懒导入）。
  额外关键字参数 ``interval/client/backend`` 供测试注入；生产走 ctx.config 默认值。
- **guards**：每轮派发前 ``_check_kill_switch``（C1 即时 SIGKILL 经 ctx.force_kill 通知 11）、
  ``_check_auth_window``（窗口外拒绝 + 到期 pause 调 20 expire_engagements，B5）、
  ``_check_scope_guard``（目标白名单，SCOPE_DENIED 禁 fallback）。
- **任务触发（调用 30 的纯逻辑任务函数）**：
  bootstrap（engagement 初始态一次）→ reason（gaps 驱动收敛，C8 升级计数落库）→
  explore（intent 认领后派发，B1）→ verify（open finding 排除创建者，F1/F7）→
  audit（抽样复核预留）。
- **worker 选择**：``worker_select.select_worker``（优先级/冷却/per-worker max_running），
  verify 用 ``select_verify_worker``；replay-engine 内建特例不走 worker 列表。
- **状态落库 + 启动 reconcile**：``scheduler_state`` 读写（worker_unhealthy_until /
  worker_rejected_until / reason_checkpoints / runtime_project_ids / reason_escalation:{eid}）；
  启动 reconcile 清僵尸 running task_runs + 超时 intent 认领（B1 释放）。
- **periodic（v2 §9.1）**：expire_engagements（20）/ 捕获对账（23 reconcile）/
  白名单热刷新（C11 服务端派生，本循环无需动作）/ task_events 原始流清理（服务端 cron）。
- **心跳/取消**：``HeartbeatLease``（runtime/heartbeat.py）+ 13 ``TaskCancellation``；
  kill switch 走即时 SIGKILL（C1）。

硬约束：**不直接连 DB** —— 一切读写经 12 的 ``CairnClient``。
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional

from ..config import DispatcherConfig
from ..coverage.writer import CoverageWriter
from ..errors import CairnClientError, ScopeDeniedError
from ..protocol.client import CairnClient
from ..runtime.backend import ExecutionBackend
from ..runtime.cancellation import TaskCancellation
from ..runtime.context import DispatcherContext
from ..runtime.heartbeat import HeartbeatLease
from ..runtime.local_backend import LocalBackend
from ..tasks import (
    TaskContext,
    TaskResult,
    run_bootstrap,
    run_explore,
    run_reason,
    run_verify,
)
from ..tasks.reason import ReasonEscalation
from .worker_select import can_dispatch, select_verify_worker, select_worker

#: TaskResult.status → task_runs.status（golden 不变量 7；DDL CHECK 枚举）
_TASK_STATUS_MAP: dict[str, str] = {
    "success": "success",
    "failed": "failed",
    "cancelled": "cancelled",
    "rejected": "rejected",
    "retryable": "failed",
}

#: scheduler_state 键
KEY_UNHEALTHY = "worker_unhealthy_until"
KEY_REJECTED = "worker_rejected_until"
KEY_REASON_CHECKPOINTS = "reason_checkpoints"
KEY_RUNTIME_PROJECTS = "runtime_project_ids"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_scope_resolver(
    client: CairnClient,
    project_to_eid: dict[str, str],
) -> Callable[[str], Any]:
    """构造 ``scope_resolver(project_id) -> ContainerScope``（缺口 1 接线）。

    resolver 把 ``project_id`` 经 ``project_to_eid`` 反查所属 engagement，再经
    ``CairnClient.get_engagement_scope`` 拉取 ``scope_policy``（DDL §2.1），解析为
    容器后端可消费的 ``ContainerScope``（capture 代理注入 + CA 信任 + 网络能力 +
    资源限制）。查不到 → 返回空 ``ContainerScope()``（安全降级，容器用默认参数）。
    """
    from ..runtime.containers import ContainerScope, resolve_scope_policy

    def _resolver(project_id: str) -> ContainerScope:
        eid = project_to_eid.get(str(project_id))
        if not eid:
            return ContainerScope()
        try:
            scope_policy = client.get_engagement_scope(eid)
        except CairnClientError:
            return ContainerScope()
        return resolve_scope_policy(eid, scope_policy)

    return _resolver


def _default_backend(
    config: DispatcherConfig,
    *,
    client: Optional[CairnClient] = None,
    project_to_eid: Optional[dict[str, str]] = None,
) -> ExecutionBackend:
    """按 runtime.execution 构造后端（测试注入覆盖）。

    container 模式且给了 ``client``+``project_to_eid`` → 挂上 ``scope_resolver``
    （真实抓包接线：capture 时注入 HTTPS_PROXY/CA 信任 + 挂载专属 CA）。
    """
    if config.runtime.execution == "container":
        from ..runtime.containers import ContainerBackend

        if client is not None and project_to_eid is not None:
            return ContainerBackend(
                config, scope_resolver=_make_scope_resolver(client, project_to_eid)
            )
        return ContainerBackend(config)
    return LocalBackend(config)


def run_dispatch_loop(
    ctx: DispatcherContext,
    *,
    interval: Optional[float] = None,
    client: Optional[CairnClient] = None,
    backend: Optional[ExecutionBackend] = None,
) -> int:
    """CLI 入口（Agent 13 懒导入）：装配依赖并运行主循环。"""
    config: DispatcherConfig = ctx.config
    client = client or CairnClient(config.server.url, config.server.api_token)
    # ``project_to_eid`` 由 loop 在派发/建 project 时填充；_default_backend 用它构造
    # container 后端的 scope_resolver（缺口 1），loop 再经同一 dict 读 project→eid。
    project_to_eid: dict[str, str] = {}
    if backend is None:
        backend = _default_backend(config, client=client, project_to_eid=project_to_eid)
    loop = DispatcherLoop(
        ctx,
        client=client,
        backend=backend,
        interval=interval if interval is not None else float(config.runtime.interval),
        project_to_eid=project_to_eid,
    )
    try:
        return loop.run()
    finally:
        try:
            backend.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


class DispatcherLoop:
    """调度主循环：guards → 选任务 → 派发 → 状态落库 → 心跳 → 孤儿清理。

    可测性：``interval`` 可注入小值；``client``/``backend`` 可注入（进程内 TestClient /
    LocalBackend / MockDriver）。``step()`` 单轮可被测试手动调用。
    """

    def __init__(
        self,
        ctx: DispatcherContext,
        *,
        client: CairnClient,
        backend: ExecutionBackend,
        interval: float = 3.0,
        escalation: Optional[ReasonEscalation] = None,
        project_to_eid: Optional[dict[str, str]] = None,
        capture_manager: Optional[Any] = None,
    ) -> None:
        self.ctx = ctx
        self.config: DispatcherConfig = ctx.config
        self.client = client
        self.backend = backend
        self.interval = float(interval)
        self.log = ctx.log
        self.shutdown = ctx.shutdown
        self.health = ctx.health
        self.drivers: dict[str, Any] = ctx.drivers

        # ---- 运行时状态（scheduler_state 回载）----
        self._rejected_until: dict[str, float] = {}
        self._bootstrap_done: set[str] = set()
        self._escalation: ReasonEscalation = escalation or ReasonEscalation()
        self._pending_intents: dict[str, list[dict]] = {}
        self._project_id: dict[str, str] = {}
        #: project_id → engagement_id 反向映射（container scope_resolver 用；派发/建 project 时填充）
        self._project_to_eid: dict[str, str] = project_to_eid if project_to_eid is not None else {}
        self._runtime_projects: set[str] = set()
        self._reason_blocked_until: dict[str, float] = {}

        # ---- 真实抓包：Dispatcher 侧捕获代理编排（缺口 2 接线）----
        if capture_manager is not None:
            self._capture_manager = capture_manager
        else:
            from ..capture.proxy import CaptureProxyManager

            self._capture_manager = CaptureProxyManager(
                ca_dir=self.config.security.capture_ca_dir
            )

        # ---- 并发账本 ----
        self._running: dict[str, dict] = {}  # run_id -> rec
        self._worker_running: dict[str, int] = {}
        self._eid_running: dict[str, int] = {}

        # ---- 心跳 ----
        self._heartbeat = HeartbeatLease(interval=self.interval, log=self.log)

        # ---- periodic 计时（interval 可注入小值；以轮次驱动）----
        self._round = 0
        self._periodic_every = max(1, int(60.0 / max(self.interval, 0.01)))  # ~60s 一次

    # ==================================================================
    # 主入口
    # ==================================================================

    def run(self) -> int:
        self._load_state()
        self._startup_reconcile()
        self._start_kill_monitor()
        try:
            while not self.shutdown.is_set():
                self.step()
                self.shutdown.wait(self.interval)
        finally:
            self._stop_kill_monitor()
            self._heartbeat.stop()
            # C3：Dispatcher 关停 → 停全部捕获代理（清理 mitmdump 进程）
            if self._capture_manager is not None:
                try:
                    self._capture_manager.stop_all()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"capture proxy stop_all 失败: {exc!r}")
        self.log("dispatch loop: shutdown")
        return 0

    def step(self) -> None:
        """一轮调度：periodic → 每个 active engagement 派发一个任务 → 状态落库。"""
        self._round += 1
        if self._round % self._periodic_every == 0 or self._round == 1:
            self._run_periodic()
        for eng in self._list_active():
            if self.shutdown.is_set():
                break
            try:
                self._process_engagement(eng)
            except CairnClientError as exc:
                self.log(f"step: engagement {eng.get('id')} 处理失败（忽略）: {exc}")
            except Exception as exc:  # noqa: BLE001 —— 单 engagement 故障不崩循环
                self.log(f"step: engagement {eng.get('id')} 异常（忽略）: {exc!r}")
        self._persist_state()

    # ==================================================================
    # guards
    # ==================================================================

    def _check_kill_switch(self, eng: Mapping[str, Any]) -> bool:
        """全局 + 项目级熔断（423 KILL_SWITCH_ON；C1）。返回 True=熔断应停止该 engagement。"""
        if not self.config.scope.enforce_kill_switch:
            return False
        return bool(eng.get("kill_switch"))

    def _handle_kill(self, eid: str) -> None:
        """熔断处理：取消在飞任务 + ctx.force_kill（C1 即时 SIGKILL）+ 停容器。"""
        self.log(f"kill switch ON: {eid} —— 取消在飞 + SIGKILL")
        for run_id, rec in list(self._running.items()):
            if rec.get("eid") == eid:
                cancellation = rec.get("cancellation")
                if cancellation is not None:
                    cancellation.kill_switch(f"kill_switch:{eid}")
        # C1：即时 SIGKILL 通知 11（13 CLI 已把 ctx.force_kill 接上 SIGKILL）
        try:
            self.ctx.force_kill(f"kill_switch:{eid}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"force_kill 失败: {exc!r}")
        # C3：kill 联动停抓包（熔断即停代理进程 + 释放端口）
        if self._capture_manager is not None:
            try:
                self._capture_manager.stop_engagement(eid)
            except Exception as exc:  # noqa: BLE001
                self.log(f"stop capture proxy({eid}) 失败: {exc!r}")
        for pid in self._project_ids_for(eid):
            try:
                self.backend.cleanup_managed_container(pid, reason="kill_switch")
            except Exception as exc:  # noqa: BLE001
                self.log(f"cleanup_managed_container({pid}) 失败: {exc!r}")

    #: kill 监控轮询间隔（C1 熔断即时性）。独立于主循环 interval——任务运行期间
    #: 主循环阻塞在 ``communicate``，本线程以该间隔轮询 kill_switch，保证触发即
    #: SIGKILL（不等任务返回）。
    _KILL_MONITOR_POLL = 0.2

    def _start_kill_monitor(self) -> None:
        """后台线程：任务运行期间监控 kill_switch，触发即 SIGKILL（C1）。

        主循环同步执行任务（``communicate`` 阻塞），kill switch 置位要等任务返回才被
        主循环观察到。本线程在运行任务期间轮询 ``list_active()`` 的 kill_switch，一旦
        发现运行中任务所属 engagement 熔断 → 立即 ``cancellation.kill_switch()``
        （即时 SIGKILL 绑定进程，C1 不走 grace），不等 communicate 返回。
        30 的 ``run_worker_phase`` 已把进程 attach 到 ``TaskCancellation``（13 §7），
        这里补上「kill 触发 → cancel()」的触发链路。
        """
        if getattr(self, "_kill_monitor", None) is not None:
            return

        def _monitor() -> None:
            while not self.shutdown.is_set():
                if self._running:
                    kill_eids = self._kill_switch_eids()
                    if kill_eids:
                        for _run_id, rec in list(self._running.items()):
                            if rec.get("eid") in kill_eids:
                                cancellation = rec.get("cancellation")
                                if cancellation is not None:
                                    cancellation.kill_switch(f"kill_switch:{rec['eid']}")
                self.shutdown.wait(self._KILL_MONITOR_POLL)

        t = threading.Thread(target=_monitor, daemon=True, name="cairn-kill-monitor")
        self._kill_monitor = t
        t.start()

    def _stop_kill_monitor(self) -> None:
        """停 kill 监控线程（join 短暂，守护线程；run() finally 调用）。"""
        t = getattr(self, "_kill_monitor", None)
        self._kill_monitor = None
        if t is not None:
            t.join(timeout=1.0)

    def _kill_switch_eids(self) -> set[str]:
        """当前 kill_switch 已置位的 active engagement id 集合（C1 熔断）。"""
        try:
            active = self._list_active() or []
        except CairnClientError as exc:
            self.log(f"kill monitor list_active failed: {exc}")
            return set()
        return {str(e.get("id")) for e in active if e.get("kill_switch")}

    def _check_auth_window(self, eng: Mapping[str, Any]) -> bool:
        """授权窗口守卫：窗口外拒绝派发（到期 pause 由 periodic expire_engagements 落实，B5）。"""
        if not self.config.scope.enforce_auth_window:
            return True
        end = eng.get("authorized_end_at")
        if not end:
            return True
        try:
            end_ts = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            return datetime.now(timezone.utc) <= end_ts
        except ValueError:
            return True  # 解析失败不阻断（服务端 expire 兜底）

    def _check_scope_guard(self, eid: str, value: str) -> bool:
        """目标白名单守卫：SCOPE_DENIED → 禁 fallback（返回 False）。"""
        if not self.config.scope.enforce_scope_guard:
            return True
        try:
            self.client.check_scope(eid, value)
            return True
        except ScopeDeniedError:
            return False
        except CairnClientError as exc:
            self.log(f"scope/check 失败（视为拒入）: {exc}")
            return False

    # ==================================================================
    # 任务触发（每 engagement 每轮一个任务；调用 30 纯逻辑）
    # ==================================================================

    def _process_engagement(self, eng: Mapping[str, Any]) -> Optional[TaskResult]:
        eid = str(eng.get("id") or "")
        if not eid:
            return None
        if self._check_kill_switch(eng):
            self._handle_kill(eid)
            return None
        if not self._check_auth_window(eng):
            return None
        # 真实抓包：active engagement 首次派发前启动 per-engagement 捕获代理（幂等）
        self._ensure_capture(eng)
        for fn in (
            self._maybe_bootstrap,
            self._maybe_reason,
            self._maybe_explore,
            self._maybe_verify,
            self._maybe_audit,
        ):
            result = fn(eng)
            if result is not None:
                return result
        return None

    # ==================================================================
    # 真实抓包接线（缺口 2：Dispatcher 侧 CaptureProxyManager 生命周期）
    # ==================================================================

    def _authorized_targets(self, eid: str) -> list[dict]:
        """拉 targets（C11：allow 白名单由 authorized targets 派生）。"""
        try:
            return self.client.list_targets(eid) or []
        except CairnClientError as exc:
            self.log(f"list_targets({eid}) 失败: {exc}")
            return []

    def _ensure_capture(self, eng: Mapping[str, Any]) -> None:
        """active engagement 首次派发前启动捕获代理（幂等）。

        读 ``scope_policy.capture_proxy.enabled``（DDL §2.1）；启用且未运行 →
        ``start_engagement``（生成 CA + 拉起 mitmdump + addon 环境）。targets 派生
        白名单（fail-closed）；白名单热刷新由服务端 ``server_assert_capture_allowed``
        兜底（F5/C11），代理本地白名单重启即更新。
        """
        if self._capture_manager is None:
            return
        eid = str(eng.get("id") or "")
        if not eid:
            return
        if self._capture_manager.is_running(eid):
            return
        try:
            scope_policy = self.client.get_engagement_scope(eid)
        except CairnClientError as exc:
            self.log(f"get_engagement_scope({eid}) 失败: {exc}")
            return
        cp = scope_policy.get("capture_proxy") or {}
        if not cp.get("enabled"):
            return
        try:
            from ..capture.client import derive_whitelist

            wl = derive_whitelist(self._authorized_targets(eid), scope_policy)
            self._capture_manager.start_engagement(
                eid,
                scope_policy,
                server_url=self.config.server.url,
                capture_token=os.environ.get(self.config.security.capture_token_env) or "",
                traffic_root=self.config.security.traffic_root,
                allow_hosts=sorted(wl.allow_capture_hosts),
                no_hosts=sorted(wl.no_capture_hosts),
            )
        except Exception as exc:  # noqa: BLE001 —— 代理启动失败不崩循环（缺 mitmdump 等）
            self.log(f"start capture proxy({eid}) 失败: {exc!r}")

    def _reconcile_capture_proxies(self) -> None:
        """periodic 对账：对 active 集合增量 start/stop（C3 kill/过期/归档联动）。

        start 已在 ``_process_engagement`` 逐 engagement 幂等触发；这里只处理
        「不再 active」的代理 → stop（kill/expire/archive 离开 active 集合）。
        """
        if self._capture_manager is None:
            return
        active = {str(e.get("id")) for e in self._list_active()}
        for eid in self._capture_manager.running_eids():
            if eid not in active:
                try:
                    self._capture_manager.stop_engagement(eid)
                    self.log(f"capture proxy 停止（engagement 不再 active）: {eid}")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"stop capture proxy({eid}) 失败: {exc!r}")

    def _maybe_bootstrap(self, eng: Mapping[str, Any]) -> Optional[TaskResult]:
        eid = str(eng["id"])
        if eid in self._bootstrap_done:
            return None
        if not self._can_dispatch(eid):
            return None
        pid = self._ensure_project(eid)
        if pid is None:
            return None
        worker = select_worker(
            self.config.workers, task_type="bootstrap", health=self.health,
            rejected_until=self._rejected_until, running_counts=self._worker_running,
        )
        if worker is None:
            return None
        driver = self.drivers.get(worker)
        if driver is None:
            return None
        result = self._run_task(
            eid, "bootstrap", worker, pid,
            lambda tctx: run_bootstrap(
                tctx, driver=driver, backend=self.backend,
                origin=f"engagement {eid} initial recon",
                goal="comprehensive authorized coverage of the authorized scope",
                hints=None, scope=self._scope_text(eid), task_cfg=self.config.tasks.bootstrap,
            ),
        )
        if result.ok:
            self._bootstrap_done.add(eid)
        return result

    def _maybe_reason(self, eng: Mapping[str, Any]) -> Optional[TaskResult]:
        eid = str(eng["id"])
        if eid not in self._bootstrap_done:
            return None
        if self._pending_intents.get(eid):  # 待 explore 消化，先不发 reason
            return None
        if not self._can_dispatch(eid):
            return None
        # C8：升级 needs_review 后停止自动重试（仅人工恢复）
        state = self._escalation.snapshot(eid)
        if state and state.get("escalated"):
            return None
        # reason 失败退避（避免 hot-loop：静态/反复失败的 reason 会饿死 verify）
        if self._reason_blocked_until.get(eid, 0.0) > time.time():
            return None
        gaps = self._get_gaps(eid)
        if not gaps:
            return None
        pid = self._project_id.get(eid)
        if pid is None:
            return None
        worker = select_worker(
            self.config.workers, task_type="reason", health=self.health,
            rejected_until=self._rejected_until, running_counts=self._worker_running,
        )
        if worker is None:
            return None
        driver = self.drivers.get(worker)
        if driver is None:
            return None
        graph_yaml = self._safe_export(pid)
        result = self._run_task(
            eid, "reason", worker, pid,
            lambda tctx: run_reason(
                tctx, driver=driver, backend=self.backend, gaps=gaps,
                graph_yaml=graph_yaml, scope=self._scope_text(eid),
                task_cfg=self.config.tasks.reason,
            ),
        )
        if result.ok:
            data = result.data or {}
            intents = data.get("intents") or []
            if intents:
                self._persist_intents(eid, pid, intents, worker)
            if (data.get("coverage") or {}).get("recommend_finalize"):
                self.log(f"reason: engagement {eid} 建议 finalize（人工批准，B4/C8）")
            self._escalation.reset(eid)
            self._reason_blocked_until.pop(eid, None)
        else:
            # C8 升级信号；非升级失败也退避，避免饿死后续任务（verify/explore）
            if result.escalate:
                self._escalation.record_failure(eid)
                self.log(f"reason: engagement {eid} 连续失败升级计数（C8）")
            backoff = max(self.interval * 20, 2.0)
            self._reason_blocked_until[eid] = time.time() + backoff
        return result

    def _maybe_explore(self, eng: Mapping[str, Any]) -> Optional[TaskResult]:
        eid = str(eng["id"])
        pending = self._pending_intents.get(eid) or []
        if not pending:
            return None
        if not self._can_dispatch(eid):
            return None
        intent = pending[0]
        iid = str(intent.get("id") or "")
        item_ids = list(intent.get("coverage_item_ids") or [])
        pid = self._project_id.get(eid)
        if not iid or not item_ids or pid is None:
            self._pending_intents[eid] = pending[1:]
            return None
        # B1 认领覆盖项（格子互斥）
        writer = CoverageWriter(self.client, log=self.log)
        claimed, busy = writer.claim_all(eid, item_ids, iid)
        if busy or not claimed:
            for c in claimed:
                writer.release_item(eid, c, iid)
            self.log(f"explore: intent {iid} 格子忙（B1），下轮换格")
            return None
        worker = select_worker(
            self.config.workers, task_type="explore", health=self.health,
            rejected_until=self._rejected_until, running_counts=self._worker_running,
        )
        if worker is None:
            for c in claimed:
                writer.release_item(eid, c, iid)
            return None
        driver = self.drivers.get(worker)
        if driver is None:
            for c in claimed:
                writer.release_item(eid, c, iid)
            return None
        graph_yaml = self._safe_export(pid)
        result = self._run_task(
            eid, "explore", worker, pid,
            lambda tctx: run_explore(
                tctx, driver=driver, backend=self.backend, intent=intent,
                graph_yaml=graph_yaml, scope=self._scope_text(eid),
                task_cfg=self.config.tasks.explore, claimed_item_ids=claimed,
            ),
        )
        # 无论结果如何，该 intent 出队（成功已写回；retryable 由任务内 release）
        self._pending_intents[eid] = pending[1:]
        return result

    def _maybe_verify(self, eng: Mapping[str, Any]) -> Optional[TaskResult]:
        eid = str(eng["id"])
        if not self._can_dispatch(eid):
            return None
        in_flight = {
            rec.get("finding_id")
            for rec in self._running.values()
            if rec.get("task_type") == "verify" and rec.get("finding_id")
        }
        for finding in self._list_open_findings(eid):
            fid = str(finding.get("id") or "")
            if fid in in_flight:
                continue
            # 详情（含 traffic_links/http_evidence，run_verify 消费）
            detail = self._finding_detail(eid, fid) or finding
            creator = str(detail.get("detected_by") or "")
            worker = select_verify_worker(
                creator, self.config.workers, health=self.health,
                rejected_until=self._rejected_until, running_counts=self._worker_running,
            )
            independence = "cross_worker"
            if worker is None:
                # F7 单 worker 兜底降级 cross_run —— **仅当存在「独立于创建者」的候选时**。
                # select_worker 显式排除创建者（F1）：唯一候选是创建者 → None → 不派发。
                worker = select_worker(
                    self.config.workers, task_type="verify", health=self.health,
                    rejected_until=self._rejected_until, running_counts=self._worker_running,
                    creator=creator,
                )
                if worker is None:
                    # 无独立复核候选：若确实不存在任何「非创建者 verify 候选」→ TV-10
                    # 不派发，finding 标 pending_verify 等待独立复核；若只是并发/健康
                    # 暂时不可用（存在独立 worker）→ 保持 open，下轮再试，避免卡死。
                    if not self._has_independent_verify_worker(creator):
                        self._mark_waiting_independent_verify(eid, fid, creator)
                    continue
                independence = "cross_run"
            driver = self.drivers.get(worker)
            if driver is None:
                continue
            self._mark_pending_verify(eid, fid, worker)
            result = self._run_task(
                eid, "verify", worker, None,
                lambda tctx: run_verify(
                    tctx, driver=driver, backend=self.backend, finding=detail, eid=eid,
                    scope=self._scope_text(eid), task_cfg=self.config.tasks.verify,
                    independence=independence,
                ),
                extra={"finding_id": fid},
            )
            return result
        return None

    def _has_independent_verify_worker(self, creator: str) -> bool:
        """是否存在「独立于创建者」的 verify 候选（F1）。

        与 fallback ``select_worker(creator=creator)`` 同口径：忽略健康/并发过滤，
        仅判断配置中是否存在 name≠creator 且声明 ``verify`` 的 worker。返回 False 表示
        唯一可 verify 的 worker 即创建者本人（TV-10：不派发，等待独立复核）。
        """
        for w in self.config.workers:
            if getattr(w, "name", "") == creator:
                continue
            if "verify" in (getattr(w, "task_types", None) or ()):
                return True
        return False

    def _mark_waiting_independent_verify(self, eid: str, fid: str, creator: str) -> None:
        """TV-10：无独立复核候选 → finding 标 ``pending_verify`` 等待独立复核（F1）。

        与 ``_mark_pending_verify`` 不同：不派发任务，仅置状态 + 审计 note。
        ``open → pending_verify`` 为机器可流转边（capture spec §5）。
        """
        try:
            self.client._request(
                "PUT", f"/engagements/{eid}/findings/{fid}",
                json={"status": "pending_verify", "note": "等待独立复核", "actor": creator},
            )
        except CairnClientError as exc:
            self.log(f"mark waiting independent verify failed: {exc}")

    def _maybe_audit(self, eng: Mapping[str, Any]) -> Optional[TaskResult]:
        """覆盖抽样复核（F3）。

        ``sample_audit`` 选样是 21 服务端逻辑；本循环暂不主动抽样（需服务端 expose
        pending audit_run 列表端点，阶段 2 联调对齐）。返回 None 表示本轮不派发。
        """
        return None

    # ==================================================================
    # 并发 / worker 账本
    # ==================================================================

    def _can_dispatch(self, eid: str) -> bool:
        return can_dispatch(
            running_projects=len({r["pid"] for r in self._running.values() if r.get("pid")}),
            max_running_projects=self.config.runtime.max_running_projects,
            running_tasks=len(self._running),
            max_workers=self.config.runtime.max_workers,
            eid_running=self._eid_running.get(eid, 0),
            max_project_workers=self.config.runtime.max_project_workers,
        )

    def _run_task(
        self,
        eid: str,
        task_type: str,
        worker: str,
        project_id: Optional[str],
        run_fn: Callable[[TaskContext], TaskResult],
        *,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> TaskResult:
        """打开 task_run → 执行任务函数 → 收尾；维护并发账本。"""
        # 执行上下文就绪（LocalBackend 建工作区 / ContainerBackend 起容器）
        if project_id is not None:
            try:
                self.backend.ensure_running(project_id)
            except Exception as exc:  # noqa: BLE001
                self.log(f"ensure_running({project_id}) 失败: {exc!r}")
        run = self.client.open_task_run(
            eid, task_type=task_type, worker=worker, project_id=project_id
        )
        run_id = str(run.get("id") or "")
        cancellation = TaskCancellation()
        tctx = TaskContext(
            client=self.client, config=self.config, cancellation=cancellation,
            worker=worker, eid=eid, project_id=project_id, run_id=run_id, log=self.log,
        )
        self._worker_running[worker] = self._worker_running.get(worker, 0) + 1
        self._eid_running[eid] = self._eid_running.get(eid, 0) + 1
        rec: dict[str, Any] = {
            "run_id": run_id, "eid": eid, "pid": project_id, "task_type": task_type,
            "worker": worker, "cancellation": cancellation,
        }
        if extra:
            rec.update(extra)
        self._running[run_id] = rec
        try:
            result = run_fn(tctx)
        except Exception as exc:  # noqa: BLE001 —— 任务函数崩溃不崩循环
            self.log(f"task {task_type}({run_id}) crashed: {exc!r}")
            result = TaskResult(status="failed", error=str(exc), error_code="INTERNAL")
        finally:
            self._worker_running[worker] = max(0, self._worker_running.get(worker, 0) - 1)
            self._eid_running[eid] = max(0, self._eid_running.get(eid, 0) - 1)
            self._running.pop(run_id, None)
        status = _TASK_STATUS_MAP.get(result.status, "failed")
        try:
            self.client.finish_task_run(run_id, status, outcome_note=result.outcome_note)
        except CairnClientError as exc:
            self.log(f"finish_task_run({run_id}) 失败: {exc}")
        self.log(f"task {task_type} worker={worker} -> {status}")
        return result

    # ==================================================================
    # 项目 / intent 持久化
    # ==================================================================

    def _ensure_project(self, eid: str) -> Optional[str]:
        if eid in self._project_id:
            return self._project_id[eid]
        try:
            projects = self.client._request("GET", "/projects", params={"engagement_id": eid}) or []
        except CairnClientError as exc:
            self.log(f"list_projects failed: {exc}")
            projects = []
        if projects:
            pid = str(projects[0]["id"])
        else:
            try:
                proj = self.client._request(
                    "POST", "/projects",
                    json={"engagement_id": eid, "title": f"engagement-{eid}"},
                )
                pid = str(proj["id"])
            except CairnClientError as exc:
                self.log(f"create_project failed: {exc}")
                return None
        self._project_id[eid] = pid
        self._project_to_eid[pid] = eid
        self._runtime_projects.add(pid)
        return pid

    def _persist_intents(self, eid: str, pid: str, intents: Iterable[dict], creator: str) -> None:
        persisted: list[dict] = []
        for it in intents:
            try:
                created = self.client._request(
                    "POST", f"/projects/{pid}/intents",
                    json={
                        "description": str(it.get("description") or ""),
                        "creator": creator,
                        "from_fact_ids": list(it.get("from") or []),
                    },
                )
                # 服务端 intent 行无 coverage_item_ids 列 —— 合并 reason 产出的格子引用，
                # 供 explore 派发（B1 认领 + prompt 注入）使用。
                created["coverage_item_ids"] = list(it.get("coverage_item_ids") or [])
                persisted.append(created)
            except CairnClientError as exc:
                self.log(f"create_intent failed: {exc}")
        if persisted:
            self._pending_intents[eid] = self._pending_intents.get(eid, []) + persisted

    def _project_ids_for(self, eid: str) -> list[str]:
        if eid in self._project_id:
            return [self._project_id[eid]]
        return [p for p in self._runtime_projects]

    # ==================================================================
    # server 查询辅助
    # ==================================================================

    def _list_active(self) -> list[dict]:
        try:
            return self.client.list_active() or []
        except CairnClientError as exc:
            self.log(f"list_active failed: {exc}")
            return []

    def _get_gaps(self, eid: str) -> list[dict]:
        try:
            return self.client.get_gaps(eid, exclude_in_progress=True, limit=50) or []
        except CairnClientError as exc:
            self.log(f"get_gaps({eid}) failed: {exc}")
            return []

    def _safe_export(self, pid: str) -> str:
        try:
            return self.client.export_yaml(pid)
        except CairnClientError as exc:
            self.log(f"export_yaml({pid}) failed: {exc}")
            return ""

    def _scope_text(self, eid: str) -> str:
        try:
            targets = self.client.list_targets(eid) or []
        except CairnClientError:
            return ""
        authorized = [t.get("value") for t in targets if t.get("scope_status") == "authorized"]
        prohibited = [t.get("value") for t in targets if t.get("scope_status") == "prohibited"]
        return f"authorized={authorized}; prohibited={prohibited}"

    def _list_open_findings(self, eid: str) -> list[dict]:
        try:
            resp = self.client._request(
                "GET", f"/engagements/{eid}/findings", params={"status": "open"}
            )
        except CairnClientError as exc:
            self.log(f"list open findings failed: {exc}")
            return []
        return (resp.get("items") if isinstance(resp, dict) else None) or []

    def _finding_detail(self, eid: str, fid: str) -> Optional[dict]:
        try:
            return self.client._request("GET", f"/engagements/{eid}/findings/{fid}")
        except CairnClientError as exc:
            self.log(f"finding detail failed: {exc}")
            return None

    def _mark_pending_verify(self, eid: str, fid: str, worker: str) -> None:
        try:
            self.client._request(
                "PUT", f"/engagements/{eid}/findings/{fid}",
                json={"status": "pending_verify", "note": "verify dispatched", "actor": worker},
            )
        except CairnClientError as exc:
            self.log(f"mark pending_verify failed: {exc}")

    # ==================================================================
    # periodic（v2 §9.1）
    # ==================================================================

    def _run_periodic(self) -> None:
        # B5：窗口到期自动 pause（expire_engagements 释放租约）
        try:
            self.client._request("POST", "/engagements/expire")
        except CairnClientError as exc:
            self.log(f"expire_engagements failed: {exc}")
        # C3：捕获代理对账（对 active 集合增量 start/stop；kill/过期/归档 → 停抓包）
        self._reconcile_capture_proxies()
        # C2 捕获完整性对账（产出 capture_gap 看板，落 scheduler_state）
        for eid in list(self._project_id):
            try:
                self.client._request("POST", f"/engagements/{eid}/capture/reconcile")
            except CairnClientError as exc:
                self.log(f"capture reconcile({eid}) failed: {exc}")
        # C11 白名单热刷新：capture 服务端按 authorized targets 即时派生，无需动作。
        # task_events 原始流清理（event_raw_retain_days）：原始文件在 Server FS，
        # 由服务端 cron 执行（本循环无 HTTP 写通道，阶段 2 对齐）。

    # ==================================================================
    # scheduler_state 落库 / 回载
    # ==================================================================

    def _persist_state(self) -> None:
        data: list[tuple[str, str]] = []
        unhealthy = self.health.snapshot() if self.health is not None else {}
        if unhealthy:
            data.append((KEY_UNHEALTHY, json.dumps(unhealthy)))
        if self._rejected_until:
            data.append((KEY_REJECTED, json.dumps(self._rejected_until)))
        checkpoints = self._reason_checkpoints()
        if checkpoints:
            data.append((KEY_REASON_CHECKPOINTS, json.dumps(checkpoints)))
        if self._runtime_projects:
            data.append((KEY_RUNTIME_PROJECTS, json.dumps(sorted(self._runtime_projects))))
        for eid in sorted(self._bootstrap_done):
            esc = self._escalation.snapshot(eid)
            if esc is not None:
                data.append((f"reason_escalation:{eid}", json.dumps(esc)))
        for key, value in data:
            try:
                self.client._request("PUT", f"/scheduler_state/{key}", json={"value": value})
            except CairnClientError as exc:
                self.log(f"scheduler_state 写入失败（忽略）: {key} {exc}")

    def _reason_checkpoints(self) -> dict[str, Any]:
        """reason_checkpoints：每 engagement 的调度进度（重启回载后据此恢复）。"""
        out: dict[str, Any] = {}
        for eid in sorted(self._bootstrap_done):
            out[eid] = {
                "bootstrap_done": True,
                "pending_intents": len(self._pending_intents.get(eid) or []),
                "updated_at": _now_iso(),
            }
        return out

    def _load_state(self) -> None:
        """启动回载 scheduler_state（重启不丢 reason 计数/冷却/运行时项目）。"""
        try:
            rows = (self.client._request("GET", "/scheduler_state") or {}).get("items") or []
        except CairnClientError as exc:
            self.log(f"scheduler_state 读取失败（忽略）: {exc}")
            return
        state: dict[str, str] = {}
        for row in rows:
            state[str(row.get("key"))] = str(row.get("value") or "")
        unhealthy_raw = state.get(KEY_UNHEALTHY)
        if unhealthy_raw and self.health is not None:
            try:
                self.health.load_snapshot(json.loads(unhealthy_raw))
            except (ValueError, TypeError):
                pass
        rejected_raw = state.get(KEY_REJECTED)
        if rejected_raw:
            try:
                self._rejected_until = {
                    k: float(v) for k, v in json.loads(rejected_raw).items()
                }
            except (ValueError, TypeError):
                pass
        checkpoints_raw = state.get(KEY_REASON_CHECKPOINTS)
        if checkpoints_raw:
            try:
                for eid, cp in json.loads(checkpoints_raw).items():
                    if cp.get("bootstrap_done"):
                        self._bootstrap_done.add(str(eid))
            except (ValueError, TypeError):
                pass
        projects_raw = state.get(KEY_RUNTIME_PROJECTS)
        if projects_raw:
            try:
                self._runtime_projects = set(json.loads(projects_raw))
            except (ValueError, TypeError):
                pass
        for key, value in state.items():
            if key.startswith("reason_escalation:"):
                eid = key.split(":", 1)[1]
                try:
                    self._escalation.load(eid, json.loads(value))
                except (ValueError, TypeError):
                    pass

    # ==================================================================
    # 启动 reconcile（v2 §8.2：僵尸 running + 超时 intent）
    # ==================================================================

    def _startup_reconcile(self) -> None:
        """清理现场：遗留 running task_runs 置 failed；超时 intent 认领置 untested（B1）。"""
        self.log("startup reconcile: 清理僵尸 running task_runs")
        for eng in self._list_active():
            eid = str(eng.get("id") or "")
            try:
                rows = self.client._request(
                    "GET", f"/engagements/{eid}/tasks", params={"status": "running"}
                )
            except CairnClientError as exc:
                self.log(f"list running task_runs({eid}) failed: {exc}")
                continue
            items = rows.get("items") if isinstance(rows, dict) else rows
            for r in items or []:
                try:
                    self.client.finish_task_run(
                        str(r["id"]), "failed", outcome_note="zombie: dispatcher restart reconcile"
                    )
                except CairnClientError as exc:
                    self.log(f"finish zombie task_run failed: {exc}")
            self._reconcile_timeout_intents(eid)

    def _reconcile_timeout_intents(self, eid: str) -> None:
        """超时 intent 认领 → 置 current_intent_id=NULL + untested（B1 释放语义）。"""
        try:
            items = self.client.list_items(eid) or []
        except CairnClientError as exc:
            self.log(f"list coverage items({eid}) failed: {exc}")
            return
        for it in items:
            iid = it.get("current_intent_id")
            if not iid:
                continue
            if self._intent_timed_out(eid, iid):
                try:
                    self.client._request(
                        "POST", f"/engagements/{eid}/coverage/items/{it['id']}/release",
                        json={"intent_id": iid},
                    )
                    self.log(f"reconcile: 释放超时 intent {iid} 的覆盖项 {it['id']}（B1）")
                except CairnClientError as exc:
                    self.log(f"release timeout intent item failed: {exc}")

    def _intent_timed_out(self, eid: str, iid: str) -> bool:
        """判定 intent 是否超时（>2×interval 无心跳）。无法解析时按超时处理（B1 释放）。"""
        pid = self._project_id.get(eid)
        if pid is None:
            try:
                projects = self.client._request(
                    "GET", "/projects", params={"engagement_id": eid}
                ) or []
            except CairnClientError:
                projects = []
            if not projects:
                return True
            pid = str(projects[0]["id"])
        try:
            proj = self.client._request("GET", f"/projects/{pid}")
        except CairnClientError:
            return True
        for intent in proj.get("intents") or []:
            if str(intent.get("id")) != iid:
                continue
            last = intent.get("last_heartbeat_at")
            if not last:
                return True  # 认领但从未心跳 → 超时
            try:
                ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                return (datetime.now(timezone.utc) - ts).total_seconds() > 2 * self.interval
            except ValueError:
                return True
        return True


__all__ = ["DispatcherLoop", "run_dispatch_loop"]
