# Agent 21 — 覆盖度引擎（Coverage Engine）

> 阶段 1 · 依赖 10。**核心差异化模块**。你提供的 `seed_default_test_types` 是 20 的依赖，务必先实现并通告。

## 0. 开工前必读
1. `CLAUDE.md`（不变量 5/7）
2. `docs/coverage-engine-implementation-spec.md` —— **全文（你的规格）**：§1 DDL、§1.1 默认测试项目录、§2 伪代码、§3 输出契约、§4 热力图、§5 验收
3. `docs/rule-registry.md`（A1/A3/A5/B1/B4/C9/F3/F11）
4. `docs/database-ddl-draft.md` §3（test_types/coverage_items/coverage_records/waivers）、§4.1（ID）
5. `docs/backend-module-skeleton.md` §2.3（覆盖路由）、§3 coverage 服务签名
6. `docs/architecture-research-report-pentest-v2.md` §4.13、§8.13、§12 规则 13
7. `docs/human-workflow-guide.md` §3（豁免/不适用/校准）

## 1. 交付范围
```
cairn/src/cairn/server/services/coverage.py   # seed_default_test_types / compute_gaps / coverage_summary / report_ready / upsert_coverage_item / claim_item_for_intent / release_item_for_intent / write_coverage_result / waive_item / rebuild_for_retest / sample_audit / apply_audit_verdict / closure_rule / reason_escalation_state
cairn/src/cairn/server/routers/coverage.py    # 矩阵/缺口/豁免/播种/抽样审计
cairn/tests/test_coverage.py
```

## 2. 必须满足的契约
- **A. `seed_default_test_types(conn, eid)`**：按 coverage spec §1.1 目录表写入 test_types（id=`tt_<slug>`、enabled=1、risk/default_depth 用表内默认）。**20 会在 create_engagement 时调用你**；接口签名固定 `(conn, eid) -> None`，先实现并写进交接物。
- **B. priority 实时口径（A3）**：`priority_score(asset_criticality, test_type_risk, depth)` 按 coverage spec §2 伪代码；`compute_gaps` 查询时**实时计算**，`coverage_items.priority_score` 仅展示缓存、不作为排序依据。`compute_gaps(conn, eid, *, threshold, exclude_in_progress=False, limit=50)`：缺口 = untested 项按 priority 降序，limit 防 prompt 撑爆（30 用 `exclude_in_progress=True`）。
- **C. 格子互斥（B1）**：`claim_item_for_intent(item_id, intent_id) -> bool`（置 `in_progress`+`current_intent_id`，已被认领返回 False）；`release_item_for_intent` **仅 current_intent_id==intent_id 才回退 untested**（NULL 不放行）。40 的重启 reconcile 依赖此语义。
- **D. 写回（C9）**：`write_coverage_result(...)` 校验 `covered_items ∈ engagement` 且为本次 intent 认领的格子（`current_intent_id != intent_id` 的格子**不放行**）；写 coverage_records（含 tested_scope/partial）+ 更新 item 状态；**幂等**：`(item_id, intent_id)` 去重，配 writeback_retries（12 提供 tuning）。outcome=`not_applicable` **只建议**，不置 item `not_applicable`（需人工建 waiver，B4 语义）。
- **E. report_ready（收敛判定）**：按 coverage spec §2 伪代码 + scope_policy.coverage 策略（min_priority_threshold/target_coverage/require_all_findings_triaged/require_depth/auto_created_closure F11/reason_escalation C8）。返回 `(bool, dict)` 明细。**untriaged 计数用 22 的 `triaged()`**（verified 不算阻塞）。
- **F. 豁免**：`waive_item(item_id, kind, reason, by)`——kind ∈ {not_applicable, out_of_scope, risk_accepted}，reason 必填；`not_applicable` 必须建 waiver 才置 item 状态。
- **G. 抽样审计（F3）**：`sample_audit` 按 audit_sampling 策略抽高优先已测格（实时 priority 口径）+ discrepancy_trigger（声称 finding_created 却无 finding 强制审）；`apply_audit_verdict`：`coverage_discrepancy` → item 回退 untested + 缺口重排。
- **H. 路由**：skeleton §2.3 全部（coverage 矩阵/items/gaps/audit/finalize 占位由 41 填）。`GET /coverage` 返回热力图数据（§4 数据契约）；`GET /coverage/gaps` 返回 compute_gaps JSON（priority 降序）。

## 3. 验收标准
1. `pytest test_coverage.py` 覆盖：seed 目录行数 ≥25；compute_gaps 排序/limit/exclude_in_progress；claim/release 互斥（并发两次 claim 第二次 False）；写回幂等（同 item+intent 重复写不重复记账）；not_applicable 无 waiver 仍算缺口；report_ready 各策略分支（含 F11 auto_created 不阻塞、C8 escalation）。
2. 与 20 联调：create_engagement 后 test_types 已播种（阶段 1 末联调）。
3. 对照 coverage spec §5 验收要点逐条自查，写入交接物。

## 4. 硬约束
- **不碰 findings 的写**（22 负责）；`report_ready` 的 triaged 计数只**读** 22 的服务函数。
- item 状态枚举与 DDL CHECK 逐字符一致；retest 重建走 A5（复用原行 retest_round+1，不新建行，UNIQUE 下不冲突）。
- 热力图 §4 的**前端**是 42 的活；你只提供数据契约（§4.1）。

## 5. 交接物
写 `dev-agents/notes/21-coverage-engine.md`：seed 实现确认、compute_gaps 签名、写回幂等键、report_ready 明细字段、给 30（gaps 消费）/40（reconcile）/41（finalize 校验）的接口说明。
