# 10-server-foundation 交接物

- 完成 Agent：10-server-foundation  日期：2026-08-06
- 阶段：Phase 0 · 串行第一包（所有业务子域依赖本包的数据层/鉴权/错误码/app 装配）

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `cairn/src/cairn/config.py` | `ServerConfig`（frozen dataclass）、`get_config()`、`DEFAULT_PAGE_SIZE/MAX_PAGE_SIZE` | 服务端配置：DB 路径 / CAIRN_API_TOKEN / evidence_root / traffic_root / archive_root / 分页默认；全部可用 `CAIRN_*` 环境变量覆盖 |
| `cairn/src/cairn/cli.py` | `main()`、`_serve`、`_dispatch`、`build_parser` | `serve`（uvicorn 起 app，`--host/--port/--db/--no-access-log/--reload`）；`dispatch` 整体透传 argv 给 13 的 `main_dispatch`，模块不可导入时打印提示返回 1 |
| `cairn/src/cairn/server/db.py` | `connect`、`init_db`、`get_db`、`next_id`、`test_type_id`、`SCHEMA_TABLES_DDL`、`SCHEMA_INDEXES_DDL` | **唯一落库者**。DDL §1-§9 全 30 表 + 25 索引 + 3 FTS5；v1→v2 迁移（含 VACUUM INTO 备份）；统一 ID 计数器（A1/A4 §4.1 映射） |
| `cairn/src/cairn/server/errors.py` | `ErrorCode`、`CairnError`、`error_payload`、`code_for_http_status` | v2 §7.3 全部 15 个错误码 + `INTERNAL`(500) 兜底 |
| `cairn/src/cairn/server/models.py` | `ErrorResponse`、`Page`、`PageResult`、`pagination_params`、~25 个 StrEnum | 分页/错误响应基础 + 通用枚举（值逐字符对齐 DDL CHECK，下游直接复用） |
| `cairn/src/cairn/server/middlewares/auth.py` | `BearerAuthMiddleware`、`default_exempt_paths`、`default_token_provider` | Bearer 纯 ASGI 中间件；缺/错 token → 401 AUTH_REQUIRED/AUTH_INVALID；豁免 GET /health 与 GET /projects |
| `cairn/src/cairn/server/routers/settings.py` | `router`、`get_settings`、`put_settings` | GET/PUT /settings（单例行 rowid=1，PUT 支持部分更新） |
| `cairn/src/cairn/server/app.py` | `create_app`、`register_business_routers`、`_register_exception_handlers`、`_mount_static` | FastAPI 装配：init_db → Bearer 中间件 → 全局异常 handler → settings → 业务路由注册点 → /health + /projects 占位 → 静态托管 |
| `cairn/src/cairn/server/static/.gitkeep` | — | Vite dist 目录占位（阶段 3 前端产物） |
| `cairn/tests/test_server_foundation.py` | 13 个测试 | 建库/表数/PRAGMA、计数器自增、settings PUT→GET、401/404/422 错误码形状、/projects 与 /health 豁免、v1 迁移、DELETE engagement 级联冒烟 |

`cairn/src/cairn/__init__.py`（`__version__="0.2.0"`）由 13 先行创建且内容正确，未改动。

## 2. 未实现 / 待定

- **业务路由全部未实现**（engagements/targets/projects/findings/coverage/capture/progress/report/export/timeline 归 20-25/41）：本包只提供注册点。
- **`/projects` 是占位端点**（返回 `[]`），由 25-graph-subdomain 接管替换；auth 豁免也是占位期的。
- **dispatch 完整逻辑**：由 13 的 `cairn.dispatcher.cli.main_dispatch` 实现，本包只透传。
- **test_types 默认目录播种 / FTS 同步触发器 / 证据目录物理创建**：归 21/23 等业务包，本包不预置。
- **静态前端**：`server/static` 只有 `.gitkeep`；`_mount_static` 仅在存在 `index.html`（Vite dist 产物）时挂载。

## 3. 对下游包的依赖假设

