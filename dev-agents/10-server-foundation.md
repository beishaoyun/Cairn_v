# Agent 10 — 服务端基座（Server Foundation）

> 阶段 0 · 串行第一包。**所有其他包依赖你**。你的职责是把数据层、鉴权、错误码、app 装配一次性立起来，让业务子域 Agent 可以并行开工。

## 0. 开工前必读（按顺序）
1. `CLAUDE.md`（仓库根）——黄金不变量（尤其 1/7/8）
2. `docs/rule-registry.md` —— 全表通读，之后注释引用规则号以它为准
3. `docs/database-ddl-draft.md` —— **全文**（你是唯一负责落库的人，必须逐表核对）
4. `docs/backend-module-skeleton.md` §1（目录）、§2.1（配置路由）、§2.6（报告路由仅列契约）
5. `docs/architecture-research-report-pentest-v2.md` §7（API 规范/错误码）、§10（迁移）
6. `docs/capture-verify-progress-spec.md` §7.1 —— 只核对 task_runs 副本与权威 DDL 一致（已对齐，勿再引入差异）

## 1. 交付范围（创建/修改）
```
cairn/src/cairn/__init__.py         # __version__
cairn/src/cairn/cli.py              # serve / dispatch 子命令（dispatch 只起 dispatcher 入口占位，具体见 12）
cairn/src/cairn/config.py           # 服务端配置：DB 路径 / CAIRN_API_TOKEN / evidence_root / traffic_root / archive_root / 分页默认
cairn/src/cairn/server/__init__.py
cairn/src/cairn/server/app.py       # FastAPI 装配 + 全局异常 handler + Bearer 中间件 + 静态/前端托管
cairn/src/cairn/server/db.py        # 连接管理 + 全量 DDL（逐条对照 database-ddl-draft.md）+ 迁移逻辑
cairn/src/cairn/server/errors.py    # 错误码枚举（对齐 v2 §7.3）
cairn/src/cairn/server/middlewares/auth.py
cairn/src/cairn/server/models.py    # Pydantic DTO 基础（分页/错误响应/通用枚举）
cairn/src/cairn/server/routers/__init__.py
cairn/src/cairn/server/routers/settings.py
cairn/tests/test_server_foundation.py
```

## 2. 必须满足的契约
- **PRAGMA**：`journal_mode=WAL; foreign_keys=ON; busy_timeout=5000; synchronous=NORMAL`（DDL §0）。
- **全表**：DDL §1-§9 所有表 + 索引 + FTS5 虚拟表，逐条照抄（含 CHECK/UNIQUE/FK/CASCADE）。特别：`findings.target_id` 是 CASCADE（勿改 RESTRICT，见 DDL §5 注释）；`finding_http_evidence.traffic_id` FK 无 ON DELETE（依赖级联顺序，注释保留）；`task_runs.project_id` 可空（B2）。
- **计数器**：`counters`、`scoped_counters`、`engagement_counters`（DDL §4.1 kind 枚举）实现自增；`test_types` 用 `tt_<slug>` 幂等键。提供统一 `next_id(conn, kind)`（engagement 作用域）与 `next_id(conn, 'engagement')`（全局）。
- **错误码**：`errors.py` 定义 `ErrorCode` 枚举，含 v2 §7.3 全部：`AUTH_REQUIRED/AUTH_INVALID/SCOPE_DENIED/KILL_SWITCH_ON/OUT_OF_AUTHORIZATION_WINDOW/PROJECT_INACTIVE/ENGAGEMENT_INVALID_STATE/LEASE_CONFLICT/FINDING_DUP/COVERAGE_DUP/COVERAGE_NOT_APPLICABLE/COVERAGE_ALREADY_COVERED/COVERAGE_POLICY_UNMET/NOT_FOUND/VALIDATION`。
- **统一错误响应**：全局 `HTTPException` handler 输出 `{"error_code","message","detail"}`；422 校验错误包 `error_code=VALIDATION` 但保留 FastAPI detail。
- **鉴权**：`middlewares/auth.py` Bearer Token 中间件（读 `CAIRN_API_TOKEN`，缺/错→401 `AUTH_REQUIRED/AUTH_INVALID`）；健康检查（`/projects` GET 或 `/health`）可豁免。
- **settings 单例**：`routers/settings.py` 暴露 `GET/PUT /settings`（skeleton §2.1），支持 `intent_timeout/reason_timeout/global_kill_switch/coverage_policy`（DDL §1）。
- **分页**：列表接口统一 `offset/limit` 参数（v2 §7.2）。
- **迁移**：实现 DDL §10 的 v1→v2 迁移（`ALTER TABLE projects ADD COLUMN engagement_id`、`bootstrap_mode→bootstrap_enabled`、settings 补列、计数器回填、`VACUUM INTO` 备份）。老库不存在时全新建库幂等。
- **app 装配**：`app.py` 挂 routers（settings 起，其余由各业务 Agent 挂入——你在 app 里留好注册点）；静态目录 `server/static`（Vite dist，阶段 3 前端产物）可空。

## 3. 验收标准（可执行）
1. `pytest cairn/tests/test_server_foundation.py` 通过：建库→建表数量 ≥ DDL 表数；`PRAGMA foreign_keys` 生效；计数器自增正确；settings PUT 后 GET 回读；401/错误码形状断言。
2. 迁移冒烟：用 v1 库文件跑一次迁移不报错（测试里构造最小 v1 库）。
3. `uv run --project cairn cairn serve --host 0.0.0.0` 能起，`GET /projects` 200。
4. **级联删除冒烟**：`DELETE engagement` 不触发 FK 违约（覆盖 findings.target_id、traffic_id 两处边角，DDL §5/§9.1 注释所述）。

## 4. 硬约束
- **不写业务路由**（engagements/findings/coverage/capture/progress/report 留给对应 Agent）；只写 settings + 健康 + 错误/鉴权基建。
- **不建任何表之外的表**；要加表先改 `database-ddl-draft.md`（先列 diff 再改）再落库。
- 不用 ORM；`db.py` 用 `sqlite3` 标准库 + 每请求短事务（开即用、用完关或请求级连接）。
- 枚举字符串必须与 DDL CHECK 逐字符一致（大小写敏感）。

## 5. 交接物
写 `dev-agents/notes/10-server-foundation.md`：表清单、错误码枚举、迁移支持、留给下游的 app 注册点说明、已知风险（如级联顺序实测结论）。
