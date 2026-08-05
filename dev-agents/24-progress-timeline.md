# Agent 24 — 进度子域（Task Runs / Events / Timeline / SSE）

> 阶段 1 · 依赖 10。你的 `open_task_run`/`append_event` 被 12 客户端和 30 任务使用；timeline 被 41 报告与 42 前端使用。

## 0. 开工前必读
1. `CLAUDE.md`（不变量 1/7/8）
2. `docs/capture-verify-progress-spec.md` §7（数据模型/流式采集/前端要点/统一时间线）、§7.2（F9 结构化流）
3. `docs/frontend-progress-view-design.md` §3（事件流渲染 + SSE 接线）、§4（数据契约）
4. `docs/database-ddl-draft.md` §9.5（task_runs/task_events）、§4.1（ID 映射 task-/ev-）
5. `docs/backend-module-skeleton.md` §2.5 尾部（tasks/events/timeline 路由）、§3 progress 服务签名
6. `docs/rule-registry.md`（D3/F9）

## 1. 交付范围
```
cairn/src/cairn/server/services/progress.py     # open_task_run / append_event / events_after / engagement_timeline / event_raw
cairn/src/cairn/server/routers/progress.py      # /engagements/{id}/tasks /tasks/{id}/events(...) /ticket /raw /timeline
cairn/src/cairn/server/services/timeline.py     # 六源聚合（graph/task/finding/traffic/coverage/report）
cairn/tests/test_progress.py
```

## 2. 必须满足的契约
- **A. task_runs**：`open_task_run(conn, *, engagement_id, project_id=None, task_type, worker)`——**project_id 可空**（B2：verify/audit/replay 不挂 project，DDL §9.5）。status 枚举 `queued/running/success/failed/cancelled/unhealthy/rejected`；ID `task-###`（Dispatcher 侧全局，10 提供或本包生成，按 DDL §4.1 说明）。
- **B. task_events**：`append_event(run_id, *, kind, level, message, raw_path)`——kind ∈ step/tool/command/output/status/error；level ∈ debug/info/warn/error；`message ≤512B`（tuning.event_summary_max_bytes，截断可）；原始流落 `raw_path` 分片文件（懒加载，`GET .../events/{seq}/raw`）。只增只读（前端无写权限面）。
- **C. 事件流**：`events_after(run_id, after_seq, limit)` 增量。`GET /tasks/{id}/events?after_seq=&limit=&kind=&level=` 支持 SSE（text/event-stream，15s 心跳 + after_seq 续传）与长轮询（hold ≤20s）双模式，用 `Accept`/`?mode=` 区分。`POST /tasks/{id}/events/ticket` 一次性 ticket（EventSource 带不了 Header，ticket 5s 过期）。
- **D. 统一时间线（D3）**：`engagement_timeline(eid, *, after_ts=None, limit=200)` 归并六源（facts.created_at / task_events.ts / finding_history / traffic.captured_at / coverage_records+waivers+audit_runs / reports.created_at），统一结构 `{ts, source, kind, actor, summary, ref}` 按 ts 升序。只读聚合，不加新表。报告「方法流程」章节数据源（41）。
- **E. 路由**：skeleton §2.5 尾部全部（`GET /engagements/{id}/tasks`、`GET /tasks/{id}`、`GET /tasks/{id}/events`、`POST /tasks/{id}/events/ticket`、`GET /tasks/{id}/events/{seq}/raw`、`GET /engagements/{id}/timeline`）。`GET /engagements/{id}/tasks?active=true` 汇总口径（供前端轮询）。

## 3. 验收标准
1. open→append→events_after 增量序列正确；seq 单调。
2. SSE 冒烟：TestClient 下 events 端点输出 `event:`/`data:` 帧 + 心跳注释；after_seq 断点续传无丢。
3. ticket 一次性 + 5s 过期；过期后 SSE 拒绝。
4. timeline 六源归并有序、limit 截断、after_ts 增量正确。
5. `task_runs.project_id=NULL`（verify 任务）可插入成功。
6. 对照 capture §7.2 / frontend §9 验收自查。

## 4. 硬约束
- **不做前端渲染**（42 负责）；你只出 API 与事件数据。
- **不做 CLI 流解析**（F9 分类器在 30 或 dispatcher/progress/stream.py，本包只收 Dispatcher 通过 HTTP 上报的事件）。
- 不加新表；task_events 的 `id` 前缀 `ev-` 语义按 DDL §4.1。
- 心跳/长轮询时长读 12 的 tuning（sse_heartbeat_seconds/longpoll_hold_seconds），不硬编码。

## 5. 交接物
写 `dev-agents/notes/24-progress-timeline.md`：事件写入/读取签名、SSE 接线细节、ticket 实现、timeline 六源字段、给 30（上报）/41（报告）/42（前端）的契约。
