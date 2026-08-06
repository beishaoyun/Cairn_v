# 22-findings 交接物

- 完成 Agent：22-findings  日期：2026-08-06
- 阶段：Phase 1 · 依赖 10（server 基座）+ 20（scope guard）+ 23（capture derive）
- 交付：`cairn/src/cairn/server/services/findings.py`、`cairn/src/cairn/server/routers/findings.py`、`cairn/tests/test_findings.py`

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `services/findings.py` | `dedup_key` / `resolve_target` / `create_finding` / `transition_finding` / `attach_evidence` / `add_http_evidence` / `add_command_evidence` / `link_finding_traffic` / `triaged` / `apply_verify_runs` / `bump_reverify` / `record_retest_confirmation` / `retest_pass_count` | 漏洞子域：去重 / B1 target / 状态机+审计 / 证据 / verify 落定 / 复测账本 / closed 门槛 / triaged |
| `routers/findings.py` | `router`（prefix `/engagements/{engagement_id}/findings`） | skeleton §2.5 全量：CRUD/evidence/http/commands/traffic/verify/retest/replay/history/过滤/export 占位 |
| `tests/test_findings.py` | 33 个测试 | §3 五项验收全过 |

## 2. 验收自测（`uv run pytest tests/test_findings.py` → **33 passed**）

1. **状态机全路径 + 权限 gate**：open→pending_verify→verified；pending_verify→pending_false_positive→false_positive（人工终态）；needs_more→reverify_count+1→≤max 回 open / >max 升 needs_review；`fixed` 触发 retest_round+1+retest_pass 归零；**agent 置 fixed/closed/false_positive/accepted → 403**；非法相邻（verified→pending_verify by agent）→ 409；closed 终态不可流转 → 409；人工可任意态（可重开 false_positive）。
2. **去重（B3）**：规范化 title（NFKC+casefold+空白折叠+尾部标点去除）同 target 第二次建 → 409 `FINDING_DUP`（detail 带已有 `finding_id`），不重复建单；客户端「命中已有→追加证据」路径可用。
3. **verify 三分支（F1/F6）**：confirmed → verified + `verified_severity` + `severity`（双轨，agent_severity 保留）；rejected → pending_false_positive（非终态）；needs_more → reverify_count+1，超 `verify_policy.max_reverify`（读 engagement.scope_policy，默认 3）→ needs_review 升级人工停止循环；verify 只写 verdict 相关字段不改其他。
4. **复测账本（C10/A2）幂等 + closed 门槛 403**：`UNIQUE(finding_id,retest_round,kind)` 同轮同类型幂等；`record_retest_confirmation` 刷新 retest_pass；closed 需 `retest_pass>=2` 且 ≥2 种类型且**必须含 replay**（HTTP 类确定性重放 / 非 HTTP 类命令重放）→ 未过 403 `SCOPE_DENIED`（detail 含 kinds/http_class/missing）；轮次递增旧轮确认不继承。
5. **triaged 口径**：open/pending_verify/pending_false_positive/needs_review 计未分诊；verified/fixed/false_positive/accepted/closed 不算。

## 3. triaged() 确认（给 21 report_ready）

- 签名：`triaged(conn, eid) -> int`（skeleton §3 对齐）。
- 口径：`SELECT COUNT(*) WHERE engagement_id=? AND status IN ('open','pending_verify','pending_false_positive','needs_review')`。**verified 不算**（已分诊，不阻塞 finalize）。
- 21 的 `report_ready` 可直接调用；服务函数无状态，短事务。

## 4. 状态机实现表

| 源状态 | 自动/校验路径（actor≠human）可达 | 人工（actor='human'）可达 | 副效应 |
|---|---|---|---|
| open | pending_verify | 任意非 closed | — |
| pending_verify | verified / pending_false_positive / needs_review / open | 任意非 closed | needs_more 循环由 `apply_verify_runs` 走 `bump_reverify` |
| pending_false_positive | false_positive / open / verified | 任意非 closed | — |
| verified | fixed / needs_review / open / accepted | 任意非 closed | — |
| needs_review | open / verified / fixed / pending_false_positive / false_positive / accepted | 任意非 closed | — |
| fixed | closed / open / verified | 任意非 closed | fixed→ fixed_at + retest_round+1 + retest_pass=0 |
| false_positive | open | 任意非 closed | — |
| accepted | closed | 任意非 closed | — |
| closed | ∅（终态） | ∅ | closed→ closed_at |

- 权限：`to_status ∈ {fixed,closed,false_positive,accepted}` 且 actor≠'human' → 403；`to_status=closed` 未过复测门槛 → 403；closed 源 → 409。
- 每条流转写 `finding_history`（actor 必填）。

## 5. derive_http 依赖状态（23 是否已提供：**已提供**）

