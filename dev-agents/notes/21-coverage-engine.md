# 21-coverage-engine 交接物

- 完成 Agent：21-coverage-engine  日期：2026-08-06
- 阶段：Phase 1 · 核心差异化模块（覆盖度收敛）
- 依赖：10（server 基座）。被依赖：20（create_engagement 播种）、22（report_ready 读 triaged）、25（conclude 编排写回）、30/40（缺口消费 + B1 认领/释放）、41（finalize 校验 report_ready）。

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `cairn/src/cairn/server/services/coverage.py` | `seed_default_test_types`、`priority_score`、`infer_criticality`、`compute_gaps`、`coverage_summary`、`report_ready`、`upsert_coverage_item`、`claim_item_for_intent`、`release_item_for_intent`、`write_coverage_result`、`waive_item`、`rebuild_for_retest`、`sample_audit`、`apply_audit_verdict`、`closure_rule`、`reason_escalation_state`、`DEFAULT_TEST_TYPES`(28)、`DEFAULT_COVERAGE_POLICY` | 覆盖子域全部服务；见 §2/§3 |
| `cairn/src/cairn/server/routers/coverage.py` | `router`（prefix `/engagements/{id}/coverage`） | skeleton §2.3 全部 + 写回/认领/释放/导出端点 |
| `cairn/tests/test_coverage.py` | 26 个测试 | §6 验收映射 |

## 2. seed 实现确认（20 硬依赖）

- **签名固定 `(conn, eid) -> None`**。20 的 `services/scope._seed_default_test_types` 已按此契约 `from .coverage import seed_default_test_types` 导入并真调用（阶段 1 联调已实测：`create_engagement` 后 test_types = **28 行**，enabled=1）。
- 按 coverage spec §1.1 目录写入 `test_types`（id=`tt_<slug>` 幂等键，`(engagement_id, name)` UNIQUE，INSERT OR IGNORE 幂等）。**只写目录，不生成覆盖项**（覆盖项由 bootstrap 播种 / 人工播种产生）。
- 只播种 enabled 的 test_types；`network_cap=false` 禁用网络项由 20/人工处理（不在本函数职责）。

## 3. 关键签名与契约

- `compute_gaps(conn, eid, *, threshold=0.0, exclude_in_progress=False, limit=50) -> list[dict]`：缺口 = untested（+可选 in_progress），排除 waived/not_applicable；**priority 实时计算（A3）**，排序 `(-priority, target_id, item_id)`，limit 默认 50。`exclude_in_progress=True` 时取 `status='untested' AND current_intent_id IS NULL`。
- **priority 口径（A3）**：`priority_score(criticality, risk, depth) = criticality × risk × (1+0.2 if depth != baseline)`；`infer_criticality(kind, service_kind)`（D5）。`compute_gaps`/`sample_audit`/热力图**一律实时计算**，`coverage_items.priority_score` 仅展示缓存、不作为排序依据。
- **写回幂等键（C9）**：`coverage_records` 以 **`(item_id, intent_id)`** 应用层去重（DDL 无 UNIQUE 约束）。幂等检查在认领校验**之前**——「服务端已成功、Dispatcher 超时重发」时 current_intent_id 已清空，重发为 no-op 成功而非 409。路由层另接受 `Idempotency-Key` 头（12 client 携带；DB 无列存储，仅作为重发信号，实际去重靠 (item_id, intent_id)）。
- **格子互斥（B1）**：`claim_item_for_intent(item_id, intent_id) -> bool`（untested 且 NULL → in_progress+claim，否则 False）；`release_item_for_intent` 仅 `current_intent_id==intent_id` 回退 untested（NULL 天然不放行，SQL `NULL = ?` 不命中）。
- **report_ready(conn, eid, policy=None) -> (bool, dict)**：`dict` 字段 = `uncovered_high`（高优先缺口，F11 排除后）、`depth_shortfall`、`summary`（coverage_summary，F11 排除分母）、`untriaged_findings`、`policy`。达标 = 无高优先缺口 + depth 达标 + `coverage_ratio ≥ target_coverage` + （require_all_findings_triaged ⇒ untriaged==0）。
- **`apply_audit_verdict` 签名与 skeleton §3 有偏差**：skeleton 写 `(conn, audit_id, *, verdict)`，本实现按 coverage spec §2 权威用 `(conn, eid, *, item_id, verdict, auditor, reason='sampling', depth_reached=None, note=None) -> audit_runs row`（创建 audit_runs + 落定 verdict 一步完成）。若 30/31 需要按 audit_id 确认，需在 21 补 `confirm_audit_run`（见 §7 未实现）。
- **`closure_rule(conn, eid, item) -> bool`**：True = 参与 report-ready 收敛口径（阻塞）；False = auto_created 目标项（不阻塞，F11）。接受 coverage_items 行或 item_id 字符串。
- **`reason_escalation_state(conn, eid, policy=None) -> bool`**：只读判定，计数落 `scheduler_state` key=**`reason_escalation:{eid}`**（JSON：`{"consecutive_failures", "finalize_rejected", "escalated"}`）。**30/40 写入该 key**，超 `reason_escalation.max_consecutive_failures`（默认3）或 `max_finalize_rejected`（默认3）或显式 `escalated=true` → 返回 True。

