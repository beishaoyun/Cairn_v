# Agent 50 — 交叉审查与验收（Docs vs Implementation 对拍）

> 阶段 3 · 最后执行。**不是写代码包，是对拍审计包**：逐文档核对实现，跑全量回归，输出差异报告。所有 Agent 的交接物（`dev-agents/notes/*.md`）是你的输入。

## 0. 开工前必读
1. `CLAUDE.md`（黄金不变量 = 你的审查清单）
2. `docs/rule-registry.md`（规则号一致性）
3. **全部 docs/** 按 CLAUDE.md §2 文档地图通读（权威文档），`docs/specs/*` 跳过（v1）
4. `dev-agents/notes/*.md`（各包交接物，交叉对照自报 vs 实际）

## 1. 审查维度（逐项出 P0/P1/P2 结论）
1. **DDL↔db.py**：`database-ddl-draft.md` §1-§9 每表/列/索引/CHECK/FK 与 `server/db.py` 逐条 diff。重点：`task_runs.project_id` 可空、`findings.target_id` CASCADE、`finding_http_evidence.traffic_id` 无 ON DELETE、`engagement_counters` kind 枚举、FTS5 contentless。
2. **API 契约**：skeleton §2 全部路由 ↔ `routers/*`（方法/路径/鉴权标注）；v2 §7.3 错误码 ↔ `errors.py` + 各路由实际抛码；统一错误响应形状 `{"error_code","message","detail"}`。
3. **服务签名**：skeleton §3 每个签名 ↔ 实现（参数/返回），改签名未同步文档的抓出来。
4. **Prompt 契约 ↔ 校验器**：prompts §8 校验器对照 ↔ `validate_*`（reason/explore/bootstrap/verify 两阶段）；`complete` 字段禁令在全部校验器生效。
5. **枚举逐字符**：engagement/finding/coverage/verify/task 状态、verify_runs.stage/independence、waiver.kind、outcome —— 全部与 DDL CHECK 比对（大小写敏感）。
6. **安全不变量（CLAUDE.md 1-9）**：Server 单写者（Dispatcher 无 DB 直连）；Agent 容器无 token（grep 容器代码/镜像/AGENTS.md 无 CAIRN_API_TOKEN 注入）；仅人工 gate（fixed/closed/finalize/豁免 的鉴权 H + 业务校验双在）；capture 强制 bridge。
7. **规则号一致性**：代码注释中的规则号在 `rule-registry.md` 全部可解析、无重号、无 D 类 C 类混用。
8. **ID 生成**：全部 ID 走 `next_id`（counters/engagement_counters），`tt_<slug>` 幂等，无裸自增。
9. **mock 回归**：`pytest cairn/tests/test_mock_end_to_end.py` 全绿（46 用例 ↔ TV 编号 ↔ 规则映射核对）；任一红 → P1。
10. **验收要点**：每 spec 的「验收要点」节逐条勾（coverage §5 / capture §10 / frontend §9 / worker-sandbox §8 / **exploration-graph-spec §7** / verify-mock-test-spec 全用例）。
11. **图子域（25）**：`exploration-graph-spec.md` §3 签名 ↔ `services/graph.py` 逐条；§4 图规则 28 条逐条 grep 到实现；skeleton §2.4 图路由 ↔ `routers/{projects,intents,hints,export}`；origin/goal 播种、goal 禁 from/to、worker∈{null,creator}、双租约 409 仲裁、B5 冻结、读时超时清理、ID 走 scoped_counters。
12. **执行管线（13/11/31）**：`WorkerDriver`/`ExecutionBackend`/`ExecProcess` 协议 ↔ 三驱动实现（claude/codex/pi）+ 容器/local 后端（11）+ mock（31）接口一致；session 提取/健康检查/`TaskCancellation`；`cairn dispatch` CLI 装配到 40 loop；LLM env key 校验（container 必须、local 禁止）；C5（驱动内无 Cairn token）。

## 2. 输出（必写）
```
dev-agents/notes/50-reviewer.md
├── 结论摘要：是否达到「从 0 交付」门槛（P0=0）
├── 差异清单：每项 [P0/P1/P2] — 文件:行 — 问题 — 建议修复（文档 or 代码）
├── 规则号核对表（全部出现编号 ↔ registry）
└── 未覆盖项：spec 验收点中实现缺失/未验证的（明确写，不假装全过）
```

## 3. 修复权
- **只允许改文档**（docs/ 下 markdown + dev-agents/notes），改动先列 diff；实现 bug 一律**写报告交回对应包**，不改代码。
- 若发现「文档自相矛盾」而非实现错误：按 CLAUDE.md §5「实现与文档冲突时先查哪个版本旧」，改文档走「先列 diff 再改」。

## 4. 硬约束
- **不实现新功能**；你的价值是找「文档说 A、代码做 B」的每一处。
- 不信任交接物自报；每个「已实现」都要能 grep/测试到证据。
- 时间戳/时区、分页、错误码这些跨切面项单独过一遍。

## 5. 完成判据
- P0 = 0；P1 全部有明确归属包与修复方案；mock 回归全绿；规则号零冲突。
- 产出可执行：下一轮由各对应包 Agent 领 P1 修复单。
