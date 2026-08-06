# Phase 0 对齐问题清单（编排者登记，未自行修复）

> 生成：2026-08-06（编排者，Phase 0 验收后）。状态：`open` / `phase2`（待阶段 2 联调）/ `env`（环境限制）。
> 规则：发现问题只登记报告，不自行改代码。各子域 Agent 开工前读本清单，命中自己范围的请处理并在交接物说明。

## 1. [phase2] 12 客户端 7 处「路径假设」端点
`scope/check`、`coverage/result`、`task_runs`、`/tasks/{id}/events`、`/tasks/{id}/finish`、report latest。
- 服务端尚无实现；阶段 2 服务端子域（20/21/24/41）实现时**以 12 交接物 §3 映射表为准对齐**，改任一侧要同步该表。
- 涉及包：20（scope/check）、21（coverage/result）、24（task_runs/events/finish）、41（report）。

## 2. [phase2] `resolve_traffic` for_model 默认值差异
- 客户端默认 `for_model=True`（digest），skeleton 服务签名默认 `for_model=False`（全量）。
- 建议：服务端保持 False=全量；40 派发 LLM 前显式传 True。阶段 2 联调确认。

## 3. [phase2] 13 容器 HTTP 健康探针路径可能过严
- claude GET `<base>/v1/messages`、codex GET `<base>/v1/models`、pi 按 wire API 选路径；只认 2xx。
- 真实网关可能对 GET 返回 404/405 → worker 误判 unhealthy。40 联调时决策是否放宽为「base 可达即健康」。

## 4. [phase2] 11 workspace_root 无 config 字段
- SecurityConfig 无 workspace_root；ContainerBackend 默认 `/var/cairn/workspace`，可注入覆盖。
- 建议：需要时在 dispatch-config-spec 补字段（先列 diff 再改）。

## 5. [env] docker Python SDK 未入 pyproject
- 遵守「不引入未明示依赖」；容器模式运行时需 `uv add docker` 或注入 `docker_client`。
- 环境 docker CLI 在当前容器 Permission denied（10/11 均确认）——容器类验收留到有权限环境。

## 6. [env] 端口 8000 被外部进程占用
- 环境存在 docker-proxy + 另一个 `/cairn`（非本仓库）`cairn serve --db-path` 进程占 8000。
- 冒烟请用空闲端口（如 8765/8766）；不动外部进程。

## 7. [phase2] `/projects` 占位 + auth 豁免
- 10 的 `/projects` 占位返回 `[]`、auth 豁免；25 接管后路由注册在占位之前，占位被遮蔽（无害）。
- **auth 豁免仍在 10 的 middlewares/auth.py**（25 不能改）。生产上应去掉豁免或换 /health 专用；25 如需强制鉴权可路由级加 Depends。阶段 3 加固确认。

## 8. [phase2] dispatch-config-spec 补 `local` 顶层段
- 12 按 `dispatch.local.example.yaml` 支持了 `local` 顶层段，但 spec §0 顶层列表未列出。
- 建议：确认后补入 spec（先列 diff）。

## 9. [git] `cairn/.venv` 未被 .gitignore 覆盖
- 提交前建议在根 `.gitignore` 加 `cairn/.venv/`（编排者未改，等你确认）。

## 10. [phase2] 20/21 播种依赖时序
- 20 在 create_engagement 调 21 的 `seed_default_test_types(conn, eid)`；21 需先实现。并行期 20 按契约写调用点 + 守卫，阶段 1 末联调验证播种。

## 11. [phase2] 25 conclude 同事务编排
- conclude 需同事务调 21 `write_coverage_result` + 22 `create_finding`（agent 只能 open）。并行期按 skeleton §3 契约写，阶段 1 末 stub 联调。

## 12. [phase2] 21 report_ready ↔ 22 triaged 依赖
- report_ready 的 untriaged 计数读 22 `triaged()`；22 需提供。并行期 21 按契约调用 + 守卫。

---

# Phase 1 新增/更新（2026-08-06 Phase 1 收尾）

> 状态注：`resolved` = 已处理；`open` / `phase2` / `env` 沿用。

## 13. [resolved] DDL 缺陷：engagement 作用域 ID vs 全局 PK → 方案 A 全局计数器
- 16 张表（targets/findings/coverage_items 等）ID 改走全局 `counters`（name=kind）自增，前缀格式不变、签名不变（`engagement_id` 参数忽略）。
- DDL 文档已改（§4.1 注 + 3 处注释 + §10 步骤 5）；10 重落 db.py + 更新 test_server_foundation（跨 engagement 全局唯一断言）。test_scope/test_coverage 此前 5 例失败转绿。
- 给下游语义：不得再假设「不同 engagement 的 target/finding 从 -001 重启」；`engagement_counters` 停用。全量 426 passed。

## 14. [open] middlewares/auth.py 现为多写者
- 10 建（Bearer 中间件 + GET /projects 豁免）；23 加 `POST /engagements/{id}/traffic` 豁免（F8 代理受限 token 路由级校验）；24 加 `GET /tasks/{id}/events` 豁免（EventSource 带不了 Header，SSE 模式 + 手动 ticket 校验）。后续改动豁免须经编排者，避免互相覆盖。

## 15. [phase2] 12 客户端「路径假设」端点落地进度（更新 #1）
- ✅ 已实现：`scope/check`（20）、`coverage/result`（21）、`/tasks/{id}/finish`（24）。
- ⚠️ 未建：`POST /engagements/{eid}/task_runs`、`POST /tasks/{id}/events` 两个 REST 写路由（24 服务层已就绪，路由形态待 30/40 联调时定，12 客户端对齐）。
- ⏳ report latest → 41（阶段 2）。

