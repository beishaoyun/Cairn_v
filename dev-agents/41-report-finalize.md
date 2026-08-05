# Agent 41 — 报告生成与收尾（Report / Finalize）

> 阶段 2 · 依赖 20（状态机）/21（report_ready）/22（findings）/23（捕获证据）/24（timeline）。

## 0. 开工前必读
1. `CLAUDE.md`（不变量 3/4）
2. `docs/architecture-research-report-pentest-v2.md` §4.10（报告交付）、§8.11（报告引擎）、§12 规则 18
3. `docs/backend-module-skeleton.md` §2.6（报告路由）、§3 report 服务签名 + D4 证据附录策略
4. `docs/human-workflow-guide.md` §6（收尾与报告门槛）
5. `docs/capture-verify-progress-spec.md` §7.4（timeline 渲染方法流程）、§8（证据三层）
6. `docs/database-ddl-draft.md` §6（reports 表）、§4.1（rpt-###）

## 1. 交付范围
```
cairn/src/cairn/server/services/report.py     # aggregate / render_markdown / render_html / finalize
cairn/src/cairn/server/routers/report.py      # POST /engagements/{id}/report（H）/ GET /engagements/{id}/report/{rpt_id}（T）
cairn/src/cairn/server/routers/engagements.py # 补 finalize 端点（占位由 20 留的 501 填上）
cairn/tests/test_report.py
```

## 2. 必须满足的契约
- **A. finalize 前置（human-workflow §6）**：调 21 `report_ready(eid, policy)` 校验覆盖策略（高优先格测到 required depth + 覆盖率 ≥95% + 剩余全豁免 + findings 分诊完成）。不达标 → 409 `COVERAGE_POLICY_UNMET` + 明细；达标 → engagement 置 completed + 自动生成报告。`POST /engagements/{id}/finalize` 鉴权 H（仅人工）。
- **B. 报告内容（aggregate）**：执行摘要 / 范围 / 方法（=24 timeline 渲染为有序步骤列表）/ 漏洞清单 / 修复建议 / 覆盖总结 / 证据附录。漏洞字段：severity 双轨（agent vs verified，`8.1→9.0` 标注）、状态、证据引用。证据附录按 **D4 策略**：内嵌触发请求/响应原文（captured 派生）+ 命令回显 + 复核记录（verify_runs independence 级别）+ 重放记录（replay_runs）；**大流量只给引用**（traffic_id+sha256+digest），按需还原，不内嵌 GB 级原始包。
- **C. 渲染**：`render_markdown`（可读交付）/`render_html`（含时间线/热力图快照）。`POST /report` 生成后写 `reports`（format/path/generated_by/created_at，rpt-###），版本可追溯。
- **D. 审计线索**：报告/复核记录含 `independence` 与 `verified_severity`，如实标注（cross_worker 同模型族 → 标注「独立性有限」，C7）。
- **E. 路由**：skeleton §2.6 全部 + §2.5 的 stats/export（`GET /engagements/{id}/stats`：漏洞 severity 分布/覆盖趋势/任务成功率；`GET .../findings/export`、`.../coverage/export`）——这些可归本包或 24，按 skeleton §2.5 归属，写交接物说明。

## 3. 验收标准
1. finalize 门槛各分支：达标置 completed；不达标 COVERAGE_POLICY_UNMET + 豁免后可重试（对照 human-workflow §6）。
2. 报告生成：markdown/html 均产出；证据附录含触发包原文 + 命令回显 + 复核记录；大流量仅引用不内嵌。
3. 报告版本：连续两次生成 rpt-001/rpt-002，可分别下载。
4. timeline 渲染方法流程章节与 24 数据一致。

## 4. 硬约束
- finalize/report 生成 **仅人工**（H 鉴权标注 + 业务 gate 双重）。
- 不读 traffic 全量进报告；D4 引用策略是硬要求。
- `completed → archived` 由 20 状态机负责，本包不重复实现。

## 5. 交接物
写 `dev-agents/notes/41-report-finalize.md`：finalize 校验明细字段、报告章节结构、D4 引用策略落地、stats/export 归属、给 42 的报告预览接口。
