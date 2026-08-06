# 50-reviewer 交叉审查与验收报告（Docs vs Implementation 对拍）

- 审查 Agent：50-reviewer  日期：2026-08-06
- 审查范围：Phase 0-2 全部包（10-13 / 20-25 / 30 / 31 / 40 / 41 + phase0-alignment），
  对拍 CLAUDE.md 黄金不变量 + docs/ 权威规格 + dev-agents/notes/ 交接物。
- 执行：`uv run --project cairn pytest -q` → **468 passed / 46 skipped（0 failed）**；
  `test_mock_end_to_end.py` → **48 passed / 46 skipped**（skipped 全部为 TV-01..46 矩阵）。

---

## 1. 结论摘要

**是否达到「从 0 交付」门槛：未完全达到（P0 = 0，但存在 1 项 P1 级验收阻断项 + 2 项 P1 安全/不变量问题）。**

- **P0 = 0**：无崩溃、无全量红、无被现行测试覆盖的安全不变量主动违反。全量 468 passed 属实。
- **关键阻断项（P1）**：`verify-mock-test-spec.md §7` 的核心验收 **TV-01..46 全链路 mock 矩阵 46 例全部 SKIPPED，从未运行**（`test_mock_end_to_end.py::e2e_ctx` 直接 `pytest.skip`）。「mock 回归全绿（46 用例）」的完成判据**不满足**——不是「绿」，是「未跑」。这是唯一接近 P0 的缺口。
- 其余 P1 见差异清单（loop 单 worker verify 派发到创建者违反 F1；`GET /projects` 未收窄鉴权豁免）。
- 规则号核对：**零冲突**（代码注释中全部规则号在 rule-registry.md 可解析；`规则 N` 引用与 v2 §12 映射一致）。
- 前端维度：**42 并行构建中**（`server/static/index.html` 已出现、src/ 在写），frontend §9 验收点暂记未覆盖，待 42 完成后复核。
- 已知开放项（phase0-alignment #17/21/22/23/24/32/33/35/36）均已复核为「文档待同步或阶段 2 待办」，无 P0。

---

## 2. 差异清单

### P1（阻断/不变量/安全，均需代码包修复或编排者裁决）

