# 25-graph-subdomain 交接物

- 完成 Agent：25-graph-subdomain  日期：2026-08-06
- 阶段：Phase 1 · 从 0 重建探索图子域（无 v1 代码可迁移）
- 依赖：10（server 基座：db/errors/models/auth/app 装配）。被依赖：20（B5 冻结接线）、21/22（conclude 三子域编排）、24（facts.created_at 时间线只读）、30/40（图协议客户端调用面）、13（graph.yaml 快照消费）。

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `cairn/src/cairn/server/services/graph.py` | `create_project`/`get_project`/`list_projects`/`delete_project`/`set_project_title`/`set_project_status`/`next_scoped_id`/`create_fact`/`create_intent`/`claim_intent`/`heartbeat_intent`/`release_intent`/`conclude_intent`/`create_hint`/`claim_reason`/`heartbeat_reason`/`release_reason`/`freeze_project_leases`/`intent_timeout_cleanup`/`reason_timeout_cleanup`/`export_graph_yaml`/`list_facts`/`list_intents`/`list_hints`/`next_project_id` | 探索图协议全部服务（spec §3 契约冻结逐条实现） |
| `cairn/src/cairn/server/routers/projects.py` | `router`（prefix `/projects`） | CRUD + reason 租约 + 详情摘要（facts/intents/hints） |
| `cairn/src/cairn/server/routers/intents.py` | `router`（prefix `/projects/{pid}/intents`） | 创建 / claim / heartbeat / release / **conclude 三子域编排** |
| `cairn/src/cairn/server/routers/hints.py` | `router`（prefix `/projects/{pid}/hints`） | hint 写入 |
| `cairn/src/cairn/server/routers/export.py` | `router`（prefix `/projects/{pid}/export`） | 图 YAML / timeline 导出 |
| `cairn/tests/test_graph.py` | 24 个测试 | §3 八项验收全覆盖（服务级 + 路由级） |

## 2. 核心实现决策

### 2.1 ID 生成（spec §1 / DDL §4.1）
- `proj_###` 走**全局 counters**（name=`'project'`），与 `eng_###` 独立——`next_project_id` 在 graph.py 内自实现（10 的 `next_id(kind='engagement')` 不是它）。
- `f###/i###/h###` 走 `scoped_counters`（kind=fact/intent/hint），各自独立 `%03d`，经 `next_scoped_id` 唯一授予，禁裸自增。
- 播种 origin=f001、goal=f002（create_project 时经 create_fact 落库）。

### 2.2 Fact 只增不改 + 幂等
- 无 update/delete 路径；`create_fact` 同 project 内重复 description 幂等跳过并返回已有事实。
- `facts.created_at` 统一微秒级 ISO8601 UTC，供 D3 时间线排序。

### 2.3 Intent 校验（spec §4-1/2）
- from_fact_ids 全部存在（404 NOT_FOUND）且**不含 goal**（VALIDATION）；
- to_fact_id 存在（404）且**非 goal**（VALIDATION，A2 无 `to='goal'` 完成边）；
- `worker∈{null,creator}`（VALIDATION）；creator 不可变（无更新路径）；
- create_intent 额外校验项目 active（403，spec §4-7：reason intent 创建遇 403 视作成功收场）。

### 2.4 租约仲裁（intent 级 + reason 级）
- `_assert_leaseable`：已 conclude → 409；`worker != 请求者` → 409 LEASE_CONFLICT；项目非 active → 403 PROJECT_INACTIVE；不存在 → 404 NOT_FOUND。
- **首次心跳即认领**：`heartbeat_intent` 在 worker=NULL 时置为请求者（spec §5）。
- 额外注册 `POST /{iid}/claim` 路由（12 客户端路径假设 `graph.claim_intent`），与 heartbeat 同语义。
- reason 租约：`claim_reason`（403 非 active / 409 他人持有 / 写 reason_worker+started_at+trigger+last_heartbeat_at）、`heartbeat_reason`（409 他人持有）、`release_reason`（仅持有者，409 他人持有）。

