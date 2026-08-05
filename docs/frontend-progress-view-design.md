# 进度监控前端视图设计（任务活动面板 + 事件流渲染）

> 配套：`architecture-research-report-pentest-v2.md` §2.2 / §8.8 / §8.15、`capture-verify-progress-spec.md` §7、`backend-module-skeleton.md` §2
> 用途：把「每一步 AI 在干什么」落地为**实时可见**的界面 —— 任务活动面板（谁在跑、跑到哪） + 事件流渲染（每步 step/tool/command/output/error 具体动作）
> 技术栈沿用：Vite + Alpine.js（或轻量框架）+ Tailwind；本文件只描述视图与交互，不涉及状态管理框架选型

---

## 1. 设计目标与信息架构

### 1.1 目标

| 目标 | 落地手段 |
|---|---|
| 实时可见每个 Agent 在干什么 | 任务活动面板：每个运行中/排队中任务一行，实时滚动最近事件 |
| 事件可回溯可取证 | 事件流：step/tool/command/output/status/error 六类，命令回显可一键复制 |
| 与业务状态联动 | explore 完成→热力图变色；verify 运行→findings 徽标脉冲；failed→告警 |
| 不断流不丢事件 | SSE 心跳 + `after_seq` 断点续传 + 长轮询降级 |

### 1.2 页面嵌入

进度视图**不新建独立页面**，嵌入 Engagement 工作台：

```
Engagement: pentest-001   [active]  窗口 08/01–08/15   [kill]
┌───────────────────────────────────────────────────────────────────────────┐
│ ① 顶部进度条带（汇总）                                                      │
│   覆盖率 62% · 进行中 3 · 排队 1 · 待复核 2 · 复测中 1 · 异常 0             │
├───────────────────────────────────────────────────────────────────────────┤
│ ② 任务活动面板（核心）            │  ③ 业务面板（Tab 切换）                  │
│   ┌────────────────────────────┐ │  ┌──────────────────────────────────┐  │
│   │ ● explore  worker-01 12evt │ │  │  覆盖热力图 │ Findings │ 报告    │  │
│   │ ● verify   worker-02  5evt │ │  │  │ 时间线（D3）                  │  │
│   │ ○ reason   worker-01   —   │ │  │   …既有视图，本设计不重复         │  │
│   └────────────────────────────┘ │  └──────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────────┘
```

- **③ 时间线 Tab（D3）**：`GET /engagements/{id}/timeline` 渲染六源统一时间轴（图/task/finding/traffic/coverage/report），按 source 着色、类型过滤、点击跳转源详情；报告「方法流程」预览同源。

- ② 面板可折叠；进入工作台默认展开（满足「每一步都要有进度」的默认可见性）
- ③ 中 Findings 面板与活动面板**双向联动**（见 §5）

---

## 2. 任务活动面板（Task Activity Panel）

### 2.1 活动行组件（一行 = 一个任务）

折叠态（默认）：

```
[●] [explore] worker-01   覆盖项 c-013   ⏱ 0:03:12   28 evt   ▸
     └ 最近事件: [step] 正在枚举 http://10.0.0.5:8080 的目录
```

展开态（点击 ▸）：

```
[●] [explore] worker-01   覆盖项 c-013   ⏱ 0:03:12   28 evt   ▾
     ├ 状态: running · 阶段: 目录枚举 · 结果: 0 findings, 1 covered
     ├ ── 事件流（§3）────────────────────────────────────
     ├ 00:00:02 [step]   开始任务 explore c-013
     ├ 00:00:05 [tool]   ▶ Bash(dirsearch)
     ├ 00:00:05 [command] $ dirsearch -u http://10.0.0.5:8080 -t 16
     ├ 00:00:41 [output] [+] 200 GET /admin/login (SIZE: 2048)
     ├ 00:00:42 [output] [+] 200 GET /admin/dashboard (SIZE: 10240)
     └ 00:00:45 [status] 覆盖项 c-013 已写回 (outcome: no_issue)
```

字段来源（全部来自 `task_runs` + `task_events`，前端不做计算）：

| 字段 | 来源 |
|---|---|
| 状态徽标 | `task_runs.status`（queued/running/success/failed/cancelled/unhealthy/rejected） |
| 任务类型标签 | `task_runs.task_type`（bootstrap/reason/explore/verify） |
| worker | `task_runs.worker` |
| 运行时长 | `started_at→finished_at`（running 时前端本地计时） |
| 事件计数 | `task_events` 计数（`GET /tasks/{id}` 返回 `event_count`） |
| 最近事件摘要 | `task_events` 中 max(seq) 行的 message |
| 业务标签（覆盖项/复测轮次） | 任务输入元数据：explore 显示 `coverage_item_ids`；verify 显示关联 `finding_id`；retest 显示 `⤾N` 轮次 |

### 2.2 状态徽标体系

