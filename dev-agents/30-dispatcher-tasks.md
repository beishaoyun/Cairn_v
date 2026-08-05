# Agent 30 — Dispatcher 任务实现（Tasks / Writers / Replay / Stream）

> 阶段 1 · 依赖 12（客户端）。与服务端子域 20-24 **并行**——你按 skeleton §2 API 契约写，联调在阶段 2。你的写回调 21/22/23 的服务端点。

## 0. 开工前必读
1. `CLAUDE.md`（不变量 2/3/4/5/6）
2. `docs/backend-module-skeleton.md` §4（校验器清单）、§1 dispatcher 目录、§3（TaskType 扩展/写回）
3. `docs/prompts-pentest-templates.md` §1-§8（各 prompt 输出契约 + 占位符 + 校验器对照）、§10
4. `docs/coverage-engine-implementation-spec.md` §3（reason/explore/bootstrap 输出契约 JSON Schema）
5. `docs/capture-verify-progress-spec.md` §2.4（traffic_ids 注入）、§4（verify 派发/输出契约）、§5、§6（replay/命令确定性重放）、§7.2（F9 流）
6. `docs/architecture-research-report-pentest-v2.md` §8.2/§8.3/§8.7
7. `docs/verify-mock-test-spec.md` §2（MOCK 语义，配合 31）

## 1. 交付范围
```
cairn/src/cairn/dispatcher/tasks/__init__.py
cairn/src/cairn/dispatcher/tasks/common.py      # 任务基类/契约校验/写回/进度上报公共
cairn/src/cairn/dispatcher/tasks/bootstrap.py   # 攻击面发现 + discoveries 播种 + sweep_complete
cairn/src/cairn/dispatcher/tasks/reason.py      # 缺口驱动收敛（gaps 输入 → intents/coverage.recommend_finalize）
cairn/src/cairn/dispatcher/tasks/explore.py     # 覆盖项驱动 + findings + coverage_result
cairn/src/cairn/dispatcher/tasks/verify.py      # 两阶段盲审（blind→comparison）→ verdict
cairn/src/cairn/dispatcher/tasks/audit.py       # 覆盖抽样复核（独立重测高优先格子）
cairn/src/cairn/dispatcher/findings/writer.py   # finding 落库 + 去重 + 证据挂载（重试）
cairn/src/cairn/dispatcher/coverage/writer.py   # coverage_result 校验 + 写回 + 复测重建（B1/A5）
cairn/src/cairn/dispatcher/replay/engine.py     # 确定性重放引擎（F4）：原始触发包 + payload 变体 → 签名比对
cairn/src/cairn/dispatcher/progress/stream.py   # CLI 结构化流解析 + 自由文本兜底分类（F9）+ task_events 摘要上报
cairn/tests/test_tasks.py
```