### 2.5 conclude 三子域编排（spec §5；同请求同事务）
`POST /projects/{pid}/intents/{iid}/conclude` body `{worker, facts[]?, coverage_result?, findings[]?}`：
1. `graph.conclude_intent`：写 facts（只增幂等）→ `concluded_at` 置位 → 释放 intent 租约；
2. `coverage_result` 转发 21 `services.coverage.write_coverage_result`（B1 认领校验在 21 内部）；
3. `findings[]` 转发 22 `services.findings.create_finding`（actor='agent' 只能 open）。
- 任一子域失败 → 整请求抛错、不 commit（连接关闭回滚）——同事务原子性（有测试验证）。
- `coverage_result.fact_id` 缺省时溯源本次产出的首个 fact；`findings[].source_fact_id` 缺省时同源（只溯源不阻塞，spec §6）。
- 21/22 经 **import 守卫**接入（`from ..services import coverage/findings as _xxx_svc`）；本仓库两者均已就绪，**真调用路径**（非 TODO 兜底）。

### 2.6 freeze_project_leases（B5）
- 签名 **`freeze_project_leases(conn, pid) -> None`**（与 exploration-graph-spec §3 一致）。
- 清全部 open intent（concluded_at IS NULL）的 worker + reason 租约（reason_worker/trigger/started_at/last_heartbeat_at 全清）。
- `set_project_status('stopped')` 内部立即调用（stopped = paused 语义）。
- **20 的 `services/scope.py#_freeze_engagement_leases` 已按此签名接线**（import 守卫，25 就绪后即真调用）——无需再改 20。

### 2.7 超时清理（读前执行）
- `intent_timeout_cleanup(conn, pid=None) -> list[str]`：读 settings.intent_timeout（默认 15），open 且 worker 非空且 last_heartbeat 超时 → worker=NULL；**已 conclude 不参与**（查询排除）。
- `reason_timeout_cleanup(conn, pid=None)`：读 settings.reason_timeout，reason 租约超时 → 清空。
- 调用点：`list_projects`/`get_project`/`export`（读前执行 + commit，读到的即清理后状态）。

### 2.8 导出
- `export_graph_yaml`：YAML 快照含 project + origin/goal/全部 fact/intent/hint（intent 含 from_fact_ids），`yaml.safe_dump(sort_keys=False)`；可被 13/30 图快照逻辑消费（spec §4-14）。
- `format=timeline`：事实增量 JSON，支持 `?after_ts=`（D3 时间线源）。

## 3. 与相邻包的接口契约

### 给 20（B5 冻结）
- `services/graph.freeze_project_leases(conn, pid) -> None`（spec §3）。20 已在 `_freeze_engagement_leases` 接线（scope.py:228-241），paused/expire 时对每个 project 调用。**已真调用**（graph.py 可导入）。

### 给 21/22（conclude 编排）
- 21 `services.coverage.write_coverage_result(conn, eid, *, item_ids, depth_achieved, outcome, fact_id, intent_id, evidence_refs, tested_scope, partial)`——**存在可用**（services/coverage.py:452）。
- 22 `services.findings.create_finding(conn, eid, *, payload, detected_by, actor='agent')`——**存在可用**（services/findings.py:325）。
- conclude 路由同事务调用两者；若后续 21/22 签名变更，改任一侧同步 skeleton §3 + 本文件。

### 给 24（时间线）
- `facts.created_at` 是 D3 六源之一（只读）。`list_facts(conn, pid, after_ts=..., limit=...)` 提供增量；`export?format=timeline` 已暴露。

### 给 30/40（图协议客户端调用面）
- 12 客户端映射（对齐 12 交接物 §3）：`claim_intent`→`POST /projects/{pid}/intents/{iid}/claim`、`heartbeat_intent`→`/heartbeat`、`release_intent`→`/release`、`conclude_intent`→`/conclude`（worker+facts[]）、`export_yaml`→`GET /projects/{pid}/export?format=yaml`。
- 错误语义（spec §2.4）：403=项目非 active（reason 写回/创建视作成功收场，release 对 403/409 静默）；409=竞争丢失跳过该条继续。
- reason 租约：`POST /projects/{pid}/reason/claim|heartbeat|release` body `{worker}`。

### 给 13（graph.yaml 快照）
- `GET /projects/{pid}/export?format=yaml` 返回 `application/yaml` 纯文本；schema 见 §2.8（project/facts/intents/hints 四段）。

