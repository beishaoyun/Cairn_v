# 24-progress-timeline 交接物

- 完成 Agent：24-progress-timeline  日期：2026-08-06
- 阶段：Phase 1 · 依赖 10（server-foundation）；open_task_run/append_event 被 12 客户端与 30 任务使用；timeline 被 41 报告与 42 前端使用。

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `cairn/src/cairn/server/services/progress.py` | `open_task_run` / `append_event` / `events_after` / `event_raw` / `finish_task_run` / `list_task_runs` / `get_task_run` / `engagement_timeline`（re-export） / `tuning_values` / `_global_next_id` | 进度子域服务层（task_runs/task_events 只增只读）；ID 全局自增（counters 表）；12 tuning 读取 |
| `cairn/src/cairn/server/services/timeline.py` | `engagement_timeline` + 六源 `_graph_events`/`_task_events`/`_finding_events`/`_traffic_events`/`_coverage_events`/`_report_events` + `_iso_to_epoch` | D3 六源聚合只读；epoch 归并（兼容 Z / +00:00 混合时间戳） |
| `cairn/src/cairn/server/routers/progress.py` | `router` + `_sse_events` / `_longpoll_events` / ticket 三函数 / `require_events_auth` / `finish_task` | 6+1 路由（tasks/events/ticket/raw/timeline/finish）；SSE+长轮询+即时 JSON 三模式 |
| `cairn/tests/test_progress.py` | 25 用例 | §3 六项验收全覆盖 |
| `cairn/src/cairn/config.py` | `ServerConfig.logs_root`（env `CAIRN_LOGS_ROOT`） | **新增字段**：任务原始流分片文件根（raw_path 懒加载用），backward-compatible 默认 `data/logs` |
| `cairn/src/cairn/server/middlewares/auth.py` | `default_exempt_paths` 增加 `GET /tasks/{id}/events` 豁免 | **扩展**：EventSource 带不了 Authorization 头；SSE 用一次性 ticket 鉴权（handler 内消费），JSON/长轮询由 `require_events_auth` 手动校验 Bearer 或 ticket |

## 2. 关键实现方式确认

### 2.1 task/event ID 生成（Dispatcher 侧全局）—— 已自实现

10 的 `next_id` 只支持 engagement 作用域 kind + `'engagement'`，**不含 task/event**。本包自实现
`_global_next_id(conn, name, prefix)`：走 `counters` 表（name=`'task'`/`'event'`，DDL §4.1 允许），
`INSERT OR IGNORE` 种子行 + `UPDATE counters SET value=value+1 ... RETURNING value`。

- 线程安全保证：每请求独立 sqlite3 连接（`get_db`）；`UPDATE ... RETURNING` 单语句原子读-改-写；
  SQLite WAL 单写者 + `busy_timeout=5000` 串行化并发 UPDATE（与 10 的 `next_id('engagement')` 同一机制）。
- 产出：`task-###` / `ev-###`（三位补零）。

### 2.2 SSE 接线细节

- 模式判定：`mode=sse` 或 `Accept: text/event-stream` → SSE；`mode=longpoll` → 长轮询；缺省 → 即时 JSON。
- SSE 生成器 `_sse_events`：先补推 `after_seq+1..` 存量摘要（`event: <kind>` + `data: <json>` 帧），再
  循环轮询新事件；每 `sse_heartbeat_seconds` 发 `: heartbeat\n\n` 注释。客户端断开 → GeneratorExit 安全退出。
- 生成器**自开自关 DB 连接**（`db_path` 来自 app config）——StreamingResponse 的 body 在请求级依赖
  `get_db` teardown 之后才消费，若用依赖连接会因连接已关而静默失败。
- 心跳/长轮询时长读 `progress.tuning_values()`（懒加载 `cairn.dispatcher.config.TuningConfig` dataclass 默认，
  15/20/512；dispatcher 不可导入时回退文档默认）。**未硬编码**。
- 测试 seam：`_SSE_MAX_HEARTBEATS`（生产 None=无限流；测试设小值让流可 `resp.read()` 结束）。

### 2.3 ticket 实现

- `POST /tasks/{id}/events/ticket` 签发（Bearer 保护）；`secrets.token_urlsafe(24)`；
  进程内 dict `{ticket: (task_run_id, monotonic 过期时间)}` + `threading.Lock`（SSE handler 跑线程池）。
- 一次性：`_consume_ticket` 用 `dict.pop` 取出即删；SSE 模式 handler 内消费。
- 5s 过期：`_TICKET_TTL_SECONDS=5.0`，比较 `time.monotonic()`；过期后 SSE 拒绝（422 `VALIDATION`）。
- 非 SSE（JSON/长轮询）路径可接受 `?ticket=`（非消耗 `_peek_ticket`）或 Bearer token（`require_events_auth`）。

### 2.4 timeline 六源字段

统一结构 `{ts, source, kind, actor, summary, ref}`（+ 少量源内扩展键），`source ∈ graph|task|finding|traffic|coverage|report`：

