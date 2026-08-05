# Agent 42 — 前端（进度面板 / 覆盖热力图 / Engagement 工作台）

> 阶段 3 · 依赖服务端 API 契约（skeleton §2 + frontend §4 + coverage §4.1）。可用契约 mock 先行，联调在服务端就绪后。

## 0. 开工前必读
1. `CLAUDE.md`（约定 10：Vite + Alpine 类轻量栈）
2. `docs/frontend-progress-view-design.md` —— **全文（你的规格）**：§2 任务活动面板、§3 事件流 + SSE、§4 数据契约、§5 联动、§6 泳道、§8 性能、§9 验收
3. `docs/coverage-engine-implementation-spec.md` §4（热力图交互设计：数据契约/渲染/状态机）
4. `docs/backend-module-skeleton.md` §2.5/§2.6（API 契约）
5. `docs/capture-verify-progress-spec.md` §7.3（前端要点）
6. `docs/exploration-graph-spec.md` §5（图导出 YAML/timeline）—— 图工作区数据源

## 1. 交付范围
```
cairn/server/static/ 或 cairn/src/cairn/server/static/     # Vite 构建产物 + 源码（由 10 的 app.py 托管）
├── index.html
├── src/  （Vite + Alpine.js + Tailwind）
│   ├── engagement/           # Engagement 工作台：顶部进度条带 + 任务活动面板 + 业务 Tab（热力图/Findings/报告）
│   ├── activity/             # 活动行组件（折叠/展开）+ 事件流渲染（虚拟滚动/自动滚动/复制命令）
│   ├── sse.js                # EventSource ticket 接线 + after_seq 续传 + 长轮询降级
│   ├── timeline/             # 统一时间线（D3）：六源着色/过滤/跳转
│   ├── heatmap/              # 覆盖热力图（coverage §4）
│   ├── graph/                # 图工作区（v2 §8.8 保留原图能力）：facts/intents/hints 节点边渲染 + intent 状态色 + 导出 YAML
│   └── findings/             # 漏洞面板：pending_verify 清单 + verify 复核中脉冲 + severity 双轨
dev-agents/notes/42-frontend.md
```

## 2. 必须满足的契约
- **A. 页面嵌入**：进度视图不新建独立页，嵌入 Engagement 工作台（frontend §1.2 布局）；默认展开活动面板。
- **B. 活动行（§2）**：状态徽标体系（queued/running/success/failed/cancelled/unhealthy/rejected，图标+文本不单靠颜色）；任务类型标签（bootstrap/reason/explore/verify/audit + retest ⤾ 轮次）；**无百分比进度条**（LLM 任务无确定总步数），用三态 + 阶段点 + 最近事件文本 + 时长 + 事件计数。
- **C. 事件流（§3）**：六类事件着色（step/tool/command/output/status/error）；摘要 ≤512B 显示前 120 字符 + 懒加载原始分片；command 行等宽 + 复制按钮 + 「命令+回显」折叠；虚拟滚动（可视区 ×3）；自动滚动 + 回到底部；增量合并（先拉历史 50 再 SSE，按 seq 去重）。
- **D. 实时通道（§3.3）**：`POST /tasks/{id}/events/ticket` → EventSource `/api/tasks/{id}/events?ticket=&after_seq=` → 断线重连指数退避 + 新 ticket；**降级**：SSE 受限时走 `GET .../events?after_seq=&limit=` 长轮询（fetch，服务端 hold ≤20s）；连接数控制：展开行才开 SSE，活动任务 >5 其余 2s 汇总轮询 `GET /engagements/{id}/tasks?active=true`。
- **E. 业务联动（§5）**：explore success 写回 → 热力图格子变绿；verify running → finding 徽标脉冲；confirmed → severity 双轨（`8.1→9.0`）；rejected → 待人工确认；failed/rejected 任务 → 顶部告警；全部完成且覆盖满足 → 「可 finalize」提示条。
- **F. 热力图（coverage §4）**：目标×测试项矩阵；cell 状态色（untested/测试中/测过/部分覆盖⚠/不适用/豁免）；**部分覆盖半色**（C9）；500ms 无交互自适应列宽；cell 点击 → 详情 + 人工动作（豁免/标记不适用/调整深度，H 操作引导）。
- **G. 时间线（D3）**：六源（graph/task/finding/traffic/coverage/report）着色、类型过滤、增量续拉 `after_ts`、点击跳转源详情。
- **H. 数据契约（§4 表）**：全部 API 按表格实现（tasks/events/ticket/raw/timeline/coverage/findings）。
- **I. 图工作区（v2 §8.8 保留原图能力）**：渲染 facts/intents/hints 图（数据源 `GET /projects/{pid}/export?format=yaml|timeline`，exploration-graph-spec §5）；节点：origin/goal 特殊节点标注（goal 为目标陈述，非完成终态）、普通 fact；边：intent 超边 from→to（open intent 标注认领 worker / concluded 置灰）；点击 intent → 跳转对应任务活动面板或时间线；导出 YAML 按钮。**只读展示**，无写权限面（人工操作服务端强制 gate）。

## 3. 验收标准（对照 frontend §9 + coverage §4）
1. 新 explore 任务入队 ≤1s 活动面板出现行；事件增量 ≤500ms 到达（SSE 正常路径）。
2. command 行含真实命令与回显、可一键复制。
3. 断网重连事件不丢（after_seq 续传）；SSE 不可用降级长轮询可用。
4. 500 条事件渲染不卡顿；5000 事件长任务摘要流畅、原始日志按需加载。
5. verify running 见「复核中」，confirmed 后 severity 徽标正确更新（双轨）。
6. 热力图状态/部分覆盖/豁免交互正确（coverage §4 状态机）。
7. 图工作区：facts/intents 节点边渲染正确，open（认领 worker）/concluded 状态可辨，goal 特殊标注，导出 YAML 可用（对照 exploration-graph-spec §7 验收点）。

## 4. 硬约束
- **单 Bearer token**：前端存储方式自定（localStorage 等），SSE 一律走一次性 ticket，**不把 token 放 URL**。
- 事件流只读，无写权限面；人工操作（H）走 UI 但服务端仍强制 gate（前端不做安全边界）。
- 中文界面，事件原文（英文 CLI 输出）保留不翻译。
- 不引入重型框架（Vite + Alpine 或等价轻量栈；组件化以够用为准）。

## 5. 交接物
写 `dev-agents/notes/42-frontend.md`：页面结构、SSE 接线实现、热力图状态机、依赖的服务端端点清单（缺失/不一致的报给对应包）、验收自测截图/结果。