## 4. 路由清单（spec §5 全部落地）

| 方法/路径 | 说明 |
|---|---|
| GET `/projects?engagement_id=&status=` | 列表（读前超时清理）；**遮蔽 10 占位** |
| POST `/projects` | 创建 + 播种 origin/goal |
| GET `/projects/{pid}` | 详情 + facts/intents/hints 摘要 |
| DELETE `/projects/{pid}` | 物理级联删除（204） |
| PUT `/projects/{pid}/title` | 标题 |
| PUT `/projects/{pid}/status` | active\|stopped（A2 无 completed）；stopped 即冻结 |
| POST `/projects/{pid}/reason/claim` | 204 / 403 / 409 |
| POST `/projects/{pid}/reason/heartbeat` | 204 / 409 |
| POST `/projects/{pid}/reason/release` | 204 / 409 |
| POST `/projects/{pid}/intents` | 201（校验 VALIDATION/404） |
| POST `/projects/{pid}/intents/{iid}/claim` | 204 / 403 / 409（12 客户端路径） |
| POST `/projects/{pid}/intents/{iid}/heartbeat` | 204 / 403 / 409（首次心跳即认领） |
| POST `/projects/{pid}/intents/{iid}/release` | 204 / 403 / 409 |
| POST `/projects/{pid}/intents/{iid}/conclude` | 204（三子域编排；同事务） |
| POST `/projects/{pid}/hints` | 201（active/stopped 皆可） |
| GET `/projects/{pid}/export?format=yaml\|timeline` | yaml 文本 / timeline JSON |

## 5. 未实现 / 待定

- **GET /facts、GET /intents、GET /hints 独立列表端点**未暴露（spec §5 路由表无此三端点）；只通过 `GET /projects/{pid}` 详情摘要与 export 读取。如 30/40 需要，可加只读路由（服务 `list_facts/list_intents/list_hints` 已就绪）。
- **conclude 幂等键**：未加 `Idempotency-Key` 头去重（与 21 coverage_records 的 `(item_id, intent_id)` 应用层去重协同；conclude 本身无去重列）。若 12/30 需要重复 conclude 防重，需 DDL 加列（报 10/orchestrator）。
- **FTS facts 同步**：fts_facts 由 DDL 建表，本包未写同步触发器（与 24/progress 同批，可由 41/后续补）。
- **reason_trigger 来源**：`claim_reason` 接受可选 `trigger` kwarg（路由未透传，默认 None）；30/40 若需记录触发源（gaps/hint），可在 claim 路由 body 增加 `trigger` 字段。

## 6. 契约偏离 / 注意

1. **VALIDATION 状态码**：spec §2.4 表格标 HTTP 400，但 `errors.py#ErrorCode.VALIDATION` 编码为 **422**（10 已定，21/22 同款）。本包一律 `CairnError(ErrorCode.VALIDATION)` → 响应 `{"error_code":"VALIDATION"}` + HTTP 422。**测试断言 422**（与 test_findings.py 一致）。如需 400 需改 errors.py（报 orchestrator，勿自行改）。
2. **auth 豁免**：`GET /projects` 仍被 10 的 `middlewares/auth.py` 豁免（占位期遗留，phase0-alignment #7）。本路由**未加路由级鉴权**（否则破坏 test_server_foundation `test_health_and_projects_200` 的 200 断言）。生产上由 orchestrator 决定收窄豁免或换 /health 专用。
3. **12 client `conclude_intent`** 只发送 `facts`（list[str]），不含 coverage_result/findings——三子域编排是 spec §5 的完整形态，12 客户端当前子集仍可用（facts 只写图）。
4. **re-intent（release 后重新认领）**：`last_heartbeat_at` 在 release 时置 NULL；claim 时重新写入——超时清理对刚 claim 的 intent 不会误清（last_heartbeat 非空且新鲜）。

## 7. 自测结果

- `uv run --project cairn pytest cairn/tests/test_graph.py` → **24 passed**。
- 全量 `uv run --project cairn pytest -q` → 待 full suite 结果（见编排者汇总；本包测试 24 全绿，与 20/21/22 的 foundation/coverage/findings 测试共跑无冲突）。
