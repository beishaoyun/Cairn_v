# 20-engagement-scope 交接物
- 完成 Agent：20-engagement-scope  日期：2026-08-06
- 阶段：Phase 1 · 授权范围子域（Engagement / Target / Scope Guard）

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `cairn/src/cairn/server/services/scope.py` | `create_engagement` / `transition_status` / `check_engagement_writable` / `check_scope_allowed` / `check_kill_switch` / `expire_engagements` / `set_kill_switch` / `get_engagement` / `list_engagements` / `update_engagement` / `delete_engagement` / `create_target` / `list_targets` / `update_target` / `delete_target` / `get_target` | 状态机、窗口、熔断、scope guard、targets CRUD（skeleton §3 前 5 签名 + targets CRUD，v2 §8.9） |
| `cairn/src/cairn/server/routers/engagements.py` | `router` + GET/POST `/engagements`、GET/PUT/DELETE `/engagements/{eid}`、PUT `/status`、POST `/kill`、GET `/{eid}/scope/check`、POST `/finalize`（501） | 自动发现挂载，未改 app.py |
| `cairn/src/cairn/server/routers/targets.py` | `router` + GET/POST `/engagements/{eid}/targets`、PUT/DELETE `.../targets/{tid}` | 删除应用层 gate |
| `cairn/tests/test_scope.py` | 38 用例（37 passed + 1 skipped） | 验收五条全覆盖 |

关键设计：
- **状态机** `planning→active→paused→completed→archived`（DDL CHECK，A2）。转换表：
  `planning→{active}`、`active→{paused,completed}`、`paused→{active,completed}`、`completed→{active,archived}`、`archived→{}`（单向不可逆）。
  前置：`planning→active` 要求 ≥1 个 authorized target + 窗口合法（start<end 或两者皆空）+ kill off（kill 开时 423 `KILL_SWITCH_ON`，其余 409 `ENGAGEMENT_INVALID_STATE`）；`completed→active` 必须 `retest=true`。
- **B5 冻结**：置 paused 时调 25 的 `services/graph.freeze_project_leases(conn, pid)`（对 engagement 下每个 project）；`expire_engagements` 内部走 `transition_status(..., 'paused')` 统一触发冻结。import 守卫：25 未就绪仅打 warning，不阻塞。
- **Scope guard（v2 §12 规则 1）**：`check_scope_allowed` —— prohibited 命中即 403 `SCOPE_DENIED`（**无 fallback**，即使落在 authorized 大网段）；authorized 精确命中返回既有 target；authorized 包含命中（子域/CIDR 内）→ `auto_created=1` 建 target 后返回（F11/规则 22，幂等）；未命中返回 None。URL/IP/CIDR/domain/hostname 匹配与格式推断见 `_classify`/`_host_of`/`_matches`。
- **删除 gate**：`delete_target` 前检查未结算 findings（status ∉ {closed,false_positive,accepted}）与 coverage_items（status ∈ {untested,in_progress}），有则 409 + 引用清单；DB 层 CASCADE 保持不动（DDL 明确勿改 RESTRICT）。
- **targets 格式校验**：kind 由值自动推断（url/ip/cidr/domain/hostname），UNIQUE(engagement_id,value) 冲突 → 409 + detail。

## 2. 未实现 / 待定

- **finalize**：`POST /engagements/{eid}/finalize` 只留 501 占位（错误体 `error_code=NOT_IMPLEMENTED`），由 **41** 实现（校验覆盖策略 → 置 completed → 生成报告）。
- **SCOPE_DENIED 审计落库**：scope 拒入当前仅打日志。`finding_history`（FK finding_id）与 `task_events`（FK task_run_id）均无 scope 检查上下文（`check_scope_allowed(conn, eid, value)` 签名无 task/finding），强行写会伪造 FK。建议 30/40 在派发侧对拒入的 task_run 记 `task_events`（有 task_run_id 上下文）。
- **kill 的 SIGKILL 下发**：`POST /kill` 只置 `kill_switch=1`；立即 SIGKILL（C1）由 40/11 轮询 active engagement（含 kill 状态）落实，本包无直接通知通道。
- **跨包 import 守卫 TODO**：`_seed_default_test_types`（21）与 `_freeze_engagement_leases`（25）当前均走守卫；21/25 就绪后移除守卫即可真调用，接口签名见 §3。

## 3. 对下游包的依赖假设

