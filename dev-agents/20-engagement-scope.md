# Agent 20 — 授权范围子域（Engagement / Target / Scope Guard）

> 阶段 1 · 依赖 10 完成。你的 router 挂在 app.py 注册点；与 21 有一个明确接口（见 §2 契约 D）。

## 0. 开工前必读
1. `CLAUDE.md`（不变量 3/7）
2. `docs/rule-registry.md`（A2/B2/C1/F11 等）
3. `docs/database-ddl-draft.md` §2（engagements/targets + scope_policy JSON）、§4.1（ID）
4. `docs/architecture-research-report-pentest-v2.md` §4.1/§4.2/§4.12、§6、§8.9、§12 规则 1
5. `docs/backend-module-skeleton.md` §2.2（授权范围路由）、§3 前 5 个签名
6. `docs/human-workflow-guide.md` §1/§2（状态机与 targets 删除 gate）
7. `docs/exploration-graph-spec.md` §3（B5 冻结函数签名，25 实现）

## 1. 交付范围
```
cairn/src/cairn/server/services/scope.py      # create_engagement / transition_status / check_engagement_writable / check_scope_allowed / check_kill_switch / expire_engagements
cairn/src/cairn/server/routers/engagements.py # 生命周期 + kill + finalize 占位（finalize 由 41 填）
cairn/src/cairn/server/routers/targets.py     # 范围 CRUD
cairn/tests/test_scope.py
```

## 2. 必须满足的契约
- **A. Engagement 状态机**：`planning→active→paused→completed→archived`（DDL CHECK）。`transition_status` 校验：planning→active 要求 scope 非空 + 窗口合法 + kill off；`completed→active` 需 `retest=true`（保留图与漏洞库，A2）；archived 单向不可逆。非法转换 → 409 `ENGAGEMENT_INVALID_STATE`。
- **B. 窗口**：`authorized_start_at/end_at`（ISO8601 UTC）。active 时 `expire_engagements()`（定时任务，见 v2 §9.1）把窗口到期的置 paused（规则 B5：清 intent claim + reason lease——调 25 的 `services/graph.freeze_project_leases(conn, pid)`，签名见 `exploration-graph-spec.md` §3）。窗口外派发 → 403 `OUT_OF_AUTHORIZATION_WINDOW`。
- **C. Scope Guard**（v2 §12 规则 1，**不可跳过的 gate**）：`check_scope_allowed(eid, value)` 判定 target_value（域名/IP/CIDR/URL 正则，§7.4）∈ authorized 集合；`prohibited` 命中 → 403 `SCOPE_DENIED` + 审计（记 finding_history 或 event），**禁止 fallback 放行**。`check_kill_switch`（全局 settings.global_kill_switch + 项目 kill_switch）→ 423 `KILL_SWITCH_ON`。`check_engagement_writable` → 非 active 时 403/409。
- **D. 创建流程**（与 21 的接口）：`create_engagement` 写 engagements 行 + **调用 `services.coverage.seed_default_test_types(conn, eid)`**（接口由 21 实现，契约：签名 `seed_default_test_types(conn, eid) -> None`，按 `coverage-engine-implementation-spec.md` §1.1 目录写入 test_types，enabled=1）。**此函数若 21 尚未就绪，你仍须先按契约写好调用点**（导入失败则留 TODO 注释 + 交接物说明，不阻塞其他交付）。
- **E. targets CRUD**：`GET/POST /engagements/{id}/targets`（T）、`PUT/DELETE /engagements/{id}/targets/{tid}`（H，鉴权标注见 skeleton §2.2）。登记校验格式 + `scope_status` 枚举。`UNIQUE(engagement_id, value)` 冲突 → 409 `COVERAGE_DUP` 不适用，用 409 + 明细。**删除应用层 gate**：DELETE 前检查 findings/coverage_items 是否引用该 target，未结算 → 409 并列出引用（human-workflow §2）；DB 层 `findings.target_id` 是 CASCADE，勿改 RESTRICT。
- **F. 路由**：`GET/POST /engagements`、`GET/PUT/DELETE /engagements/{id}`、`PUT /engagements/{id}/status`（H）、`POST /engagements/{id}/kill`（H，立即 SIGKILL 语义通知 40/11）。finalize 路由只留 501 占位（41 填）。

## 3. 验收标准
1. 状态机全路径测试：合法/非法转换、retest=true 特例、archived 单向。
2. 窗口测试：到期自动 pause（`expire_engagements` 调用后 status 变化）。
3. scope guard：prohibited → SCOPE_DENIED 且无 fallback；auto_created target（findings 写回建，由 22 用）已在 scope 校验后创建（`check_scope_allowed` 支持值→新 target）。
4. targets 删除：被引用 → 409 列出引用；未引用 → 删除成功且不破坏级联。
5. 创建 engagement 后 test_types 已播种（若 21 就绪则断言行数 ≥1）。

## 4. 硬约束
- 只做 scope 子域；不碰 coverage_items/findings 的写逻辑（引用接口调用除外）。
- 状态迁移的审计留痕（finding_history 或 task_events）由调用方或本服务按文档要求落，不静默。
- `engagement_counters` kind 含 `target`/`finding_history`——t-### 与 fh-### 生成走 10 的 `next_id`。

## 5. 交接物
写 `dev-agents/notes/20-engagement-scope.md`：状态机/守卫实现说明、seed_default_test_types 契约确认（21 是否已提供）、删除 gate 行为、给 41/40 的接口清单。
