# default-prompts 交接物（真实 LLM prompt 模板组）

- 完成 Agent：default-prompts  日期：2026-08-06
- 依据：`docs/prompts-pentest-templates.md` §1-§8（真实 LLM 模板规格）+ `dispatcher/tasks/common.py` 校验器 + 各 `build_*_prompt` 实际渲染占位符 + `docs/coverage-engine-implementation-spec.md` §3。
- 交付：`cairn/src/cairn/dispatcher/prompts/default/*.md`（9 个，与 mock 组同名）+ `cairn/tests/test_default_prompts.py`（10 用例全绿）。
- 未 git commit。

---

## 1. 文件清单与输出契约摘要

| 模板 | 校验器 | 输出契约（data 内字段） |
|---|---|---|
| `bootstrap.md` | `validate_bootstrap_payload` | `fact.description`(必填) + `sweep_complete` + `discoveries[]`(target 必填/port/service) + `coverage.outcome`(固定 no_issue)。**禁 `complete`** |
| `bootstrap_conclude.md` | `validate_bootstrap_payload` | 同 bootstrap（静态模板，无占位符） |
| `reason.md` | `validate_reason_payload` | `intents[]`(from⊆合法 fact/description/coverage_item_ids⊆gaps) **或** `coverage.recommend_finalize=true`+`waivers[]`；高优先缺口存在 → 必出 intent 或 finalize（收敛硬约束）。**禁 `complete`** |
| `explore.md` | `validate_explore_payload` | `description`(必填) + `findings[]`(severity/cvss/cwe/asset/evidence_refs 相对路径/traffic_ids/http[]/commands[]) + `coverage`(covered_items⊆认领/depth_achieved/outcome/tested_scope.partial)。**禁 `complete`** |
| `explore_conclude.md` | `validate_explore_payload` | 与 explore 同构（findings 可选，coverage 必填） |
| `verify_blind.md` | `validate_verify_blind_payload` | `observations[]`(必填可空：vuln/severity/traffic_id/basis) + `traffic_note` |
| `verify_comparison.md` | `validate_verify_compare_payload` | `stage="comparison"` + `verdict`∈{confirmed,rejected,needs_more_evidence} + `verified_severity` + `reason`(非空) + `verified_traffic_ids`⊆流量 + `http_mismatch`(bool) + `suggested_action` |
| `audit.md` | `validate_explore_payload` + 自定义 `verdict` | explore 同构 + `verdict`∈{match,coverage_discrepancy}（audit.py 读取，缺省按 coverage.outcome 推断） |
| `replay.md` | `validate_replay_result` | `result`∈{remediated,unchanged,ambiguous,error} + `matched_original`(非负整数)。确定性引擎结果契约，非 LLM 输出 |

**关键点**：所有模板强调「只返回一个原始 JSON 对象」（严格 JSON，无前后缀杂文）；`complete` 字段一律被拒（黄金不变量 5，bootstrap 用 `sweep_complete` 表「初探完成」）。

## 2. 占位符表（与任务代码渲染一致）

| 模板 | 占位符 | 来源 |
|---|---|---|
| `bootstrap.md` | `{origin}` `{goal}` `{hints}` `{scope}` | `tasks/bootstrap.py::build_bootstrap_prompt` |
| `bootstrap_conclude.md` | （无） | `build_bootstrap_conclude_prompt`（静态） |
| `reason.md` | `{graph_yaml}` `{gaps}` `{scope}` | `tasks/reason.py::build_reason_prompt` |
| `explore.md` | `{graph_yaml}` `{intent_id}` `{intent_description}` `{coverage_context}` `{traffic_ids}` `{scope}` | `tasks/explore.py::build_explore_prompt`（`traffic_ids`=traffic_candidates） |
| `explore_conclude.md` | `{intent_id}` `{intent_description}` `{coverage_context}` | `build_explore_conclude_prompt` |
| `verify_blind.md` | `{traffic_digest}` `{scope}` | `tasks/verify.py::build_verify_blind_prompt` |
| `verify_comparison.md` | `{observations}` `{finding}` `{traffic_digest}` `{scope}` | `build_verify_compare_prompt` |
| `audit.md` | `{item_id}` `{target_value}` `{target_id}` `{test_type_name}` `{test_type_id}` `{depth_required}` `{status}` `{scope}` | `tasks/audit.py::build_audit_prompt`（item 字段） |
| `replay.md` | `{trigger_traffic_id}` `{variants}` `{scope}` | `replay/engine.py`（引擎输入上下文） |