- **21（coverage）· seed 契约确认：21 尚未提供**。`services/coverage.py` 不存在，`create_engagement` 内调用点已写好：
  `from .coverage import seed_default_test_types`；契约 `seed_default_test_types(conn, eid) -> None`（按 coverage-engine-spec §1.1 写入 test_types，enabled=1）。21 就绪后本调用即生效（无需改 scope.py）。**验收 5 的播种断言在 21 未就绪时 skip（当前 1 skipped 即此因）。**
- **25（graph）· 冻结契约**：`services/graph.freeze_project_leases(conn, pid) -> None`（exploration-graph-spec §3），paused 时对本 engagement 下全部 project 调用。
- 复用 10：`next_id(conn, kind, engagement_id=...)`（t-### / fh-###）、`CairnError`/`ErrorCode`、`server.models` 枚举（EngagementStatus/TargetKind/ScopeStatus）。`services/__init__.py` 未改动。

## 4. 自测结果

- `uv run --project cairn pytest cairn/tests/test_scope.py` → **37 passed, 1 skipped**（skipped=21 未就绪的播种断言）。
- `uv run --project cairn pytest -q` 全量 → **178 passed, 1 skipped**（无回归；此前 12 的 4 个 test_protocol_client 失败已随并行交付转绿）。
- `cairn serve` 冒烟：POST/GET `/engagements` 200、GET `/engagements` 直出数组（对齐 12 `list_active` 期望）、`/engagements/{eid}/scope/check` 200/403、PUT `/status` 状态机、DELETE targets gate 409 均验证通过。

## 5. 给下游的注意事项

- **给 41（finalize）**：`POST /engagements/{eid}/finalize` 501 占位待替换；置 completed 可用 `scope.transition_status(conn, eid, 'completed')`（无 retest 需求）；41 需要读取 `check_engagement_writable`/`check_kill_switch` 语义与 coverage `report_ready`。
- **给 40（dispatcher）**：列表/守卫端点契约与 12 客户端一致——`GET /engagements?status=active`（直出数组，含 kill_switch）、`PUT /engagements/{eid}/status` body `{"status", "retest"}`、`POST /engagements/{eid}/kill` body `{}`、`GET /engagements/{eid}/scope/check?value=`（authorized 命中 200+target / 其余 403 SCOPE_DENIED，fail-closed）。`check_scope_allowed` 返回 None 在路由层被映射为 403（歧义目标跳过语义由 40 自定）。
- **targets `scope` 键兼容**：POST/PUT targets 的请求体 `scope` 是 `scope_status` 的兼容别名（12 `create_target` 用 `scope` 键；响应统一输出 `scope_status`）。
- **⚠ 发现的 DDL 缺陷（跨包影响 21/22，需编排者裁决）**：`targets.id`（及 `findings.id`/`coverage_items.id` 等同为 `TEXT PRIMARY KEY` 全局唯一）但 ID 走 **engagement 作用域** `engagement_counters` 计数器——`next_id(conn,'target',engagement_id=)` 对每个 engagement 都从 `t-001` 起，**第二个 engagement 创建第一个 target 即 `UNIQUE constraint failed: targets.id`**。10 的 foundation 测试只测了 next_id 未跨 engagement 插行，未暴露。当前 scope 代码按契约用 next_id；多 engagement 同库场景会 PK 冲突。建议：DDL 改为复合主键 `(id, engagement_id)` 或 ID 前缀含 engagement 序，需 10/编排者定夺（本包未改 DDL）。
- **409 错误码选择**：target 重复与删除 gate 的 409 因 ErrorCode 枚举无 target 专属码（`COVERAGE_DUP` 不适用），统一用 `ENGAGEMENT_INVALID_STATE` + 明细（契约仅要求 409+detail，未要求具体码）。如需专属码，应在 `errors.py` 增补（10 冻结文件，需协调）。
- **窗口语义**：`planning→active` 要求窗口 start<end 或两者皆空（单端给一半即 409）；`expire_engagements` 只处理 `authorized_end_at <= now` 的 active（无 end 的永不过期）。时间戳统一 `%Y-%m-%dT%H:%M:%SZ`，解析兼容 `+00:00`。
- **auto_created target 的 note**：scope guard 自动建 target 写 `note='scope guard auto-created'`、`added_by='agent'`；22（findings）的 `resolve_target` 可复用 `check_scope_allowed` 的 auto-created 语义（F11/规则 22）。