- **业务路由注册**：下游 Agent 新建 `cairn/server/routers/<模块>.py`，模块内定义并导出 `router: fastapi.APIRouter`，`app.register_business_routers` 自动发现挂载，无需改 `app.py`（避免写冲突）。模块导入失败会静默跳过并打 warning——所以业务路由模块要能独立导入。
- **DB 访问**：路由函数用 `db: sqlite3.Connection = Depends(get_db)`（`cairn.server.db.get_db`，请求级短连接，已施加 WAL/FK/busy_timeout）。枚举列名一律小写、snake_case（DDL 直出）。
- **ID 生成**：一律 `from cairn.server.db import next_id`；engagement 作用域 `next_id(conn, kind, engagement_id=eid)`，全局 `next_id(conn, 'engagement')`；图子域 scoped（`f###/i###/h###`）由 25 自实现。`test_types` 幂等键 `test_type_id(slug)`。
- **错误码**：业务代码 `raise CairnError(ErrorCode.XXX, message=..., detail=...)`，全局 handler 输出 `{"error_code","message","detail"}`。
- **通用枚举**：复用 `cairn.server.models` 的 StrEnum（值即 DDL CHECK 字符串），勿重复定义导致漂移。
- **settings 单例**：DB settings 表（rowid=1）由 init_db 回填；GET/PUT /settings 已实现，业务可直接读该表（coverage_policy 为 JSON 文本列）。
- **鉴权**：服务端 Bearer 中间件统一拦截，业务路由无需重复鉴权；`GET /settings` 等列表/写接口均需 token。

## 4. 自测结果

- `uv run --project cairn pytest cairn/tests/test_server_foundation.py` → **13 passed**（含 PRAGMA、计数器、settings 回读、401/422/404、迁移、级联删除）。
- `cairn serve --host 127.0.0.1 --port 8765 --no-access-log --db /tmp/cairn_smoke/cairn.db` 冒烟 → `GET /projects` = `[]` HTTP 200；`GET /health` 200；`GET /settings`（无 token）= 401 AUTH_REQUIRED（错误体形状正确）。
- `cairn dispatch --config /nonexistent.yaml` → 透传 13，配置加载失败 exit=1（占位链路正常）。
- 全量 `pytest -q` = 81 passed + **4 failed 均在 `test_protocol_client.py`（12 的包）**：其 stub server 把 `authorization` 当 query 参数读（应 `Header()`），未读真实 Bearer 头 → 恒 401。**与本包无关**（stub 自包含，不 import cairn.server.app）。
- `dispatch*.yaml` 三文件 YAML 解析 OK（12 交付物）。

## 5. 给下游的注意事项

- **app 注册点**：`cairn.server.app.register_business_routers` 自动发现 `cairn.server.routers` 下所有非 settings 模块的 `router` 并 include。20-24 只需新增模块文件；若某 Agent 需要显式控制挂载顺序，可自行改 `BUSINESS_ROUTER_MODULES`（文档用途）或直接在 app.py 显式 include（本文件写冲突面以 orchestrator 协调为准）。
- **级联删除实测结论**：SQLite 按「引用方子表创建顺序」执行级联。DDL 顺序保证 `findings`（含 finding_http_evidence/replay_runs/finding_traffic_links）先于 `traffic_entries` 被删，故 `finding_http_evidence.traffic_id`、`replay_runs.trigger_traffic_id` 两个**无 ON DELETE 的 FK** 不触发违约。`DELETE engagement` 冒烟已实测通过；**不要改动建表顺序**，否则边角 FK 会违约。
- **迁移**：老库先 `VACUUM INTO 'backup_<ts>.db'`（跳过空库）；`projects.engagement_id` 补列（带 CASCADE FK）、`bootstrap_mode→bootstrap_enabled`（RENAME COLUMN）、settings 补 4 列；counters 保留 v1 数值不归零。已知局限：v1 `projects` 若带旧 status CHECK（含 completed）不会重建（SQLite ALTER 无法改 CHECK），仅补列。
- **DDL 拆分**：索引与 FTS5 在 `SCHEMA_INDEXES_DDL`（建表+补列之后再执行），否则 v1 老库建 `idx_projects_eng` 会因列未补报错。加表/加索引请同时改两处常量并**保持 SCHEMA_TABLES_DDL 顺序**。
- **鉴权豁免**：`GET /projects` 目前豁免鉴权（健康冒烟用，v2 §6.3「可豁免」）。25 接管 /projects 后由编排者决定是否收窄（生产上应去掉豁免或换 /health 专用）。
- **`next_id` 依赖 `UPDATE ... RETURNING`**（SQLite ≥ 3.35）；环境已确认 3.40.1。若需兼容更老 SQLite 再改 UPDATE+SELECT。
- **422 校验错误**：全局 handler 包 `error_code=VALIDATION` 但 `detail` 保留 FastAPI `exc.errors()` 结构。
- **静态托管**：`app._mount_static` 只在 `server/static/index.html` 存在时挂载 `/`，阶段 3 前端构建后生效；空目录不会吞 API 404。