| source | 表 | ts 字段 | kind 示例 | actor | summary | ref |
|---|---|---|---|---|---|---|
| graph | facts / intents(concluded) / hints | created_at / concluded_at | fact_created / intent_concluded / hint_created | creator / None | description/content | fact/intent/hint id |
| task | task_events JOIN task_runs | task_events.ts | step/tool/command/output/status/error | task_runs.worker | event.message | event id |
| finding | finding_history JOIN findings | finding_history.created_at | status_change | actor | note 或 `from→to` | finding id |
| traffic | traffic_entries | captured_at | captured | client | `METHOD url [status]` | traffic id |
| coverage | coverage_records / waivers / audit_runs | created_at | coverage_result / waiver:{kind} / audit:{reason} | tested_by / created_by / auditor | 结果/理由/verdict | record/waiver/audit id |
| report | reports | created_at | report | generated_by | `{format} report generated` | report id |

- 排序/`after_ts` 比较用 `_iso_to_epoch`（解析 ISO8601 兼容 `Z` 与 `+00:00`）——服务端各处时间戳写入格式不一致，字符串比较不可靠。
- 只读聚合，不加新表；limit 截断（默认 200，路由上限 1000）。

## 3. 未实现 / 待定

- **前端渲染**：不做（42 负责，只出 API 与事件数据）。
- **CLI 流解析 / F9 分类器**：不做（30/dispatcher/progress/stream.py 负责；本包只收 HTTP 上报的已分类事件）。
- **`task_events.seq` 并发安全**：seq = `MAX(seq)+1`（非事务锁）。Dispatcher 单进程单 worker 串行上报
  无竞态；若未来多进程并发 append 同一 run，需 UNIQUE(task_run_id, seq)（不新增表，加索引属 db.py 面，需编排者同意）。
- **业务标签**（explore 的 coverage_item_ids / verify 的 finding_id / retest 轮次）：task_runs 无元数据列，
  前端 §2.1「业务标签」未实现；建议 30 把元数据写入 `outcome_note`（JSON），42 自行解析。
- **`/tasks/{id}/finish`**：skeleton §2.5 无此端点，但 12 客户端 path 假设（phase0-alignment #1）需要，已补齐
  （`POST /tasks/{run_id}/finish`，仅终态，置 finished_at/outcome_note）。

## 4. 给下游的契约

### 给 30（任务上报 / Dispatcher）

- 上报链路：`open_task_run(eid, task_type, worker, project_id=None)` → 返回 `{id: 'task-###', status: 'queued', ...}`；
  `POST /engagements/{eid}/tasks` 服务签名 = `services.progress.open_task_run`（**无 REST 路由**，30 走 12 客户端的
  `POST /engagements/{eid}/task_runs` 路径假设——注意：本包未建该 REST 路由，12/30 需按 12 交接物 §3 对齐或由编排者定夺）。
  若需 REST，建议路由 `POST /engagements/{eid}/task_runs`（12 客户端已假设）。
- 事件上报：`append_event(run_id, *, kind, level, message, raw_path)`；`POST /tasks/{run_id}/events`（12 客户端路径假设，
  本包**未建该 REST 写路由**——同上，需 12/30 对齐或编排者定夺；服务层已就绪）。
- 收尾：`POST /tasks/{run_id}/finish` `{status: success|failed|cancelled|unhealthy|rejected, outcome_note}`。
- 摘要截断：`message` ≤ `tuning.event_summary_max_bytes`（512B 字节级截断）；原始流写 `raw_path`（相对 `logs_root`）。

### 给 41（报告「方法流程」）

- 数据源：`GET /engagements/{eid}/timeline?after_ts=&limit=` 返回六源统一事件，直接渲染为有序步骤列表。
- 每源保留 `source/kind/actor/summary/ref`；报告可按 source 分组/过滤。

### 给 42（前端）

- 活动面板：`GET /engagements/{eid}/tasks`（含 status/worker/duration_seconds/event_count/latest_event）；
  `GET /engagements/{eid}/tasks?active=true` 看板 2s 轮询。
- 事件流：`GET /tasks/{id}/events` 三模式——SSE（`EventSource(/tasks/{id}/events?ticket=..&after_seq=..&mode=sse)`，
  **无 Authorization 头**，靠 ticket）；长轮询（`?mode=longpoll&after_seq=`，hold ≤20s）；即时 JSON（`?after_seq=&limit=`）。
- SSE 接线：先 `POST /tasks/{id}/events/ticket` → `{ticket, expires_in:5}` → EventSource；断线重连用新 ticket + after_seq 续传。
- 原始流：`GET /tasks/{id}/events/{seq}/raw` 懒加载（文本/纯文本）；摘要超 512B 时展示前 120 字符 + 展开原始。
- 时间线 Tab：`GET /engagements/{id}/timeline?after_ts=&limit=`（按 source 着色、按类型过滤、after_ts 增量续拉）。
- 注意：`GET /tasks/{id}/events` 已豁免 Bearer 中间件（EventSource 带不了 Header）；JSON/长轮询走 fetch 需带 Bearer 或 `?ticket=`。

## 5. 自测结果

- `uv run --project cairn pytest cairn/tests/test_progress.py -q` → **25 passed**（含 SSE 帧+心跳、after_seq 续传、
  ticket 一次性+过期、timeline 六源/limit/after_ts、project_id=NULL、finish、raw 懒加载）。
- 全量 `uv run --project cairn pytest -q` → **423 passed, 46 skipped, 0 failed**（skips 为容器/docker 权限类）。
- auth 豁免、config.logs_root、路由自动发现均验证正常。
