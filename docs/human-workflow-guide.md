# 人工操作手册（Human Workflow Guide）

> 配套：`architecture-research-report-pentest-v2.md`、`frontend-progress-view-design.md`
> 用途：面向渗透/红队人员，说明所有**仅人工**操作的正确流程 —— Engagement 生命周期、范围管理、覆盖矩阵人工动作、漏洞状态升级、复测签收、报告交付、应急熔断
> 关键原则：**状态升级（verified/fixed/closed/false_positive/accepted）、豁免、finalize 均仅人工**（v2 §6.2 / §12 规则 4/18）；Agent 只能创建 open 漏洞 + 补证据。

---

## 1. Engagement 生命周期

```
planning ──► active ──► paused ──► completed ──► archived
   ▲          │  ▲        │  ▲
   └──────────┘  └────────┘  └─（复测）completed→active 需显式 retest=true
```

| 阶段 | 人工动作 | 前置条件 / 说明 |
|---|---|---|
| **planning** | 定义标题、授权时间窗、scope_policy；登记 targets；预置测试项目录 | AI 不探索；人工可写 Hint |
| **→ active** | `PUT /status active` | scope 非空 + 窗口合法 + kill switch off |
| **paused / → active** | `PUT /status` | 硬停止语义；恢复后按 active 重调度 |
| **completed** | `POST /finalize`（见 §6） | 覆盖策略达标（`COVERAGE_POLICY_UNMET` 时先豁免） |
| **→ active（复测）** | `PUT /status active` + `retest=true` | 保留历史图与漏洞库 |
| **archived** | `PUT /status archived` | completed → archived 单向不可逆 |

> 授权窗口到期**自动**置 paused（无需人工）；熔断是最高优先（§8）。

## 2. 范围管理（targets）

- **登记**：域名/IP/CIDR/URL，`scope_status=authorized|prohibited`，`criticality` 按类型默认推断可覆盖（D5）。
- **禁用规则**：`prohibited` 目标任何任务命中即 `SCOPE_DENIED` 且审计，**无 fallback**（§12 规则 1）。
- **新增授权资产**：登记后捕获白名单自动刷新（C4，≤1 interval）。
- **删除**：应用层 gate——`DELETE /targets/{tid}` 前检查该 target 是否仍被 findings/coverage 引用；未结算时返回 409 并列出引用，需先人工结算（关闭/改挂/豁免）后再删。DB 层 `findings.target_id` 为 CASCADE（勿用 RESTRICT，会与 `DELETE engagement` 级联顺序冲突）。

## 3. 覆盖矩阵人工动作

热力图（目标×测试项）是"应测尽测"的可视化真相；每个格子必须是：**测过 / 显式豁免 / 不适用**，**不存在隐含跳过**（§12 规则 13）。

| 动作 | 何时做 | 说明 |
|---|---|---|
| **豁免（waive）** | 风险接受 / 规则外 / 目标无此功能 | 选 kind（not_applicable/out_of_scope/risk_accepted）+ **必填理由**；写 `waivers` |
| **标记不适用** | 服务确实无该功能 | 必须伴随 `waivers(kind='not_applicable')`，否则仍算未覆盖（B4）。explore 的 `outcome=not_applicable` 只是「建议」（热力图标「建议 N/A」），由人工确认建 waiver 后才置 `status='not_applicable'` |
| **调整深度** | 高价值资产需要更深测试 | `PUT coverage/items/{cid}` 改 depth |
| **强制校准** | AI 写回异常/明显漏测 | 人工改状态并留 note |
| **抽样复核确认** | 高优先格被 audit 打回（⚠ 徽标） | 看 `audit_runs` 理由，确认是"真漏测"还是"误判"，决定重测或恢复 |

> **部分覆盖**：格子显示黄绿 ⚠ 表示只测了部分端点/参数，不算充分覆盖；reason 会列低优先级补测（C9）。

## 4. 漏洞全生命周期操作

状态机：`open → pending_verify → verified / pending_false_positive → false_positive / needs_review / fixed → closed`

