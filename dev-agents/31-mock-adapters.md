# Agent 31 — Mock 适配器与回归测试基座

> 阶段 1 · 依赖 12/30 的契约定义。你是**全链路验收的使能者**：让 `verify-mock-test-spec.md` 的全部用例可跑。

## 0. 开工前必读
1. `CLAUDE.md`（黄金不变量 + 测试口径）
2. `docs/verify-mock-test-spec.md` —— **全文（你的规格）**：§1 目标、§2 mock 机制改造、§3-§6 用例
3. `docs/backend-module-skeleton.md` §1（adapters/mock.py 位置）、§3（Mock 扩展说明）
4. `docs/prompts-pentest-templates.md` §9（mock 组适配）
5. `docs/architecture-research-report.md` §（SeedSessionDriver/MOCK_<PHASE> 语义，v1 参照）

## 1. 交付范围
```
cairn/src/cairn/dispatcher/workers/adapters/mock.py   # 扩展：verify/audit/replay phase + coverage/findings 输出 + prompt_has 条件
cairn/src/cairn/dispatcher/prompts/mock/*.md          # mock prompt 组（含 phase 字段，按 prompts §9）
cairn/tests/mock_harness.py                            # mock_cfg 辅助（_phase/_verify/_replay）+ seed 辅助（如 replay_seed）
cairn/tests/test_mock_end_to_end.py                    # 由 50 作为验收入口，你先把 harness 与 46 用例的 runner 写好
```

## 2. 必须满足的契约
- **A. MOCK 语义（verify-mock-test-spec §2）**：`MOCK_ALLOWED_OUTCOMES` 加入 `verify` phase（confirmed/rejected/needs_more_evidence/accepted_false/invalid_json/empty/command_fail）与 `replay` phase（remediated/unchanged/ambiguous/error）；`MOCK_DEFAULT_BEHAVIOR` 默认值补齐；`MOCK_ALLOWED_ENV_KEYS` 由 outcomes 自动派生 → `MOCK_VERIFY`/`MOCK_REPLAY` 自动合法。
- **B. payload 注入**：verify/replay 输出可带 `payload` 字段（verified_severity/verified_traffic_ids/suggested_action/reason；replay 的 matched_original/result）。`prompt_has` 规则条件（阶段 prompt 含某占位符 → 命中对应行为，支持 blind/comparison 两阶段区分）。
- **C. mock explore 扩展**：输出 `findings`（可带 http/commands 证据）+ `coverage_result`（covered_items/depth_achieved/outcome）概率化，`MOCK_EXPLORE_COVERAGE_OUTCOME` 等环境变量控制。
- **D. 用例 runner**：把 verify-mock-test-spec 的 46 用例（TV-01..TV-46）组织为 pytest 参数化用例，标注规则映射（§3-§6）；seed 预置（traffic/coverage/engagements 场景）。`replay_seed`：预置 tr-101（role=replay）+ 触发包文件。
- **E. 异常注入**：崩溃/挂起/非 JSON/空输出/accepted=false 均可达（通过 `MOCK_*` outcomes 概率或强制）。

## 3. 验收标准
1. `pytest cairn/tests/test_mock_end_to_end.py` 全绿（46 用例逐条对应 TV 编号）。
2. 每个 mock 输出形状通过 30 的校验器（validate_* 全部接受/拒绝符合预期）。
3. 挂起超时重派（TV-40）、拒绝落地 pending_false_positive（TV-04）、needs_more 循环超限（TV-20）等关键路径可稳定复现。
4. `prompt_has` 条件生效：盲审/对照两阶段能被区分注入。

## 4. 硬约束
- mock 是**契约验证器**，不做业务实现；若某用例暴露 30/21/22 的 bug，记录到交接物，不改 mock 去掩盖。
- 不新增 MOCK_* 变量名（遵循 verify-mock-test-spec §2 已定义集合；确需新增先列 diff 更新 spec）。
- 测试环境不真实发包、不真起 mitmproxy。

## 5. 交接物
写 `dev-agents/notes/31-mock-adapters.md`：MOCK_ALLOWED_OUTCOMES 全表、46 用例↔规则映射核对、未通过用例清单（交给 50 复验）、harness 用法。
