# 41-report-finalize 交接物

- 完成 Agent：41-report-finalize  日期：2026-08-06
- 阶段：Phase 1/2 · 报告生成与收尾（Report / Finalize）
- 依赖：20（状态机）/21（report_ready）/22（findings 明细）/23（捕获证据）/24（timeline）

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `cairn/src/cairn/server/services/report.py` | `aggregate` / `render_markdown` / `render_html` / `finalize` / `generate` / `list_reports` / `latest_report` / `get_report` / `stats` | 报告聚合与渲染 + finalize 编排 + 指标统计 |
| `cairn/src/cairn/server/routers/report.py` | `generate_report`(POST /report, H) / `latest_report`(GET /report) / `download_report`(GET /report/{rpt_id}) / `engagement_stats`(GET /stats) | skeleton §2.6 + §2.5 stats |
| `cairn/src/cairn/server/routers/engagements.py` | `finalize_engagement`（**替换 20 留的 501 占位**） | 只改 finalize 端点，未动其他路由 |
| `cairn/tests/test_report.py` | 13 用例 | §3 四项验收 + stats + 路由冒烟 |

## 2. 验收关键输出

1. **finalize 门槛各分支**：覆盖达标 → 置 completed + 自动生成 markdown/html（rpt-001/002）；不达标 → 409 `COVERAGE_POLICY_UNMET` + 明细（豁免后可重试）；planning 未激活 → 409 `ENGAGEMENT_INVALID_STATE`；kill 开 → 423；重复 finalize → 409。
2. **报告生成**：markdown/html 均产出；证据附录含触发包原文（`finding_http_evidence`）+ 命令回显 + 复核记录（verify_runs independence）+ 重放记录（replay_runs）；大流量仅引用（traffic_id + sha256 + digest），不内嵌。
3. **报告版本**：连续生成 rpt-001/rpt-002，`GET /report/{rpt_id}` 可分别下载；`GET /report`（latest）供 12 客户端。
4. **timeline 方法流程**：§3 = 24 `timeline.engagement_timeline` 渲染为有序步骤列表，与 24 数据同源一致。

## 3. finalize 校验明细字段

`finalize`（服务 `report.finalize`）流程：
1. 熔断 gate：`scope.check_kill_switch`（423 `KILL_SWITCH_ON`）；
2. 状态 gate：仅 `active`/`paused` 可 finalize（planning/completed/archived → 409 `ENGAGEMENT_INVALID_STATE`）；
3. 策略 gate：`coverage.report_ready(eid, policy)`，policy = `scope_policy.coverage`（缺省 21 `DEFAULT_COVERAGE_POLICY`）；不达标 → 409 `COVERAGE_POLICY_UNMET` + detail：
   - `uncovered_high`（高优先缺口列表，F11 排除后）
   - `depth_shortfall`（深度短欠计数）
   - `summary`（coverage_summary：total/covered/coverage_ratio/untested/in_progress/na/waived/with_finding/partial）
   - `untriaged_findings`（未分诊计数，走 22 `findings.triaged`）
   - `policy`（生效策略）
4. 达标 → `scope.transition_status(eid, 'completed')`（完成置 completed_at）→ 自动 `generate`（markdown + html 两版写入 reports 表）。

「仅人工」双重：H 语义标注（路由 docstring）+ 业务 gate（覆盖收敛 + 置 completed 均非 Agent 可达）；实际鉴权由 C5（Agent 容器不持 token）+ Dispatcher 写回白名单落实。

## 4. 报告章节结构（aggregate）

```
1. 执行摘要（engagement + severity 分布 + 覆盖总结）
2. 授权范围（targets 列表）
3. 方法流程（24 timeline → 有序步骤列表，source/kind/actor/summary/ref）
4. 漏洞清单（按生效 severity 降序；severity 双轨 agent vs verified 标注；证据内嵌）
5. 修复建议（finding.remediation 聚合）
6. 覆盖总结（coverage_summary + HTML 热力图快照 coverage_matrix）
7. 证据附录（D4）
```

漏洞字段：`severity`（生效）/`agent_severity`/`verified_severity` 双轨，`8.1→9.0` 式差异标注（规则 27）；状态、CVSS、CWE、description/remediation、evidence_summary、retest_round/pass、verify_runs、replay_runs、retest_confirmations、traffic_refs。

## 5. D4 引用策略落地

- **内嵌层**：`finding_http_evidence`（captured 派生，body ≤64KB，规则 21）+ `finding_command_evidence` 回显 + `verify_runs`（stage/independence/verdict/verified_severity）+ `replay_runs`（status/result/matched_original/trigger_traffic_id）。
- **引用层**：`traffic_refs`（经 23 `get_linked_traffic`）—— 每项 `{traffic_id, role, source, method, url, status, req_path, resp_path, req_bytes, resp_bytes, sha256, digest}`；`digest` 为 best-effort F2 digest（`resolve_traffic(for_model=True)`，文件缺失/损坏 → None，引用仍含 sha256+路径，可 `GET /traffic/{tid}?for_model=true` 还原）。
- **不内嵌**：不读 traffic 全量进报告；不把 GB 级原始包写入 reports（D4/规则 25）。还原走 traffic 路由，报告阅读器按需取用。

