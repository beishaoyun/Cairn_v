# Agent 22 — 漏洞子域（Findings / Evidence / Verify 落定）

> 阶段 1 · 依赖 10。你的 `triaged()` 是 21 的 report_ready 依赖；verify_runs 落定逻辑被 30 的 verify 任务调用（经 12 客户端）。

## 0. 开工前必读
1. `CLAUDE.md`（不变量 3/4/7）
2. `docs/database-ddl-draft.md` §5（findings/evidence/http_evidence/history/retest_confirmations）、§9.2（verify_runs）、§9.3（replay_runs）、§4.1（ID）
3. `docs/capture-verify-progress-spec.md` §4（verify 触发/派发/输出契约）、§5（状态机）、§6（复测多确认）
4. `docs/backend-module-skeleton.md` §2.5（漏洞闭环路由）、§3 findings 服务签名
5. `docs/architecture-research-report-pentest-v2.md` §4.9、§8.10、§12 规则 4/18/26/28-36
6. `docs/human-workflow-guide.md` §4/§5（状态机/复测签收门槛）
7. `docs/prompts-pentest-templates.md` §4（verify 两阶段契约）、§8（校验器对照）

## 1. 交付范围
```
cairn/src/cairn/server/services/findings.py    # dedup_key / resolve_target / create_finding / transition_finding / attach_evidence / add_http_evidence / triaged / apply_verify_runs / bump_reverify / record_retest_confirmation / retest_pass_count
cairn/src/cairn/server/routers/findings.py     # 漏洞 CRUD/状态流转/证据/http/commands/verify/replay/traffic 关联/history
cairn/tests/test_findings.py
```

## 2. 必须满足的契约
- **A. 状态机**：`open→pending_verify→verified|pending_false_positive|needs_review|fixed|false_positive|accepted|closed`（DDL CHECK）。`create_finding(actor='agent')` **只能建 open**；人工（actor='human'）可任意态。`verified` 由 verify confirmed 自动置（非仅人工，但 severity 取 `verified_severity` 双轨）；**`fixed/closed/false_positive/accepted` 仅人工**（v2 §6.2/规则 4）。非法流转 → 409/403。
- **B. 去重（B3）**：`dedup_key(engagement_id, target_id, normalized_title)`；`(engagement_id, target_id, title)` 唯一索引。重复 → 追加证据不重复建单（409 `FINDING_DUP` 语义由客户端处理成「命中已有」）。title 规范化（URL 规范化 B3）。
- **C. 未知资产（B1）**：`resolve_target(conn, eid, asset, scope)`——asset 不在 targets 时先 `check_scope_allowed` 校验，通过则 **auto_created** target（`auto_created=1`），再建 finding。禁止 NOT NULL 冲突。
- **D. verify 落定（F1/F6）**：`apply_verify_runs(fid, vr)`：verdict=confirmed → `verified` + `verified_severity`；rejected → `pending_false_positive`（**非终态**）；needs_more_evidence → `reverify_count+1`，超 `verify_policy.max_reverify` → `needs_review`（升级人工，停止自动循环）。`bump_reverify` 返回是否超限。verify 只写 verdict 相关字段，不改 finding 其他内容。
- **E. 证据**：`finding_evidence`（文件白名单 image/text/pdf）、`finding_http_evidence`（source ∈ captured/agent_typed；**captured 由 23 的 capture 服务派生**，你只登记与关联）、`finding_command_evidence`。`POST /findings/{fid}/evidence` 鉴权 H；http/commands 鉴权 T。
- **F. 复测账本（C10/A2）**：`record_retest_confirmation(fid, kind, note, actor)`——kind ∈ {replay, verify, human}，`UNIQUE(finding_id, retest_round, kind)` 幂等（同轮同类型重复不计）；刷新 `retest_pass`（当前轮行数）。`retest_pass_count(fid)` 返回当前轮账本明细。`closed` 前置门槛：`retest_pass>=2` 且 ≥2 类型，HTTP 类必须含 replay（规则 26/31），未过 → 403（在 `transition_finding` 里实现 gate）。
- **G. 路由**：skeleton §2.5 全部（findings CRUD/evidence/http/traffic/commands/verify/replay/history/过滤/export stats 占位给 41/42）。`GET /findings/{fid}/history` 返回 finding_history 审计流（含 actor/from/to）。

## 3. 验收标准
1. 状态机全路径（capture spec §5 图）+ 权限 gate（agent 不能置 fixed/closed）。
2. 去重：同 target+规范化 title 第二次建 → 命中已有并追加证据。
3. verify 三分支落定 + max_reverify 升级（对照 TV-20 语义）。
4. 复测账本幂等 + closed 门槛 403（对照 TV-31/TV-44/TV-46 语义）。
5. `triaged()` 口径：open/pending_verify/pending_false_positive/needs_review 计未分诊；verified 不算。

## 4. 硬约束
- 证据字节本身不落 DB（落文件，DB 只存引用路径 + mime/size）。
- **不实现捕获/流量索引**（23 负责）；`derive_http_from_capture` 由 23 提供，你只定义调用点。
- 不写 verify 任务的**派发**（那是 30）；你只落 verdict 结果与状态机。
- finding 状态机变更必须写 `finding_history`（每条流转一行，actor 必填）。

## 5. 交接物
写 `dev-agents/notes/22-findings.md`：状态机实现表、triaged 口径、closed 门槛 gate 行为、给 21（report_ready）/23（capture 派生）/30（verify 落定）/41（报告证据）的接口。
