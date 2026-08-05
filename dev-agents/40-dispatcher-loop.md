# Agent 40 — Dispatcher 调度主循环（Scheduler Loop / Guards / Reconcile）

> 阶段 2 · 依赖 12/30（任务实现）+ 11（容器）+ 13（驱动/CLI 入口）+ 20/21/22/24（服务端 API）。把 30 的「单任务」编排成闭环调度。

## 0. 开工前必读
1. `CLAUDE.md`（不变量 1/2/9）
2. `docs/architecture-research-report-pentest-v2.md` §8.2（Dispatcher 改造：guards/联动/状态落库/启动 reconcile/孤儿清理）、§9（定时任务清单）、§8.6
3. `docs/backend-module-skeleton.md` §1（scheduler/loop.py、worker_select.py）、§3（TaskType 扩展）
4. `docs/dispatch-config-spec.md` §6（scope 守卫开关）、§7（tuning 魔数）
5. `docs/capture-verify-progress-spec.md` §2.5（周期对账）、§4（verify 派发独立性）、§6（fixed 触发复测时序）
6. `docs/database-ddl-draft.md` §7（scheduler_state）
7. `docs/rule-registry.md`（B5/C1/C8/C11/F7）
8. `dev-agents/13-dispatcher-runtime.md`（CLI 装配回调签名、WorkerDriver/健康检查接口——你编排的对象）

## 1. 交付范围
```
cairn/src/cairn/dispatcher/scheduler/__init__.py
cairn/src/cairn/dispatcher/scheduler/loop.py         # 主循环：guards → 选任务 → 派发 → 状态落库 → 心跳 → 孤儿清理
cairn/src/cairn/dispatcher/scheduler/worker_select.py # worker 选择：优先级/冷却/verify 排除创建者/replay-engine 特例
cairn/src/cairn/dispatcher/runtime/heartbeat.py       # 保留改造（intent/reason 租约心跳 + task_runs 心跳）
cairn/src/cairn/dispatcher/__init__.py
cairn/tests/test_scheduler_logic.py
```

## 2. 必须满足的契约
- **A. guards（§8.2 + dispatch-config-spec scope 段）**：每轮派发前 `_check_scope_guard`（目标白名单，SCOPE_DENIED 禁 fallback）、`_check_kill_switch`（全局 settings.global_kill_switch + 项目 kill_switch → 423 KILL_SWITCH_ON，C1 即时 SIGKILL 通知 11）、`_check_auth_window`（窗口外拒绝 + 到期自动 pause，B5 释放租约）。
- **B. 任务触发**：
  - bootstrap：engagement 初始态（无 bootstrap 完成）→ 派发一次；
  - reason：每轮（或 gaps 变化触发），输入 compute_gaps（exclude_in_progress=True，21）；空转/失败计数按 `reason_escalation`（C8）升级 needs_review 后停止自动重试；
  - explore：intent 认领后派发（对应覆盖项格子已 claim，B1）；
  - verify：finding=open 自动入队，**排除创建 worker** + `verify_eligible`（F7），单 worker 降级 cross_run；
  - audit：`sample_audit`（21）产出后派发（独立 worker ≠ 原测试者）；
  - retest：finding=fixed → 重建覆盖项（A5）+ 入队 replay + retest explore + verify（capture §6 时序，并行无依赖）。
- **C. worker 选择**：优先级升序 + 冷却（rejected/unhealthy，tuning）+ per-worker max_running + per-engagement max_project_workers + 全局 max_workers + max_running_projects。`replay-engine` 为内建 worker（不走 worker 列表，30 提供引擎）。
- **D. 状态落库与启动 reconcile（§8.2）**：每轮把 `reason_checkpoints`/`worker_unhealthy_until`/`worker_rejected_until`/`runtime_project_ids` 写 `scheduler_state`（重启回载）。启动 reconcile：本地遗留 `task_runs` status='running' 僵尸行置 failed；`coverage_items.current_intent_id` 认领但 intent 超时（>2×interval 无心跳）→ 置 current_intent_id=NULL + status='untested'（B1 释放语义）。
- **E. 孤儿清理**：`cleanup_orphan`/`managed_container_names` 接入主循环（v2 §8.2 明确"修复原死代码"）。
- **F. 周期任务（v2 §9.1）**：`expire_engagements`（20）、捕获完整性对账（23 `reconcile`，产出 capture_gap 看板）、白名单热刷新（23）、task_events 原始流清理（tuning.event_raw_retain_days）。
- **G. 心跳/取消**：`HeartbeatLease` 语义保留；kill switch 走即时 SIGKILL（不走 grace，C1）；403/409 心跳 → 立即 fail；其他失败 2×interval 宽限。
- **H. 编排方式**：主循环**调用 30 的任务函数**（纯逻辑），调度节奏由 `runtime.interval` 驱动；实现必须可测（interval 可注入小值）。

## 3. 验收标准
1. `pytest test_scheduler_logic.py`：守卫拒绝路径（prohibited/kill/窗口外）；worker 选择（优先级/冷却/排除创建者/replay 特例）；并发上限（max_workers/max_running_projects）。
2. 启动 reconcile：构造僵尸 running 行 + 超时 intent → 重启后状态正确。
3. 端到端（配合 31 mock）：bootstrap→reason→explore→verify→fixed→retest→closed 全链路在一轮调度内推进；reason 空转升级 needs_review 停自动重试（对照 TV-43）。
4. kill 即时性：项目 kill → 容器 SIGKILL + 停捕获（用 11 的接口冒烟）。
5. scheduler_state 回载：重启后 reason 计数/冷却不丢。

## 4. 硬约束
- **不直接连 DB**：所有读写走 12 客户端 → Server。
- **CLI 入口由 13 装配**：你提供主循环 callable（签名按 13 交接物），`cairn dispatch` 的加载/信号由 13 负责，不重复实现。
- **不实现单任务逻辑**（30 负责）；你只编排。
- 派发粒度：verify 排除创建者的判断在 worker_select 内（30 提供 creator 归属信息）。
- interval 相关的魔数读 tuning，不硬编码。

## 5. 交接物
写 `dev-agents/notes/40-dispatcher-loop.md`：调度状态机图、任务触发条件表、reconcile 实现、periodic 清单、端到端冒烟结果、已知问题。