## 6. stats / export 归属决策

- **`GET /engagements/{id}/stats` → 归 41**（本包 `report.stats`）：漏洞按 severity 分布（生效 + agent）、覆盖趋势（coverage_records 按自然日）、任务成功率（task_runs by status + success_rate）、verify/replay 审计。
- **`GET .../findings/export` → 归 22**（`routers/findings.py` 已实现 JSON/CSV）；**本包不重复注册**。
- **`GET .../coverage/export` → 归 21**（`routers/coverage.py` 已实现含豁免/审计）；**本包不重复注册**。

## 7. 报告版本与下载

- `reports` 表（DDL §6）：`id='rpt-###'`（counters kind='report' 全局），`format`（markdown/html），`path`（相对 reports_root，落盘文件），`generated_by`（'human'），`created_at`。
- 文件根：`reports_root` = `os.path.join(dirname(config.db_path), 'reports')`（派生，**未改 10 的 config.py**）；服务默认 `data/reports`。
- 端点：
  - `POST /engagements/{eid}/report`（H）body `{formats, generated_by}` → 生成报告（不改状态）；
  - `GET /engagements/{eid}/report`（T）→ 最新报告 JSON（含 content，12 客户端路径假设）；
  - `GET /engagements/{eid}/report/{rpt_id}`（T）→ 下载原文（markdown→text/plain，html→text/html）。

## 8. 给 42（前端）的报告预览接口

- `GET /engagements/{eid}/report`：最新报告，返回 `{id, engagement_id, format, path, generated_by, created_at, content}` —— content 即 markdown/html 原文，前端直接渲染/高亮。
- `GET /engagements/{eid}/report/{rpt_id}`：指定版本下载（text/plain 或 text/html）。
- `GET /engagements/{eid}/stats`：severity 分布 / 覆盖趋势 / 任务成功率 JSON，供预览页顶部指标。
- finalize 失败详情（`detail.uncovered_high` 等）可直接 tooltip：`POST /engagements/{eid}/finalize` 409 响应体。

## 9. 未实现 / 待定

- **PDF 报告**：DDL CHECK 含 'pdf'，本包只做 markdown/html（skeleton 标 PDF 可选扩展）。
- **报告归档迁移（C4）**：reports 文件物理迁移/加密属运维（23 交接物 §9 归档留待 41/运维）。
- **report_events 计入 timeline**：24 的 `_report_events` 已读 reports 表，生成报告后 timeline 自动含 report 事件（D3 六源闭环，本包无需额外接线）。
- **归档 reports_root**：未加入 ServerConfig（10 文件冻结）；当前从 db_path 派生，如后续需要独立配置需协调 10。

## 10. 对他人包的改动 / 注意

- **`routers/engagements.py`（20 所有）**：仅替换 finalize 501 占位为真实端点（加 `import os`、`Request`、`report as report_svc`），未动其他路由。
- **`tests/test_scope.py::test_router_finalize_501`**：原断言 501，已改为断言 planning → 409 `ENGAGEMENT_INVALID_STATE`（finalize 行为归 41，属必要适配）。
- **`replay_runs` 无 created_at 列**（DDL §9.3 只有 started_at/finished_at）：报告按 `ORDER BY started_at` 取数。
- **`verify_runs` 无 engagement_id 列**：stats 的 verify 审计经 findings JOIN 归属。
- **遗留测试垃圾**：仓库根有若干 `<sqlite3.Connection object at 0x...>/` 空目录（早期测试传 `reports_root=str(db_conn)` 的产物，已修复测试；沙箱拒绝删除，留待编排者清理）；`data/reports/eng_001/` 同理。

## 11. 自测结果

- `uv run --project cairn pytest cairn/tests/test_report.py -q` → **13 passed**。
- 已完成包回归：report/scope/coverage/findings/progress/capture/server_foundation/
  protocol_client/dispatcher_config/graph/container_archives/local_execution → **279 passed**（无回归）。
- 路由注册确认：`/engagements/{eid}/finalize`、`/engagements/{eid}/report`（POST+GET）、
  `/engagements/{eid}/report/{rpt_id}`、`/engagements/{eid}/stats` 均在 openapi 中。
- 全量说明：并行 Agent（30/31/40）共享仓库，全量 pytest 常被并发运行拖慢/锁争用；
  单独跑已完成包无回归。dispatcher 侧曾短暂因 `ReasonEscalation` 导出缺失 collection
  失败（30 并行改动，已修复），与 41 无关。