| # | 文件:行 | 问题 | 建议修复（归属） |
|---|---|---|---|
| P1-1 | `cairn/tests/test_mock_end_to_end.py:588`（e2e_ctx `pytest.skip("e2e_ctx wiring owned by Agent 50; not yet installed")`） | **TV-01..46 全链路 mock 矩阵全部 SKIPPED，从未运行**。46 skipped 恰好等于 TV 矩阵数；`test_mock_end_to_end.py` 只有 48 个驱动/脚本单测在跑。verify-mock-test-spec §7「46 用例…全部在无 LLM、无真实流量下运行」未达成；「mock 回归全绿」判据不满足（未跑 ≠ 绿）。30/40 交付后 importorskip 已不再跳过，是**接线缺失**而非红测试。 | **代码包（30/40/31 或新接线 Agent）**：实现 `e2e_ctx` —— 进程内 FastAPI Server TestClient + CairnClient + DispatcherLoop(LocalBackend + MockDriver，worker-A=创建者、worker-B/C=独立 verify)，seed engagement/targets/traffic/findings，`pump_until_idle`。TV_CASES 场景函数已写好、rules 标注已就绪，只欠装配。 **[fixed 2026-08-06 by wiring-agent]** `e2e_ctx` 已接线；`test_mock_end_to_end.py` → **94 passed / 0 skipped**（TV-01..46 全绿，run 而非 skip）；全量 `pytest -q` → **517 passed / 0 failed**。实现摘要：每 TV 用例新建 in-process Server（temp DB）+ CairnClient + DispatcherLoop(LocalBackend + 3×MockDriver，worker-A=创建者、worker-B/C=独立 verify) + seed engagement/targets/traffic/finding；mock env 按 tv_id 确定性注入；`pump_until_idle` 以 task_run 数增量判 idle；`find_verify_run` 经 verify_runs.task_run_id（失败分支回退最新 verify task_run）。对 30/40/22 的极小修复：run_verify 增加 accepted=false→rejected 映射 + critical P0 error 事件；routers/findings.list_replay 排序列 created_at→started_at（DDL §9.3 无 created_at）。 |
| P1-2 | `cairn/src/cairn/dispatcher/scheduler/loop.py:429-436` | **单 worker verify 兜底会派发给 finding 创建者**：`select_verify_worker` 返回 None（排除创建者后无候选）时，fallback `select_worker(verify)` 会把 verify 派给「唯一 worker = 创建者」并标 `cross_run`。违反 F1「排除创建者」独立不变量，且与 verify-mock-test-spec TV-10 断言（「不派发给 A；finding 停留 pending_verify；任务标等待独立复核」）**直接冲突**。TV-10 接线后会红。 | **40**：单 worker 且该 worker 即创建者时，**不派发**、finding 保持 pending_verify（记「等待独立复核」）；或与 30/文档对齐 TV-10 语义（cross_run 仅适用于「存在独立于创建者的单 worker」场景）。 |
| P1-3 | `cairn/src/cairn/dispatcher/scheduler/loop.py:177-191` + `runtime/cancellation.py` | **C1「熔断即时性」未满足**：主循环同步执行任务（`communicate` 阻塞），kill switch 只在**每轮调度开始**检查。任务运行中 kill 被置位时，要等该任务返回才观察到，**非「触发即 SIGKILL」**（C1 要求不等下一轮）。40 交接物已承认此局限。 | **40/11**：任务线程化或加 kill 探针（每任务周期轮询 kill_switch，触发即 `cancellation.kill_switch` + `ctx.force_kill`）。 |
| P1-4 | `cairn/src/cairn/server/middlewares/auth.py:41-42`（GET /projects 豁免）+ `routers/projects.py`（无路由级鉴权） | **`GET /projects` 无鉴权**：任何无 token 的 HTTP 调用可枚举全部 project（含 engagement_id/title）。渗透平台服务器对未认证调用暴露项目元数据。phase0-alignment #7/#26 标为 open（占位期遗留）。 | **10/25/orchestrator**：去掉 `GET /projects` 豁免（仅保留 GET /health），或 projects 路由加显式 `Depends` 鉴权；同步更新 test_server_foundation 豁免断言。 |

**P1-2 / P1-3 / P1-4 修复状态**（跨包修复 Agent，2026-08-06）：

- **[fixed 2026-08-06 by wiring-agent] P1-1**：`test_mock_end_to_end.py::e2e_ctx` 从 `pytest.skip` 改为真实装配（in-process Server + CairnClient + DispatcherLoop + LocalBackend + 3×MockDriver + DispatchView/E2EHttpClient）；`pump_until_idle` 以 task_run 数增量判 idle；`find_verify_run` 经 verify_runs.task_run_id（失败分支回退最新 verify task_run）。TV-01..46 全绿（94 passed / 0 skipped），全量 `pytest -q` 517 passed / 0 failed。附带极小修复：run_verify accepted=false→rejected + critical P0 error 事件（TV-03/16）；routers/findings.list_replay 排序列 created_at→started_at（DDL §9.3 无 created_at）。

- **[fixed 2026-08-06 by fix-agent] P1-2**：`loop.py::_maybe_verify` fallback 的 `select_worker` 现在显式传 `creator=creator`（排除创建者，F1）；无独立候选且不存在任何「非创建者 verify worker」（`_has_independent_verify_worker` 判 False，TV-10）时**不派发**，finding 标 `pending_verify`（`_mark_waiting_independent_verify`，note「等待独立复核」）；并发/健康暂时不可用（存在独立 worker）时保持 open 下轮再试。cross_run 仅在「非创建者候选」存在时降级。新增 2 例针对性测试（单 worker=创建者不派发 / 双 worker 派到非创建者）。原 E2E 单 worker 用例改为双 worker（mock-A 创建、mock-B 复核）以保持全链路 verified 断言。
- **[fixed 2026-08-06 by fix-agent] P1-3**：`loop.py` 新增后台 kill 监控线程（`_start_kill_monitor`/`_stop_kill_monitor`，`run()` 起停，`_KILL_MONITOR_POLL=0.2s`）：任务运行期间（`self._running` 非空）轮询 `list_active()` 的 kill_switch，一旦运行中任务所属 engagement 熔断 → 立即 `cancellation.kill_switch()`（即时 SIGKILL 绑定进程，C1 不走 grace），不等 `communicate` 返回。30 的 `run_worker_phase` 已把进程 attach 到 `TaskCancellation`，本修复补上「kill 触发 → cancel」的触发链路。新增 `test_kill_monitor_kills_running_task_immediately` 断言运行中 `sleep 30` 进程在 kill_switch 触发后被立即 SIGKILL。
- **[fixed 2026-08-06 by fix-agent] P1-4**：`auth.py::default_exempt_paths` 移除 `GET /projects` 豁免（仅保留 `GET /health`；42 前端静态资源豁免不受影响）；`test_server_foundation.py::test_health_and_projects_200` 改为断言 GET /projects 无 token → 401 AUTH_REQUIRED（原 200 豁免断言移除）；`test_graph.py` 本已带 Authorization 头，无需改动。