| 操作 | 触发者 | 流程 |
|---|---|---|
| 登记 | Agent（自动）或人工 | Agent 只能建 `open` + 补证据；人工可登记任意态（留 `detected_by=human`） |
| **复核触发/确认** | 人工可强制 | 默认 explore 产出即自动入队 verify（独立 worker，盲审两阶段）；人工可 `POST /findings/{fid}/verify` 再次复核 |
| **verified** | 独立复核流水线（verify confirmed）自动置；人工可覆盖 | 生效 severity = `verified_severity`（双轨标注 agent vs verified 差异）；`verified` 非「仅人工」，但 `fixed/closed/false_positive/accepted` 仅人工（见 §9） |
| **false_positive** | 人工终态 | verify rejected 先落 `pending_false_positive`，**终态必须人工确认** |
| **needs_review** | 系统升级（F6/C8 超限） | 补证循环停止，人工介入 |
| **fixed** | 人工标记 | 触发覆盖项重建 + replay/retest/verify 自动复测 |
| **closed** | **仅人工** | 见 §5 复测签收门槛 |

- **去重**：`(engagement_id, target_id, 规范化 title)`；重复只追加证据，不重复建单（B3 URL 已规范化）。
- **证据**：证据文件白名单（image/text/pdf）+ 请求/响应包（Web 类必备，以捕获字节为准，C2）。

## 5. 复测签收流程（F4/C10）

```
fixed ──► replay(确定性) + retest explore → verify ──► 确认账本(3 类型各 ≤1/轮)
   └─ 任何通道"仍存在" ──► 回 verified/open + P0 告警，retest_pass 归零，retest_round+1
```

人工 `closed` 前置门槛：
1. `retest_pass >= 2` **且** 含 ≥2 种类型（replay/verify/human）；
2. **HTTP 类必须含确定性 replay 确认**；**非 HTTP 类必须含命令确定性重放确认**（`kind='replay'`，F4 对应物）；未过门槛 → 403 拦截；
3. 覆盖收敛（或剩余未覆盖全豁免）。

> 注意：`closed` 是终态、不可由 AI 直接写；重复触发同一类型确认不再累计（账本幂等）。

## 6. 收尾（finalize）与报告

1. **finalize 前置**：报告 `report_ready`（覆盖策略达标：高优先格测到 required depth + 覆盖率 ≥95% + 剩余全豁免 + findings 分诊完成）。
2. 不达标 → 返回 `COVERAGE_POLICY_UNMET`；只能通过**豁免剩余项（人工+理由）**后重试。
3. 达标 → `POST /finalize` → Engagement 置 completed → 自动生成报告。
4. **报告**：`POST /report` 生成（执行摘要/范围/方法[时间线渲染]/漏洞清单/修复建议/证据附录）；证据附录 = 触发包原文 + 命令回显 + 复核记录 + 大流量引用（D4）。
5. 报告版本记入 `reports` 表，可追溯；`completed → archived` 归档。

## 7. 应急熔断（最高优先）

| 开关 | 触发 | 效果 |
|---|---|---|
| 全局 kill switch | CLI/API | **立即** SIGKILL 全部 Agent（不走 grace，C1）；停捕获；拒新派发 |
| 单项目 kill | `POST /engagements/{id}/kill` | 该项目任务取消 + 停容器 + 停捕获 |

- 熔断后任务不进入 conclude 收尾（同原 cancelled 语义）。
- 恢复：确认目标安全后清开关 → 重新调度。

## 8. 日常检查清单（人工轮巡）

1. 顶部条带：覆盖率 / 进行中任务 / 异常任务数 → 异常任务点开事件流定位。
2. `capture_gap` 计数 >0 → 排查代理/白名单（C2）。
3. `pending_verify` 滞留 >30min → 检查 worker 可用性 / 单 worker 降级告警（F7）。
4. `needs_review` 出现 → 立即人工介入（不再自动循环）。
5. 授权窗口临近结束 → 确认高优先格覆盖收敛 → 准备 finalize。

## 9. 权限边界提醒（H/T 语义）

- 平台为**单 token**（T/H 同一 Bearer）；"仅人工"靠**业务规则 + Agent 不持 token** 双重落实（§12 规则 37）。
- Agent 容器不注入 `CAIRN_API_TOKEN`；若发现 Agent 能调 H 接口 → 立即轮换 token + 重建容器（ops-runbook §9）。