## 16. [phase2] 12 客户端缺 `create_intent` 方法
- 25 已实现 intent 创建端点（routers/intents.py）；30 需要 `create_intent` 客户端方法，12 方法面缺失。30/40 联调时补 12 client + 同步映射表。

## 17. [open] graph spec §2.4 标 VALIDATION=400，代码统一 422
- 10 的 `ErrorCode.VALIDATION` 编码 422（v2 §7.3）；21/22/25 均按 422。graph spec §2.4 的 400 为旧值，建议同步 spec（先列 diff）。

## 18. [phase2] 30 replay_runs 结果回写端点未建
- 22 只登记 queued；`replay_runs` 结果（matched_original/result）回写端点缺失。阶段 2 补（22 或 41 侧）。

## 19. [phase2] 30 C8 计数持久化归 40
- reason_escalation 计数需落 `scheduler_state`（40 职责）；30 已返回 escalate 标志。

## 20. [phase2] replay 命令受控执行器通道（executor_url）未接入
- 30 replay/engine.py 的命令确定性重放需要 11/40 接入 `executor_url` 侧车通道；HTTP 重放经捕获代理已通。

## 21. [phase2] 21 `apply_audit_verdict` 签名偏离 skeleton §3
- skeleton 为 `(conn, audit_id, *, verdict)`；21 按 coverage spec §2 用 `(conn, eid, *, item_id, verdict, auditor, ...)` 一步式。阶段 2 决定统一口径并同步两处。

## 22. [phase2] 22 `retest_pass_count` 返回 dict（skeleton 为 int）
- 返回当前轮账本明细（更有信息量）。阶段 2 确认 41/30 消费端是否接受，需同步 skeleton。

## 23. [phase2] 24 `task_events.seq` 无并发锁
- 当前 `MAX(seq)+1`；Dispatcher 单进程串行上报无竞态。如需并发安全需 db.py 加 `UNIQUE(task_run_id, seq)`（报 10 后再落）。

## 24. [phase2] 21 `seed_from_discovery` 未实现
- bootstrap 播种覆盖项（依赖 20 `ensure_target`）；播种原语用 upsert_coverage_item。阶段 2 补薄封装。

## 25. [phase2] TV-01..46 全 skipped（mock 端到端）
- 31 已写好 harness + runner + 规则映射；需 40 loop + 进程内 Server 才可跑。50 作为验收入口复验。

## 26. [open] /projects auth 豁免未收窄（延续 #7）
- 25 已接管 /projects（遮蔽 10 占位），但未加路由级鉴权（避免破坏 test_server_foundation 豁免断言）。阶段 3 加固时收窄。

## 27. [env] 46 个测试因无 Docker skip
- container_archives/local_execution 类测试在无 Docker 环境 skip；真实容器验收留到有权限环境（同 #5）。

---

# Phase 2 新增/更新（2026-08-06 Phase 2 收尾）

## 28. [phase2] 40 新增 `server/routers/dispatch.py`（跨域写路由）
- 40（Dispatcher 侧）补建了 Server 端 task_runs/events/scheduler_state/expire/capture-reconcile 写路由——解决 #15 中 24 未建的 `POST /engagements/{eid}/task_runs` 与 `POST /tasks/{id}/events`。
- 功能可用（全量 468 绿），但归属跨域：Dispatcher 包写了 Server 路由。阶段 3 复核是否归入 24 或正式归属 40。

## 29. [phase2] 40 对 20 `scope.py create_engagement` 补 `conn.commit()`
- 20 的 seed 未提交 → test_types 播种永远为空（生产不可用）。40 已修复（注释标注）。20 应知悉。

## 30. [phase2] 40 对 22 `findings.py FindingCreate` 补 `detected_by`
- 22 的 Pydantic 模型 `extra="forbid"` 拒绝 12 客户端请求体（缺 detected_by）→ explore 写回 422。40 已补字段。22 应知悉。

## 31. [phase2] 40 对 13/31 测试适配
- `test_dispatch_cli.py::test_no_loop_wired_returns_2` 过时（loop 已接线，原测试会让 main_dispatch 真跑循环挂起）→ 已替换。
- `test_mock_end_to_end.py` e2e_ctx `raise`→`pytest.skip`（40 交付后 importorskip 不再跳过）。

## 32. [phase2] 41 报告数据源列缺失
- `replay_runs` 无 `created_at`（报告按 `started_at` 排序）；`verify_runs` 无 `engagement_id`（stats 经 findings JOIN 归属）。如需报告级查询更顺，可后续补列（报 10）。

## 33. [phase2] `reports_root` 未入 ServerConfig
- 41 当前从 db_path 派生 reports 根目录；ServerConfig（10 冻结）无该字段。需要时补 config + 文档。

## 34. [resolved] 测试垃圾清理
- 41 早期测试误传 `reports_root=str(db_conn)` 产生的 7 个 sqlite-connection 空目录 + `data/reports/` 已由编排者清理。根因是测试错误，非产品缺陷。

## 35. [phase2] 40 已知：mock reason 固定引用 c-001
- 静态 mock reason 固定引用覆盖项 c-001，被覆盖后重跑会 VALIDATION 失败（mock 假象非 bug）；loop 已加 reason 失败退避防饿死 verify。

## 36. [phase2/env] 40 未实现
- audit 自动抽样（需 21 暴露 pending audit 端点）；replay fixed 自动复测（需 41/22 编排）；task_events 原始流清理（服务端 cron）；Docker 下 kill 真实 SIGKILL 中断性（已用 11 接口模拟）。