> 注：mock 组模板用简化占位符（如 `{target}`×`{test_type}`）；default 组以**代码实际渲染**为准（audit 拆为 `{target_value}`/`{test_type_name}` 等 8 个）。模板 JSON 示例中的字面花括号（`{"accepted":...}`）不是占位符。

## 3. 与校验器的对照结论

- **bootstrap**：`fact.description` 非空 / `discoveries[].target` 必填 / `coverage.outcome` ∈ 枚举 / `sweep_complete` 对象或布尔 → 模板逐条写明。
- **reason**：`intents[].from` 非空数组且不含 `goal` / `coverage_item_ids` ⊆ gaps / `waivers[].kind` ∈ {not_applicable,out_of_scope,risk_accepted} / 收敛硬约束（高优先缺口存在 → intents 或 finalize）→ 模板写入规则，并标注「waivers 仅建议，人工批准才生效」。
- **explore/audit**：`findings[]` 白名单（severity/cvss_score∈[0,10]/cwe_id=CWE-\d+/asset/evidence_refs 相对路径/http method+绝对 url+status/commands.command 必填）+ `coverage`(covered_items⊆认领/depth/outcome，findings 非空→outcome=finding_created，no_issue→tested_scope) → 模板字段说明逐一对应。
- **verify 两阶段**：blind 只喂 digest+scope（防锚定），`observations` 必填可空；comparison 喂 observations+finding+digest，`stage=comparison`/`verdict`/`verified_severity`/`reason`/`verified_traffic_ids`/`http_mismatch`/`suggested_action` → 模板两阶段分文件，字段与校验器逐一吻合。
- **replay**：`result`/`matched_original` 与 `validate_replay_result` 完全一致。

## 4. 约束注入（已内嵌到每个模板）

- **授权范围**：只允许触碰 `{scope}` 声明的 authorized 目标；prohibited 目标严禁任何连接/扫描/探测。
- **禁止越界/DoS/破坏性操作**：全部 9 个模板均有（含两个 conclude 收尾模板）。
- **C5 证据纪律**：explore 明确「证据引用必须来自 `{traffic_ids}` 候选列表（C5：无法自查捕获索引）」；Web 类 `traffic_ids`+`http[]` 语义注释、非 HTTP 类 `commands[]` 真实回显；捕获字节为准，编造 → `http_mismatch`。

## 5. 验证

```bash
uv run --project cairn pytest cairn/tests/test_default_prompts.py -v   # 10 passed
uv run --project cairn pytest cairn/tests/test_dispatcher_config.py cairn/tests/test_tasks.py -q  # 91 passed（无回归）
```

`test_default_prompts.py` 覆盖：文件齐全非空 / 占位符集合⊆预期（与 build_* 一致）/ 样例上下文渲染无残留 / 渲染含校验器关键字段 / 严格 JSON 提示 + 无 mock 标记 + 无 `"complete":` 字段 / 授权边界约束 + C5 / `prompt_group` 默认 `default` 且 `dispatch.example.yaml` 显式钉住 / default 组与 mock 组同名 / `build_*_prompt` 加载路径样例渲染。

## 6. 未做项 / 留给下游

- **加载机制未改**：`prompts/default/*.md` 目前是权威模板文档，运行时 prompt 由 `tasks/*.py` 的 `build_*_prompt` 内联渲染（与 mock 组同构）。若后续要「从文件加载模板」，需给 Dispatcher 加一个 `prompts/{prompt_group}/<task>.md` 加载器，并保证占位符集合与本交接物 §2 一致——本组文件即目标内容。
- **audit `verdict`**：`audit.py` 用 `validate_explore_payload` 校验后读 `data.verdict`（无则按 `coverage.outcome` 推断）。模板已要求显式输出 `verdict`∈{match,coverage_discrepancy}。
- **replay 非 LLM**：replay 为确定性引擎，`replay.md` 只记录结果契约与边界约束，不接入模型。
- **AGENTS.md**：`prompts-pentest-templates.md` §10 的 worker 容器 `AGENTS.md` 模板（打包资源）未在本任务范围内创建（属 11-worker-sandbox 交付）。