- 23 已交付 `services/capture.py`，提供 `derive_http_from_capture(conn, fid, traffic_id, *, traffic_root=None) -> dict` 与 `link_finding_traffic`。本模块 import 守卫已自动接线（`_derive_http`/`_capture_link_traffic` 非 None）。
- **互递归安全已确认**：本模块 `add_http_evidence`（source='captured'）先登记行再调 23 derive；23 的 derive 内部以 `(fid, traffic_id, source='captured')` dedup 打断环，无死循环。
- **C2 分流**：`create_finding` 中 agent 上报的 http[]（即使标注 captured）统一以 `source='agent_typed'` 登记（语义注释），不占用 captured 去重槽——23 的 derive（30 派发时调用）随后可派生真相行；trigger 关联由 `traffic_ids` 建立。若直接调 `POST /findings/{fid}/http`（source='captured'+traffic_id）仍会触发 23 derive 调用点。

## 6. 给下游的接口

- **21（report_ready）**：`findings.triaged(conn, eid) -> int`；`findings.retest_pass_count(conn, fid) -> dict`（当前轮账本明细，见 §8 签名偏离）。
- **23（capture 派生）**：`services.findings.add_http_evidence(conn, fid, *, http_obj) -> dict`（23 的 derive 经它登记）；`services.findings.link_finding_traffic` 现在委托给 23 的 `capture.link_finding_traffic`。
- **30（verify 落定 / replay / retest / 写回）**：
  - `POST /engagements/{id}/findings/{fid}/verify` body `{verdict, verified_severity, reason, verified_traffic_ids, stage, independence, task_run_id, actor}` → `services.findings.apply_verify_runs(conn, fid, *, vr)`（三分支落定）。
  - 写回：`POST /engagements/{id}/findings` body（actor='agent'，只能 open；`asset`/`target_id` 二选一，未知资产走 20 scope guard auto_created）。
  - 复测：`POST /findings/{fid}/retest`（kind ∈ replay/verify/human）；`POST /findings/{fid}/replay`（登记 queued 行，执行引擎归 30）。
  - 状态升级：`PUT /findings/{fid}` body `{status, note, actor}`；**30 不得置 fixed/closed/false_positive/accepted（403）**，closed 另受复测门槛。
- **41（报告证据）**：`GET /findings/{fid}` 返回 evidence/http_evidence/command_evidence/traffic_links/retest 明细；`GET /findings/{fid}/history` 审计流；`GET /findings/export?format=json|csv`（占位，41 可接管）。
- **20（scope guard）**：`resolve_target` 委托 `scope.check_scope_allowed`（prohibited→403；authorized 包含命中→其 auto_created target；未命中→403）。

## 7. 未实现 / 待定

- **verify 派发（30）**、**replay 引擎（30）**、**retest explore 派发（30）**、**coverage 重建（21）**——本包只落状态机与账本，不派发。
- **捕获/流量索引（23）**——`traffic_entries` 索引、digest、代理回写均归 23；本包只消费 `traffic_id` 关联与 `derive_http_from_capture`。
- **`GET /engagements/{id}/traffic` / `GET traffic/{tid}` / `POST traffic`**：归 23 的 traffic.py 路由，本包未实现。
- **evidence 字节上传**：采用 JSON+base64（未引入 python-multipart，遵守「不引入未明示依赖」）；白名单 image/*、text/*、application/pdf；路径防穿越（`..`/绝对路径净化）。如后续引入 multipart 可改为 UploadFile。
- **`/stats` 端点**：留给 42/24，本包未建（避免路由冲突）。
- **FTS5 findings 同步**：fts_findings 由 DDL 建表，本包未写同步触发器（与 24/progress 同批，可由 41 或后续补）。

## 8. 契约偏离 / 注意

- **`retest_pass_count` 返回 dict**（`{retest_round, count, details:[{kind,note,actor,created_at}]}`），偏离 skeleton §3 标注的 `-> int`——任务契约 F 要求「返回当前轮账本明细」。调用方用 `["count"]` 取行数。
- **`closed` 门槛错误码用 `SCOPE_DENIED`（403）**——错误码表无专门的「门槛」码；语义按规则 26/31 明确。如后续希望区分可新增错误码（需先改 rule-registry/errors）。
- **`resolve_target` 子域/CIDR 包含命中的最终语义以 20 的 `check_scope_allowed` 为准**：子域命中父域时会 auto_created 具体子域 target（F11，不阻塞收敛），而非复用父域 target。
- **20 未交付时的占位**：`resolve_target` 在 `_scope_check` 缺失时直接 auto_created 放行（本仓库 20 已交付，占位未触发，仅作并行期守卫）。
- **full suite 现状**：`tests/test_findings.py` 33 全过；全量 254 passed + 5 failed 均在 **21 的 test_coverage.py（4）与 20 的 test_scope.py（1）**——test_type_id(`tt_web_xss`) 全局 PK 与 21 `seed_default_test_types` 播种冲突等，属 20/21 并行期联调问题，与本包无关（本包未触碰 coverage/scope 代码与测试）。