## 6. 修复记录：业务 kind ID 从 engagement 作用域改为全局计数器（2026-08-06）

**问题**：`targets.id`/`findings.id`/`coverage_items.id` 等 16 个业务表 ID 是全局 `TEXT PRIMARY KEY`，
但 `next_id` 原实现对每个 engagement 从 `t-001`/`fd-001`/`c-001` 起独立计数（写 `engagement_counters`）。
第二个 engagement 即与首个 engagement 复用同一 ID → PK 冲突（20/21/22/23 复现，test_scope 1 例 +
test_coverage 4 例失败）。

**方案（用户已批准 A）**：DDL §4.1 已更新——16 个业务 kind 的 ID 改为**全局**三位补零自增，经
`counters` 表（name=kind）唯一授予，跨 engagement 全局唯一。`engagement_counters` 表保留但**停用**
（仅兼容历史迁移/旧库），新代码不再写入。

**改动**（仅 `cairn/src/cairn/server/db.py` + `cairn/tests/test_server_foundation.py`）：

- `next_id(conn, kind, engagement_id=None)` 对非 `'engagement'` 的业务 kind 改用全局
  `counters` 表：`INSERT OR IGNORE INTO counters (name, value) VALUES (?, 0)` +
  `UPDATE counters SET value=value+1 WHERE name=? RETURNING value`（与 `kind=='engagement'`
  分支同一原子机制）。前缀格式不变（`t-###`/`fd-###`/`c-###` 等）。
- **函数签名不变** `next_id(conn, kind, engagement_id=None)`；`engagement_id` 对业务 kind
  **忽略**（docstring 已说明）。`test_type_id` 不动。
- `test_server_foundation.py::test_counters_increment` 断言从「不同 engagement 独立计数
  （t-001 重启）」改为「全局唯一（跨 engagement 不重启、不传 engagement_id 也继续自增）」；
  删除了「缺 engagement_id → ValueError」断言（该参数现为可选）。
- 迁移无需改动：v1→v2 迁移只建 `engagement_counters` 表、不回填；`next_id` 首次使用自动
  播种 counters 行。迁移冒烟（`test_migrate_v1`）仍过。

**给下游的语义说明**：

1. **`engagement_id` 参数对业务 kind 已废弃**：20-24 现有调用 `next_id(conn, kind, engagement_id=eid)`
   继续可用（参数被忽略），新代码可直接 `next_id(conn, "target")`。不要依赖「不同 engagement
   的 target/finding 从 -001 重启」——现在全局唯一，跨 engagement 连续。
2. 依赖 `targets.id`/`findings.id` 等具体值的测试/代码，**不得硬编码跨 engagement 复用的
   `-001`**；同一连接内按自增顺序取实际 ID（`next_id` 返回即用）。
3. `engagement_counters` 表不再被写入：任何依赖它的查询/回填应改走 `counters` 表
   （name=kind）。图子域 `proj_###/f###/i###/h###` 仍走 `scoped_counters`，不受影响。
4. task_runs/task_events 的 `task-###`/`ev-###` 由 Dispatcher 侧全局生成
   （`progress.py::_global_next_id`，name='task'/'event'），与本修复无关，仍保持。
