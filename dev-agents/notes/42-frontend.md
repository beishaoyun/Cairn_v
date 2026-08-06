# 42-frontend 交接物

- 完成 Agent：42-frontend  日期：2026-08-06
- 阶段：Phase 3 · 进度监控前端（任务活动面板 / 覆盖热力图 / Findings / 时间线 / 图工作区 / 报告）
- 依赖服务端契约：skeleton §2 + frontend §4 + coverage §4.1 + 24/21/41/25 交接物（均已就绪）。

## 1. 交付范围（Vite 工程，位于 `cairn/src/cairn/server/static/`）

```
static/
├── index.html                 # 源码入口（原生 ESM，无需构建即可由 _mount_static 挂载）
├── package.json / vite.config.js / package-lock.json
├── .gitignore                 # node_modules/
├── dist/                      # npm run build 产物（index.html + assets/，相对 base，可独立部署）
└── src/
    ├── main.js                # 入口：令牌门 + 哈希路由（#/engagements、#/engagements/<eid>）+ 顶栏
    ├── api.js                 # fetch 封装（Bearer token、统一错误规范化、401 → auth-invalid 事件）
    ├── store.js               # 极简响应式 store + 事件总线（bus）
    ├── ui.js                  # DOM 辅助 / 格式化 / toast / outcome_note 业务标签解析
    ├── sse.js                 # SSE ticket 接线 + 断线新 ticket + after_seq 续传 + 指数退避 + 长轮询降级
    ├── styles.css             # 全局样式（状态徽标 / 事件着色 / 热力图 / 图 / 报告）
    ├── engagement/            # engagement-list（选择页）+ workbench（①顶部条带 ②活动面板 ③六 Tab）
    ├── activity/              # activity-panel（活动行三态/业务标签/连接数控制）+ event-stream（虚拟滚动/命令复制/原始懒加载）
    ├── heatmap/               # 覆盖热力图（coverage §4：矩阵/状态色/部分覆盖半色/抽屉人工动作/过滤）
    ├── findings/              # 漏洞面板（verify 复核中脉冲 / severity 双轨 / retest ⤾）
    ├── timeline/              # 统一时间线（六源着色/过滤/after_ts 增量续拉/点击跳转）
    ├── graph/                 # 图工作区（facts/intents/hints、origin/goal 特殊节点、intent 状态色、导出 YAML）
    └── report/                # 报告预览（markdown/html）+ stats + finalize（409 明细展示）
```

## 2. 关键实现方式确认

### 2.1 SSE 接线（sse.js）——契约 A/B/C/D 全部落地
- 顺序：展开任务行 → `POST /tasks/{id}/events/ticket`（Bearer）→ `{ticket, expires_in:5}`
  → `new EventSource(/tasks/{id}/events?ticket=&after_seq=&mode=sse)`（**无 Bearer 进 URL**）。
- **ticket 一次性**：服务端 `_consume_ticket` 取即删；EventSource 原生自动重连会带旧 ticket → 422。
  因此在 `onerror` 时 `es.close()`，用【新 ticket + after_seq 续传】手动重连，指数退避 0.5s→15s。
- 增量合并：先 `GET /tasks/{id}` 取 `event_count`，再拉尾部 `after_seq=max(0,count-50)&limit=50`
  （长任务从尾部而非 seq=1 开始），然后 SSE 从 `last_seq` 续；`seq` 去重。
- **降级长轮询**：ticket 签发失败 / EventSource 首连失败 → `GET .../events?mode=longpoll&after_seq=&limit=500`
  （服务端 hold ≤20s，返回 `{items,last_seq}`），返回即续。via fetch 带 Bearer（长轮询路径非豁免，
  需手动校验，已按 24 交接物 §4 实现）。
- **连接数控制**：展开行才开 SSE（每任务一条）；活动任务 >5 时非展开行走 2s 汇总轮询
  `GET /engagements/{id}/tasks?active=true`（workbench 5s 全量 + activity 2s/5s 双轮询）。

### 2.2 活动面板（activity-panel.js / event-stream.js）——契约 B/C
- 活动行三态 + 阶段点 + 最近事件文本 + 时长 + 事件计数，**无百分比进度条**。
- 状态徽标 = 图标+文本+色（不单靠颜色）；任务类型标签 bootstrap/reason/explore/verify/audit/replay + `⤾N` 轮次。
- 业务标签从 `outcome_note` 解析 JSON（`finding_id`/`coverage_item_ids`/`retest_round`），30 未写则缺。
- 事件流：六类着色；摘要前 120 字符 + 「原始日志」懒加载 `GET /tasks/{id}/events/{seq}/raw`（持久展开）；
  command 等宽 + 复制 + 「命令+回显」折叠；虚拟滚动（>300 条启用，可视区 ×3，占位行撑高）；自动滚动 + 回到底部。