### P2（文档同步 / 待办 / 环境）

| # | 位置 | 问题 | 建议（归属） |
|---|---|---|---|
| P2-1 | `docs/coverage-engine-implementation-spec.md` §1 vs `docs/database-ddl-draft.md` §8 / `server/db.py:489-491` | **FTS5 contentless 不一致**：coverage spec §1 用 `content=''`（contentless），DDL §8 与 db.py 均无 `content=''`。FTS 同步触发器全未实现（占位），但 spec 与 DDL 口径漂移。 | 文档（coverage spec 或 DDL 二选一统一；db.py 跟随 DDL 权威）。 |
| P2-2 | `docs/exploration-graph-spec.md` §2.4 | VALIDATION 标 HTTP 400，代码（errors.py + 各路由）统一 422（phase0-alignment #17，open）。 | 文档（graph spec §2.4 改 422）。 |
| P2-3 | `docs/backend-module-skeleton.md` §3 `services/coverage.py#apply_audit_verdict` / `services/findings.py#retest_pass_count` | 两处签名与实现偏离（phase0-alignment #21/#22）已登记但 skeleton §3 **未同步**：`apply_audit_verdict(conn, audit_id, *, verdict)` ≠ 实现 `(conn, eid, *, item_id, verdict, auditor, ...)`；`retest_pass_count -> int` ≠ 实现返回 dict。 | 文档（skeleton §3 同步到实现；或 21/22 改码，需协调）。 |
| P2-4 | `docs/dispatch-config-spec.md` §8 / `runtime/containers.py:142-144` | 「capture 模式必须 bridge」为配置注释，**无运行时代码强制**：`network_mode` 直接用 config，capture 开启 + host 网络不会报错（C12 归属失效仅文档提示）。 | 文档注明或 11 加运行时守卫（capture enabled 且 network_mode=host → 启动拒绝）。 |
| P2-5 | `docs/backend-module-skeleton.md` §2.5 | `POST /engagements/{id}/findings/{fid}/evidence` 标 `H`，实现为 JSON+base64（无 multipart、无 actor gate）。evidence 上传 Agent 也走此端点（explore 写回需要），`H` 标注与实现语义不符（D2 下仅语义标注，无实际差异）。 | 文档（skeleton §2.5 注释说明 evidence 写回 Agent 亦可达，业务白名单落实）。 |
| P2-6 | `cairn/src/cairn/server/routers/traffic.py` + `middlewares/auth.py:43-46` | F8 代理单写者端点正确：`POST /engagements/{eid}/traffic` 豁免主 token 中间件、路由级 `require_capture_token` 校验受限 token。无问题，仅确认。 | —（确认项）。 |
| P2-7 | `docs/worker-sandbox-hardening.md` §3 | Dockerfile 未能在本环境构建/运行（docker CLI Permission denied，phase0-alignment #5/#27），容器加固仅 fake-client/CLI 单测覆盖（`test_container_archives.py` 28 passed）。真实镜像验收留待有权限环境。 | env（编排者/CI）。 |
| P2-8 | `cairn/src/cairn/dispatcher/runtime/containers.py:292` | 容器名 `cairn-{project_id}` vs graph spec §4-15 `cairn-dispatch-<project_id 的 /→->`：前缀不一致（proj 无 `/` 时唯一性无碍，属命名偏离）。 | 文档或代码二选一对齐（低优先）。 |
| P2-9 | `docs/backend-module-skeleton.md` §3 `services/progress.py#open_task_run` | 服务签名 `(engagement_id, project_id=None, task_type, worker)` 与客户端 `open_task_run(eid, task_type=..., worker=..., project_id=...)` 参数顺序/关键字不同，但语义一致；REST 由 40 补建 `dispatch.py`。无功能问题。 | 文档（skeleton §3 参数顺序同步，低优先）。 |
| P2-10 | `docs/database-ddl-draft.md` §9.3 / `server/services/report.py:175` | `replay_runs` 无 `created_at`（报告按 `started_at` 排序）；`verify_runs` 无 `engagement_id`（stats 经 findings JOIN）。phase0-alignment #32。 | 文档或 DDL 加列（低优先，报 10）。 |