## 2. 必须满足的契约
- **A. 校验器（skeleton §4）**：`validate_reason_payload`（intents 引用覆盖项 / coverage / 禁 complete）、`validate_explore_payload`（description+findings[]+coverage_result）、`validate_findings_payload`（severity/cvss/cwe/evidence_refs 白名单）、`validate_coverage_result`（covered_items∈engagement/未覆盖/outcome 枚举）、`validate_bootstrap_payload`（fact+sweep_complete+discoveries）、`validate_verify_blind_payload`（observations）、`validate_verify_compare_payload`（stage=comparison+verdict/verified_severity/reason/traffic_ids/http_mismatch）、`validate_replay_result`（matched_original/result）。输出统一 `{accepted, data}` 或非包装兼容；**`complete` 字段一律拒绝**（bootstrap 用 `sweep_complete`）。
- **B. reason（C8 收敛）**：输入 `{gaps}`（21 的 compute_gaps，exclude_in_progress=True）；输出 `intents[]`（每个引用 ≥1 未覆盖项）或 `coverage.recommend_finalize=true + waivers[]`（**建议**，人工批准才生效）。覆盖未满且两者都缺 → 任务失败 + escalation 计数（落 21 的 reason_escalation_state）。
- **C. explore（C2/B1）**：派发前注入 `{coverage_context}`（认领格子）+ **traffic_ids 候选**（`list_traffic(eid, client=<worker>, since=intent_start)`，Agent 只从候选引用，不能自查捕获索引——C5）；写回走 `coverage/writer.py`：claim 互斥（B1）→ `write_coverage_result`（幂等）→ findings 落库（writer）+ 证据（http/commands）+ `link_traffic(role='trigger')`。`outcome=not_applicable` 只建议不置状态。
- **D. verify（F1/F7）**：派发**排除创建该 finding 的 worker**（`verify_eligible` 且 ≠ 创建者）；一次任务 = 两次顺序模型调用（blind 只喂 digest+scope → comparison 喂 observations+finding+digest）；`verify_model` 非空时 comparison（或整任务）换模型池 → `independence=cross_model`。输出契约按 prompts §4.3/§8。`http_mismatch` 比对在任务内完成（fetch `resolve_traffic` 全量对 claim http[]）。落定调 22 `apply_verify_runs`。
- **E. replay 引擎（F4）**：不依赖 LLM（worker=`replay-engine`）。输入原始触发包（`resolve_traffic` 全量）→ 重放 + payload 变体 → 比对 `matched_original`/result（unchanged/remediated/ambiguous/error）；`remediated` → `record_retest_confirmation(kind='replay')`。重放请求**经捕获代理**发送（复测证据闭环，role='replay'）。**命令确定性重放**（非 HTTP 类，capture §6.1）：受控执行器 wrapper 重放 command 抓真实 stdout/stderr + sha256，判定签名。
- **F. 进度上报（F9）**：`progress/stream.py` 首选 CLI 结构化输出（`--output-format stream-json` 等）；兜底严格分类（`$ ` 前缀/工具调用行/`⚑ ` 注入前缀/stderr/traceback → 对应 kind；stdout 里 "error"/"failed" **不算 error**）；摘要 ≤512B 落 `append_event`，原始流分片写文件。
- **G. 写回重试**：findings/coverage 写失败退避 1 次再放弃（tuning.writeback_retries），仍失败只记日志不无界重试；覆盖写回幂等键 `(item_id, intent_id)`。

## 3. 验收标准
1. 每任务用 mock 驱动（31）跑通：bootstrap→reason→explore→verify 的 happy path + 契约拒绝路径。
2. 校验器单元测试：合法/非法 payload 各自拒绝；`complete` 字段被拒；verify 两阶段契约校验。
3. reason 收敛约束：覆盖未满不出 intent 也不出 finalize → 任务失败（对照 coverage §3 硬约束）。
4. explore 写回：claim 互斥（他人格子被拒）、not_applicable 建议、traffic_ids 候选注入。
5. replay：remediated/unchanged 分支 + 账本幂等（对照 TV-30/31/44）。
6. F9 分类：scanner 输出含 "error" 不产生 error 事件。

## 4. 硬约束
- **Agent 容器不持 token**：所有写回由你（Dispatcher 进程）经 12 客户端完成，绝不把凭据放进 prompt/容器环境。
- **不实现调度循环/worker 选择/心跳**（那是 40）；你只提供单任务执行 + 写回 + 校验的纯函数/类，供 40 编排。
- verify 派发逻辑（排除创建者）你实现**选择函数**，40 负责接入 loop。
- 规则号引用以 rule-registry 为准；prompt 输出契约与 prompts-pentest-templates 冲突时以文档为准并同步（先列 diff）。

## 5. 交接物
写 `dev-agents/notes/30-dispatcher-tasks.md`：任务→校验器→写回映射表、replay/命令重放实现、traffic_ids 注入流程、给 40 的调用接口（函数签名 + 返回值）、未做项。