| 状态 | 徽标 | 颜色 | 语义 |
|---|---|---|---|
| queued | ○ | 灰 | 排队中（等待空闲 worker） |
| running | ●（脉冲动画） | 蓝 | 运行中 |
| success | ✓ | 绿 | 正常完成 |
| failed | ✗ | 红 | 异常失败（含契约校验拒绝后重试耗尽） |
| cancelled | ⊘ | 灰 | 被取消（熔断/人工停止） |
| unhealthy | ⚠ | 橙 | worker 心跳异常被标记 |
| rejected | ⊘ | 红 | 输出 `accepted:false` 或派发被拒 |

> 颜色不单独承载语义，均配图标 + 文本（可访问性，见 §7）。

### 2.3 任务类型标签

| 类型 | 标签色 | 说明 |
|---|---|---|
| bootstrap | 紫 | 攻击面发现 + 覆盖播种 |
| reason | 蓝 | 缺口记账 + 收敛建议 |
| explore | 绿 | 覆盖项驱动探索（打补证/复测标签） |
| verify | 橙 | 独立复核（显示关联 finding id + 「复核中」脉冲） |

### 2.4 进度表达（关键设计：LLM 任务无确定总步数）

Agent 任务**没有确定性总步数**，因此**不使用百分比进度条**，改用「三态 + 阶段点」：

1. **三态**：排队（○）→ 运行（● 动画）→ 终态（✓/✗/⊘）
2. **阶段点**：若 CLI 输出结构化阶段（如 codex 的 plan/execute/review），映射为有限阶段点 `● ● ○ ○`（当前阶段高亮，只表示**相对位置**，不表示百分比）
3. **当前在做**：最近一条 `step` 事件文本（"正在枚举目录结构"）+ 运行时长 + 事件计数 —— 这是「每一步 AI 在干什么」的直接答案

**排序**：running 优先 → queued → 终态；终态按 finished_at 倒序，最多保留最近 N=50 个（可配）。

---

## 3. 事件流渲染（Event Stream）

### 3.1 事件行格式与着色

```
HH:MM:SS.mmm  [类型图标]  [级别徽标]  [消息摘要]
```

| kind | 图标 | 颜色 | 含义 |
|---|---|---|---|
| step | ⊹ | 蓝 | Agent 阶段变化（"正在枚举目录"） |
| tool | ▶ | 紫 | 工具调用（`▶ Bash(dirsearch)`） |
| command | $ | 琥珀 | 实际执行命令（等宽字体） |
| output | ⡿ | 灰 | 工具输出回显 |
| status | ⚑ | 绿 | 平台注入的状态（覆盖写回/发现登记） |
| error | ⚠ | 红 | 错误/重试/超时 |

级别徽标（level）：`debug` 灰 / `info` 无 / `warn` 琥珀 / `error` 红底。

### 3.2 渲染规则

- **摘要 ≤512B**：SSE 只推摘要；超长消息前端显示前 120 字符 + 「展开原始」→ 懒加载 `raw_path` 分片文件（`GET /tasks/{id}/events/{seq}/raw`）
- **command 行特殊处理**：`$` 前缀 + 等宽字体 + **复制按钮**（证据取证）；若该 command 有回显（output 紧随其后），整段可折叠为「命令 + 回显」一对
- **虚拟滚动**：只渲染可视区 ×3 的 DOM 节点，避免长任务刷爆 DOM
- **自动滚动**：位于底部时自动跟随新事件；向上滚动即暂停，右下角浮出「回到底部」
- **增量合并**：进入面板先 `GET events?limit=50` 拉最近历史，再开 SSE 接实时流，按 seq 去重合并

### 3.3 实时通道（SSE 接线）

EventSource 不支持自定义 Header（带 Token），用一次性 ticket：

```
1. POST /tasks/{task_run_id}/events/ticket     → {ticket, expires_in: 5s}
2. EventSource(/api/tasks/{id}/events?ticket=..&after_seq=<last_seq>)
3. 服务端: 先补推 after_seq+1.. 的存量摘要，再实时推；每 15s 发注释行心跳
4. 客户端: 维护 last_seq；断线重连时用新 ticket + after_seq 续传（指数退避）
```

**降级**：SSE 被代理/浏览器限制时，走 `GET /tasks/{id}/events?after_seq=&limit=` 长轮询（fetch，服务端 hold 最多 20s）。

**连接数控制**：每浏览器 SSE 上限 6 个。活动任务 >5 时：非展开行只走「汇总轮询」`GET /engagements/{id}/tasks?active=true`（2s 一次），仅用户展开的行开 SSE。

### 3.4 事件来源与分类（Dispatcher 侧 · F9）

**首选：CLI 结构化输出**。驱动支持时用 `claude-code --output-format stream-json`、codex/pi 的对应 JSON/debug 输出 —— 事件天然结构化（step/tool/command/output 直接映射），进度面板信号可靠，无需正则猜测。

**兜底：自由文本分类（严格模式）**。真实 CLI 无结构化输出时，`progress.stream.classify_line` **只对控制面明确特征分类**：