## 4. 路由清单（skeleton §2.3 + 扩展）

| 方法/路径 | 说明 |
|---|---|
| `GET /engagements/{id}/coverage` | 矩阵+热力图（§4.1 数据契约；A3 实时 priority；cells 含 partial/retest_round） |
| `GET /engagements/{id}/coverage/items` | 覆盖项列表（**裸 list**，12 client 期望） |
| `POST /engagements/{id}/coverage/items` | 人工播种（upsert，seed_source=human） |
| `PUT /engagements/{id}/coverage/items/{cid}` | 调整深度 / 强制校准（coverage_items 无 note 列，仅 depth_required/status） |
| `POST /engagements/{id}/coverage/items/{cid}/waive` | 人工豁免（B4：kind + 必填 reason） |
| `GET /engagements/{id}/coverage/gaps` | compute_gaps JSON（**裸 list**，priority 降序；query: threshold/exclude_in_progress/limit） |
| `POST /engagements/{id}/coverage/result` | explore 写回（12 client `write_coverage_result`；B1 校验 + C9 幂等 + Idempotency-Key 头） |
| `POST /engagements/{id}/coverage/items/{cid}/claim` | B1 认领（200 `{"claimed": bool}`；并发第二次 `claimed:false`，非 409） |
| `POST /engagements/{id}/coverage/items/{cid}/release` | B1 释放（owner 才回退；NULL 不放行） |
| `GET /engagements/{id}/coverage/audit` | audit_runs 历史（分页包装） |
| `POST /engagements/{id}/coverage/items/{cid}/audit` | 手动触发/确认（带 verdict → apply_audit_verdict；不带 → 建 pending audit_run） |
| `GET /engagements/{id}/coverage/export` | 覆盖矩阵导出（含 waivers/audits） |
| `POST /engagements/{id}/finalize` | **41 实现**（本路由不含） |

## 5. 给下游接口说明

### 给 30（dispatcher tasks · reason/explore）
- **reason 输入**：`GET /coverage/gaps?exclude_in_progress=true&limit=50` → 裸 list，priority 降序；每项 `{item_id, target_id, target_value, test_type_id, test_type_name, depth, priority}`。**必须 exclude_in_progress=true**（B1：in_progress 格已被认领）。
- **explore 派发前**：对 intent 引用每个格子 `POST /coverage/items/{cid}/claim` body `{"intent_id": ...}`；任一 `claimed:false` → 该 intent 不派发，下轮换格。
- **explore 写回**：`POST /coverage/result` body `{item_ids, depth_achieved, outcome, fact_id, intent_id, evidence_refs, tested_scope, partial}`，建议带 `Idempotency-Key` 头。**outcome=no_issue 必须声明 tested_scope**（C9：未声明 → 422 VALIDATION）。写回 409 `COVERAGE_ALREADY_COVERED` = 预期分支（他人认领/已测），写回作废 + release。
- **reason 升级**：连续校验失败/ finalize 被拒写 `scheduler_state` key `reason_escalation:{eid}`；`GET`（或调 21）`reason_escalation_state` 判定是否升级 needs_review。