---

## 3. 规则号核对表（代码出现编号 ↔ registry）

| 编号 | 代码引用处（抽查） | registry | 结论 |
|---|---|---|---|
| A1/A2/A3/A4/A5 | db.py next_id 注释、coverage.py priority/rebuild、graph.py set_project_status、findings.py transition | ✅ A 组 | 一致 |
| B1 | coverage.py claim/release/write、loop.py claim_all、scope.py resolve_target | ✅ B1 格子互斥 | 一致 |
| B2 | progress.py open_task_run project_id 可空、db.py task_runs | ✅ B2 | 一致 |
| B3 | findings.py _normalize_title / dedup_key | ✅ B3 URL/title 规范化去重 | 一致 |
| B4 | coverage.py waive_item / write_coverage_result not_applicable | ✅ B4 豁免 | 一致 |
| B5 | scope.py transition paused / graph.py freeze_project_leases | ✅ B5 冻结 | 一致 |
| B6/B7 | containers.py 卷挂载 workspace / evidence | ✅ B6/B7 | 一致 |
| C1 | cancellation.py kill_switch SIGKILL、loop.py _handle_kill、containers.py | ✅ C1 | **实现**（但 loop 同步模型见 P1-3） |
| C2 | capture.py derive/reconcile、findings.py http source、verify.py http_mismatch | ✅ C2 | 一致 |
| C3 | capture_proxy.py stop_engagement / whitelist.clear | ✅ C3 | 一致 |
| C5 | containers.py _assert_no_cairn_secrets、workers/base.py | ✅ C5 | **grep 实证无 Cairn token 注入 Agent 容器** |
| C7 | verify.py independence cross_model | ✅ C7 | 一致 |
| C8 | coverage.py reason_escalation_state、loop.py _escalation | ✅ C8 | 一致 |
| C9 | coverage.py write_coverage_result partial/tested_scope | ✅ C9 | 一致 |
| C10 | findings.py record_retest_confirmation / _assert_closed_gate | ✅ C10 | 一致 |
| C11/C12 | capture.py derive_allow_hosts / resolve_client | ✅ C11/C12 | 一致 |
| D2 | middlewares/auth.py（T/H 同 token） | ✅ D2 | 一致 |
| D3 | timeline.py engagement_timeline 六源 | ✅ D3 | 一致 |
| D4 | report.py 证据附录引用层 | ✅ D4 | 一致 |
| D5 | coverage.py infer_criticality | ✅ D5 | 一致 |
| F1 | verify.py blind→comparison、loop.py select_verify_worker | ✅ F1 | **loop 单 worker 派发到创建者，见 P1-2** |
| F2 | capture.py make_digest / resolve_traffic for_model | ✅ F2 | 一致 |
| F3 | coverage.py sample_audit / apply_audit_verdict | ✅ F3 | 一致 |
| F4 | replay/engine.py compare_signature | ✅ F4 | 一致 |
| F5 | capture.py assert_capture_allowed / server_assert | ✅ F5 | 一致 |
| F6 | findings.py bump_reverify | ✅ F6 | 一致 |
| F7 | loop.py 单 worker cross_run 降级 | ✅ F7 | **降级语义见 P1-2** |
| F8 | traffic.py require_capture_token、auth.py 豁免 | ✅ F8 | 一致 |
| F9 | progress/stream.py classify_line | ✅ F9 | 一致 |
| F10 | report/capture 协议边界降级（命令证据） | ✅ F10 | 一致（无 WebSocket/隧道专表，命令证据兜底） |
| F11 | coverage.py closure_rule / report_ready 排除 | ✅ F11 | 一致 |
| TV-30/32/44 | mock_harness TV_CASES | ✅ TV 编号 | 一致（未运行，见 P1-1） |
| 规则 1/2/3/4/5/8/13/18/19/21/22/26/27/39 | 各处代码注释（v2 §12 旧编号） | registry「出处」列 | 与 v2 §12 映射一致，无重号 |