| 分类器规则（严格，按行匹配） | 产出 kind |
|---|---|
| 阶段横幅（如 `┌──`, `[plan]`, `Starting…`, `Exploring…`） | step |
| `▶` / `Tool use:` / 工具名调用行 | tool |
| `$ ` 前缀（harness 注入的命令回显）或 Bash 工具体首行 | command |
| Dispatcher 注入前缀 `⚑ `（覆盖写回/发现登记） | status |
| 来自 **stderr 流** 或 `traceback` / `command not found` / exit≠0 标记 | error |
| 其余非空行 | output |

**F9 防噪声**：stdout 里的 "error"/"failed"/"timeout" **不产生 error 事件**（scanner/nmap 输出常含这些词）；仅 stderr 流或严格错误签名才置红 —— 避免活动面板刷假红。

- **mock 驱动**不经分类，直接发结构化事件（见 `verify-mock-test-spec.md`）
- 分类错误兜底：无法分类的非空行一律 output，不影响链路

---

## 4. 数据契约（前端依赖的 API）

| 方法/路径 | 用途 |
|---|---|
| `GET /engagements/{id}/tasks` | 任务列表（含 status/worker/duration/event_count/latest_event/业务标签） |
| `GET /engagements/{id}/tasks?active=true` | 活动任务汇总（看板/条带轮询） |
| `GET /tasks/{task_run_id}` | 单任务详情 |
| `GET /tasks/{task_run_id}/events?after_seq=&limit=&kind=&level=` | 增量事件（SSE 与轮询共用） |
| `POST /tasks/{task_run_id}/events/ticket` | SSE 一次性 ticket |
| `GET /tasks/{task_run_id}/events/{seq}/raw` | 原始分片文件（懒加载） |
| `GET /engagements/{id}/coverage` | 覆盖热力图（联动，既有） |
| `GET /engagements/{id}/findings?status=pending_verify` | 待复核清单（联动，既有） |
| `GET /engagements/{id}/timeline?after_ts=&limit=` | 统一时间线（D3：六源聚合，增量续拉） |

---

## 5. 业务联动

| 触发 | 界面效果 |
|---|---|
| explore 任务 success 且覆盖写回 | 热力图对应格子变绿；任务行 ✓ |
| verify 任务 running | Findings 面板对应 finding 徽标显示「复核中」脉冲 + 橙点 |
| verify confirmed | finding → verified，severity 徽标更新为 `verified_severity`（若与 agent 不同，显示双轨 `8.1→9.0`） |
| verify rejected | finding → false_positive（人工确认前显示「待人工确认」） |
| retest 任务 | 任务行 ⤾ 徽标 + 轮次计数；findings 面板显示「复测 #2」 |
| failed/rejected 任务 | 顶部条带红点 + 聚合告警「2 个任务异常」；点击直达对应行 |
| 全部任务完成且覆盖满足 | 提示条「覆盖策略已满足，可提交人工收尾 finalize」 |

---

## 6. 多任务看板（泳道模式）

并发任务多时（≥4）切换到**泳道看板**：按 worker 分列，任务卡片在列内流动：

```
worker-01            worker-02            worker-03
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ ● explore      │  │ ● verify tr-01 │  │ ○ reason       │
│ ● explore      │  │ ● verify tr-02 │  │                │
│ ○ bootstrap    │  └────────────────┘  └────────────────┘
└────────────────┘
```

- 列顶 = worker 健康状态（健康/不健康/离线）
- 卡片 = 折叠态活动行；点击卡片在侧栏展开事件流
- 一键切换：条带模式 / 泳道模式

---

## 7. 可访问性与细节

- 类型/状态颜色**不唯一承载语义**：均配图标 + 文本标签
- 事件流键盘导航：↑↓ 选择行，Enter 展开，C 复制命令
- 等宽字体日志 + 行号 + 复制；长行自动换行
- 中文界面，事件原文（英文 CLI 输出）保留不翻译

---

## 8. 性能与成本控制

| 手段 | 说明 |
|---|---|
| 摘要入库 ≤512B | SSE/轮询只传摘要；原始流落文件懒加载 |
| 虚拟滚动 | DOM 节点数 ≤ 可视区 ×3 |
| 连接数上限 | 展开行才走 SSE；其余 2s 汇总轮询 |
| 原始流按 task 保留 | 保留最近 N 天，pcap/traffic 全量另存（见 capture spec §8） |
| 事件只增只读 | `task_events` 只增，前端只读，无写权限面 |

---

## 9. 验收要点

1. 新 explore 任务入队后 ≤1s 活动面板出现对应行（排队态可见）
2. 事件增量 ≤500ms 到达（SSE 正常路径）
3. command 行含真实命令与回显，可一键复制（证据取证路径）
4. 断网重连后事件不丢（`after_seq` 续传），SSE 不可用时降级长轮询可用
5. 500 条事件渲染不卡顿（虚拟滚动生效）
6. verify running 时 Findings 面板可看到「复核中」，confirmed 后 severity 徽标正确更新
7. 5000 事件的长任务：摘要区流畅滚动，原始日志按需加载不一次性拉全