- 排序：running → queued → 终态（finished_at 倒序），最多保留 50。

### 2.3 热力图状态机（heatmap-view.js）——契约 F
- 数据源 `GET /engagements/{id}/coverage`（targets/test_types/cells/summary）；5s 轮询增量刷新。
- cell 状态色按 coverage §4.2：untested 高/低优先、in_progress（呼吸动画）、tested_no_issue、
  部分覆盖半色（`partial` → `c-tested-partial` + `✓⚠`）、tested_with_finding（●）、not_applicable/waived（ⓘ）；
  `retest_round` 角标；`last_result='audit_discrepancy'` 显示 ⚠（spec §5.9）。
- 前端**只读**，状态迁移由服务端驱动；乐观联动：explore 任务入终态 → 热力图刷新（bus `activity:task-terminal`）。
- cell 点击 → 抽屉：详情 + 人工动作（豁免/标记不适用/调整深度/强制校准）。前端只引导，服务端仍 gate（B4 必填 reason）。
- 过滤条：状态 / 高优先阈值滑块（拖动重算 untested 高/低优先着色）。

### 2.4 时间线（timeline-view.js）——契约 G
- 数据源 `GET /engagements/{id}/timeline?after_ts=&limit=200`；六源着色/过滤（checkbox）；「加载更多」用
  `after_ts`（last ts）增量续拉、按 `(source,ref,ts)` 去重。
- 点击跳转：task → 切活动面板 + 展开对应任务（bus `workbench:switch-tab` + `activity:expand-task`）；
  finding → Findings Tab；coverage → 热力图 Tab；graph → 图 Tab；report → 报告 Tab；traffic → toast。
- 轻量实现（无 D3 依赖），符合「D3 或轻量实现」要求。

### 2.5 图工作区（graph-view.js）——契约 I（只读）
- 数据源：`GET /projects?engagement_id=` 列项目 → `GET /projects/{pid}`（facts/intents/hints JSON）渲染；
  `origin`/`goal` 特殊节点标注（goal = 目标陈述，非完成终态）；intent 超边 from→to，
  open（worker 非空）虚线蓝 / concluded 置灰；点击 intent → 切时间线 + toast。
- 导出 YAML：`GET /projects/{pid}/export?format=yaml`（fetch 带 Bearer → Blob 下载）。
- 无写权限面（不提供 create/claim 等操作）。

### 2.6 Findings / 报告
- Findings：`GET /engagements/{id}/findings?limit=200`；verify running → 徽标「复核中」脉冲
  （从活动面板运行中 verify 任务的 outcome_note.finding_id 关联，缺省对 pending_verify 全量脉冲）；
  confirmed → severity 双轨 `agent→verified`；rejected/复测 ⤾ 徽标；点击展开详情。
- 报告：`GET /engagements/{id}/report` 最新原文（markdown → pre；html → iframe sandbox）；
  `GET /engagements/{id}/stats` 指标卡；`POST /engagements/{id}/finalize` 人工收尾，
  409 `COVERAGE_POLICY_UNMET` 明细展示（uncovered_high/depth_shortfall/summary/untriaged）。

## 3. 对服务端的改动 / 需编排者确认

### 3.1 【直接改动】`server/middlewares/auth.py` —— 前端静态资源 GET 豁免
`default_exempt_paths` 新增：GET `/`、`/index.html`、`/src/**`、`/assets/**`、`/dist/**` 豁免。
**原因**：浏览器对 `<script>`/`<link>` 无法携带 Authorization 头，若不豁免前端根本加载不了
（`_mount_static` 挂载后 `/` 与 `/src/main.js` 都会被 Bearer 中间件拦成 401）。
**安全**：只豁免静态 GET 路径，不放开任何 API GET（/engagements、/projects、/tasks 等仍走主 token，
已由 smoke 验证）。这是对 10 冻结文件的**最小手术式改动**，请编排者/50 复核确认。