### 给 40（dispatcher loop · reconcile）
- **启动 reconcile**：遗留 `coverage_items.current_intent_id` 认领但 intent 超时（>2×interval 无心跳）→ 调 `POST /coverage/items/{cid}/release` body `{"intent_id": <原认领>}`（**服务端唯一写者，不能直写 DB**）；release 仅 owner 回退，NULL 不放行——不会误清他人认领。也可批量 `GET /coverage/items` 拿 current_intent_id。
- 复测重建：finding `fixed` 后调 `PUT /coverage/items/{cid}` 或服务端 `rebuild_for_retest`（41/25 编排），复用原行 retest_round+1（A5）。

### 给 41（report finalize）
- finalize 前置校验：调 `report_ready(conn, eid, policy)`（policy 来自 engagement.scope_policy.coverage，缺省 `DEFAULT_COVERAGE_POLICY`）。未达标返回 `COVERAGE_POLICY_UNMET`（v2 §12 规则 18）；达标才置 completed。
- `report_ready` 明细供前端 tooltip：`uncovered_high`（未达标项列表）、`summary`（含 partial 计数）。
- **豁免绕过**：剩余未覆盖只能经 `POST /coverage/items/{cid}/waive`（人工+理由）后再提交。

### 给 25（graph conclude 编排）
- conclude 同事务：先 claim（如未认领）→ `write_coverage_result`（覆盖记账）+ 22 `create_finding`（agent 只能 open）。写回校验会拒绝未认领格（B1）。

## 6. 验收映射（dev-agents/21 §3 + coverage spec §5 逐条自查）

| 验收要点 | 实现/测试 |
|---|---|
| seed 目录行数 ≥25 | `test_seed_default_test_types`（28 行，幂等，enabled=1）+ 联调 `create_engagement→28` |
| compute_gaps 排序/limit/exclude_in_progress | `test_compute_gaps_sort_limit_exclude` |
| claim/release 互斥（并发二次 claim False） | `test_claim_release_mutex`（含非 owner 不放行、NULL 不放行） |
| 写回幂等（同 item+intent 不重复记账） | `test_write_coverage_result_claims_and_idempotent` + 路由层 Idempotency-Key 重发 |
| not_applicable 无 waiver 仍算缺口 | `test_not_applicable_outcome_only_suggests` + `test_waive_item_kind_reason_and_status` |
| report_ready 各策略分支（F11 auto_created 不阻塞、C8 escalation） | `test_report_ready_branches`、`test_report_ready_depth_shortfall`、`test_report_ready_f11_auto_created_not_blocking`、`test_report_ready_untriaged_findings_via_22`、`test_reason_escalation_state_c8` |
| spec §5.1 compute_gaps 与热力图同口径 | `test_compute_gaps_sort_limit_exclude` + `get_coverage_matrix` 同 `priority_score` |
| spec §5.2 COVERAGE_ALREADY_COVERED 单测 | `test_write_coverage_result_rejects_unclaimed_and_foreign`（未认领/他人认领/跨 engagement） |
| spec §5.3 explore 无 coverage → 校验失败 | 路由层 `POST /result` item_ids min_length=1（422）；conclude 校验在 25 |
| spec §5.4 finalize 未达标 → COVERAGE_POLICY_UNMET | `report_ready` 返回 ok=False；41 据此返回 409（端点属 41） |
| spec §5.5 items+records 同事务 | 服务只写、路由单 commit；`write_coverage_result` 同事务 |
| spec §5.7 F11 闭环 | `test_report_ready_f11_auto_created_not_blocking`（含关闭 F11 对照） |
| spec §5.8 F3 抽样复核 | `test_sample_audit_discrepancy_and_apply_verdict`、`test_sample_audit_sampling_and_covered_matches` |
| spec §5.10 A3 口径统一 | `test_sample_audit_a3_same_priority_engine`（缓存列污染不影响） |
| spec §5.11 A5 复测重建 | `test_rebuild_for_retest_reuses_row` |
| spec §5.12 B1 格子互斥 | `test_claim_release_mutex` + 路由 claim/release |
| spec §5.13 C8 reason 升级 | `test_reason_escalation_state_c8` |
| spec §5.14 C9 部分覆盖 | `test_write_coverage_result_partial` + `test_write_coverage_result_no_issue_requires_tested_scope` |
| spec §5.15 D5 criticality | `test_infer_criticality_d5` |