**零冲突**。未发现 code 中出现 registry 未登记/重号的 A/B/C/D/F/O/TV 编号。

---

## 4. 未覆盖项（spec 验收点未验证/未实现）

| 维度 | 状态 | 说明 |
|---|---|---|
| **verify-mock TV-01..46** | **未验证（46 例全部 skipped）** | `test_mock_end_to_end.py::e2e_ctx` 直接 skip。30/40/31 已就绪但**未接线**。这是最大的未覆盖项（P1-1）。 |
| **前端 frontend §9** | **进行中（42 并行构建）** | `server/static/index.html` 已出现（15:01）、src/ 在写；`_mount_static` 已会挂载 `/`。前端验收点暂记未覆盖，待 42 完成复核；注意 SPA catch-all 与 API 404 的交互。 |
| **Docker 真实容器验收**（worker-sandbox §8） | 未验证（环境无 docker 权限） | 镜像构建/运行/`docker scout`/`capsh --print` 等留待有权限环境；单测层面 fake-client/CLI 全覆盖（test_container_archives 28 passed）。 |
| **audit 自动抽样派发（F3 服务端闭环）** | 未实现 | `loop._maybe_audit` 返回 None；21 `sample_audit` 选样无 REST pending 列表端点（phase0-alignment #36）。40 交接已承认。 |
| **replay 自动复测接线**（fixed→rebuild→replay→retest explore→verify） | 未接线 | 40 交接承认（需 41/22 服务端 fixed 触发 + 扫描端点）。`ReplayEngine`（30）已实现。 |
| **task_events 原始流清理**（event_raw_retain_days） | 未实现 | 服务端 cron（40 交接承认）。 |
| **FTS5 同步触发器**（fts_facts/findings/coverage） | 未实现 | 各包交接一致承认（占位表）。 |
| **`confirm_audit_run`**（两阶段 audit 派发） | 未实现 | 21 交接承认；`apply_audit_verdict` 为一步式。 |
| **`seed_from_discovery` 服务端薄封装** | 未实现 | 21 交接承认；Dispatcher 侧 `CoverageWriter.seed_from_discovery`（30）已实现并走 upsert 原语。 |
| **mitmproxy 真实集成 / addon / pcap / 归档 C4 物理迁移** | 未实现 | 23 交接承认（`MitmProxyEngine` 已写、未联调；`FakeProxyEngine` 用于测试）。 |
| **`cairn-executor` 侧车（P1）** | 未实现 | 11 交接承认（compose 占位）。 |

---

## 5. 给编排者的交接要点（下一轮 P1 修复单）

1. **接线 TV-01..46**（P1-1，最高优先）——代码包实现 `e2e_ctx`（in-process Server + CairnClient + DispatcherLoop + LocalBackend + MockDriver，多 worker）。
2. **修 loop 单 worker verify 派发到创建者**（P1-2）——40，对齐 TV-10。
3. **C1 熔断即时性**（P1-3）——40/11，任务线程化或 kill 探针。
4. **收窄 `GET /projects` 鉴权豁免**（P1-4）——10/25/orchestrator。
5. **文档同步**（P2-1/P2-2/P2-3/P2-4）：coverage spec FTS5 contentless、graph spec VALIDATION=400→422、skeleton §3 apply_audit_verdict/retest_pass_count、dispatch-config bridge 强制说明（本包可改文档，待编排者批准 diff）。

**完成判据复核**：P0=0 ✅；P1 全部有明确归属与修复方案 ✅（4 项）；mock 回归**未全绿**（TV 矩阵未跑，P1-1 阻断）；规则号零冲突 ✅。