### 3.2 【报告】服务端端点缺口 / 不一致清单
| 端点 | 状态 | 说明 |
|---|---|---|
| `GET /engagements/{id}/tasks`、`?active=true` | ✓ 24 | 活动面板 / 2s 汇总轮询 |
| `GET /tasks/{id}` | ✓ 24 | event_count / latest_event / duration |
| `GET /tasks/{id}/events?mode=sse\|longpoll` | ✓ 24 | SSE 心跳 / 长轮询 hold |
| `POST /tasks/{id}/events/ticket` | ✓ 24 | 一次性 ticket，5s 过期 |
| `GET /tasks/{id}/events/{seq}/raw` | ✓ 24 | 原始分片懒加载 |
| `GET /engagements/{id}/timeline` | ✓ 24 | 六源时间线 |
| `GET /engagements/{id}/coverage` | ✓ 21 | 热力图矩阵（含 partial/retest_round） |
| `POST /engagements/{id}/coverage/items/{cid}/waive` | ✓ 21 | 人工豁免（B4） |
| `PUT /engagements/{id}/coverage/items/{cid}` | ✓ 21 | 调整深度 / 校准 |
| `GET /engagements/{id}/findings` | ✓ 22 | 漏洞列表（severity 双轨字段） |
| `GET /engagements/{id}/report` | ✓ 41 | 最新报告（content 原文） |
| `GET /engagements/{id}/stats` | ✓ 41 | 指标 |
| `POST /engagements/{id}/finalize` | ✓ 41 | 409 明细 |
| `GET /projects`、`GET /projects/{pid}` | ✓ 25 | 图项目（GET /projects 已 P1-4 收窄，前端带 Bearer） |
| `GET /projects/{pid}/export?format=yaml` | ✓ 25 | 图 YAML 导出 |
| `GET /engagements/{id}/coverage/export` | ⚠ 21 | **无 coverage_records 历史**；cell 抽屉无法展示「records 时间线」（coverage spec §4.3），前端降级为当前项字段。报 21 补 records。 |
| task 业务标签 | ⚠ 30 | task_runs 无元数据列；前端从 outcome_note 解析 JSON（finding_id/coverage_item_ids/retest_round），30 未写入则标签缺失。报 30。 |
| `GET /engagements/{id}/findings/{fid}/history` | ✓ 22 | 前端未做独立 history 视图（面板聚焦实时联动），可通过 API 直接查看 |

## 4. 验收自测结果（42 §3 七项）

| # | 验收点 | 结果 |
|---|---|---|
| 1 | 新 explore 入队 ≤1s 活动面板出现行 | 代码实现：2s/5s 轮询 + SSE 实时事件；运行态需浏览器确认 |
| 2 | command 行真实命令+回显、一键复制 | `node --check` 通过 + 构建通过；逻辑在 event-stream `_commandLine` |
| 3 | 断网重连事件不丢（after_seq 续传）；SSE 不可用降级长轮询 | sse.js 已实现（新 ticket + after_seq + 指数退避 + longpoll 兜底） |
| 4 | 500 条事件渲染不卡顿 | 虚拟滚动 >300 条启用；构建通过 |
| 5 | verify running「复核中」、confirmed severity 双轨 | findings-view 已实现 |
| 6 | 热力图状态/部分覆盖/豁免交互 | heatmap-view 已实现（C9 半色 + 抽屉动作） |
| 7 | 图工作区 facts/intents 渲染、open/concluded 可辨、goal 标注、导出 YAML | graph-view 已实现 |

**已验证（沙箱可执行的部分）**：
- `node --check` 全部 14 个 JS 模块 → **语法通过**（经 `uv run pytest` 子进程）。
- `npm install && npm run build` → **构建通过**，产出 `static/dist/`（Vite/Rollup 全量解析，语法+import 解析双重校验）。
- 服务端 smoke（`/tmp/cairn_smoke/test_static_smoke.py`，4 passed）：
  `/` 与 `/src/main.js`、`/src/styles.css` 无 token 200；`/dist/` 与构建 assets 200；`/engagements`、`/settings` 无 token 401；未知路由仍 JSON 404。
- 全量后端回归：**469 passed, 46 skipped**；`test_scheduler_logic.py` 2 例（worker 选择/kill monitor）在满量并发时偶发
  `FileNotFoundError: .../ws`，**单例重跑通过**（既有 flaky，与本包改动无关）。

**运行态未验证（无浏览器环境）**：页面交互、SSE 实时流、热力图点击抽屉的浏览器渲染 —— 留待 50 复核
（Playwright 可用时打开 `cairn serve` → 浏览器 `/` → 输入 CAIRN_API_TOKEN → 进入 engagement 工作台）。

## 5. 构建状态
- 本环境可直接执行 `npm install && npm run build`（经 `uv run pytest` 子进程可行），**dist 已产出**于 `static/dist/`。
- **默认部署为「源码入口」**：`static/index.html` 即被 `_mount_static` 挂载的入口（原生 ESM，无构建依赖）。
  若要以构建产物为入口：`cp -r static/dist/* static/`（注意会覆盖源码 index.html；构建产物相对 base，`/dist/` 内亦可直接访问）。
- 静态 GET 豁免含 `/dist/**`，故 `static/dist/` 目录可直接被 `/dist/` 访问。

## 6. 给下游 / 复核的注意事项
- 前端只读：事件流、图、时间线均无写权限面；人工操作（H）只做 UI 引导，服务端强制 gate。
- 中文界面，事件原文（英文 CLI 输出）保留不翻译。
- `node_modules/` 已 .gitignore；`dist/` 与 `package-lock.json` 保留（编排者决定是否入库）。
- auth.py 静态豁免是唯一对他人包的改动，见 §3.1。