## 7. 未实现 / 待定

- **`seed_from_discovery`**（bootstrap 播种覆盖项）未实现——依赖 20 的 `ensure_target`（scope 校验 + 建 target）。**30/40** 播种时用本包 `upsert_coverage_item(conn, eid, target_id, test_type_id, depth, seed_source=...)` 为原语（20 已解析 target）。建议阶段 2 联调时补一个薄封装。
- **`confirm_audit_run`**（按 audit_id 确认已建的 pending audit_run）未实现——`apply_audit_verdict` 是「建 + 落定」一步式。若 30/31 需要两阶段（先派发 audit、后确认），需补该函数（待确认需求）。
- **coverage_items 无 note 列**：`PUT /items/{cid}` 仅支持 depth_required/status，「强制校准留 note」暂无法持久化（DDL 无列）。需 DDL 加列或另建 audit 记录。
- **`seed_from_discovery` 的 FTS 同步**（fts_coverage）未实现（spec §1 可选）。
- **`rebuild_for_retest` 由谁触发**：finding fixed 后由 41/25 编排调用（不在本包路由暴露）。

## 8. 对他人包的改动（集成修复）

- **`cairn/tests/test_scope.py` `test_target_delete_referenced_by_coverage_409`**：create_engagement 现已预置默认目录（含 tt_web_xss），该测试手工 INSERT 同 id 撞主键。已把手工 test_type 改为非默认目录 slug `tt_web_custom`（含注释）。**20/orchestrator 知悉**：这是 21 seed 激活后的必然适配，非行为变更。

## 9. 需要登记的对齐问题（供 orchestrator）

1. **[潜在 DDL 缺陷] `targets.id` 全局 PK + `engagement_counters`（kind='target'）作用域计数器**：两个 engagement 都会各自生成 `t-001`，全局 PK 会撞（测试中实测 `UNIQUE constraint failed: targets.id`）。当前实现绕开（跨 engagement 测试不建双 target）。DDL §4.1 与 §2 targets PK 存在口径不一致，建议 DDL 改为 `(engagement_id, id)` 复合 PK 或 target 用全局计数器。**需 DDL 修订（非本包）**。
2. **[phase2] `coverage/result` 端点**：12 client 已按 `POST /engagements/{eid}/coverage/result` 假设实现，本包已对齐（见 12 交接物 §3 映射表）。
3. **report_ready untriaged 读 22**：`from .findings import triaged` 已解析（22 已提供，`services/findings.py` 存在且可导入）；未提供时回落本地等价 SQL（import 守卫 + TODO）。**22 已提供，非回落分支**。

## 10. 自测结果

- `uv run --project cairn pytest -q` → **259 passed**（含本包 26 个 + 全量回归；修复 20 的 1 个 seed 碰撞测试后全绿）。
- `create_engagement` 联调 → test_types 28 行（seed 集成生效）。
- dispatch yaml 三文件解析 OK（CLAUDE.md 验证命令）。
