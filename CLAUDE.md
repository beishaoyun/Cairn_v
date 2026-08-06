# Cairn v2 — 渗透测试平台（从零重建）

> 本文件是仓库级全局约定，任何 Claude Code Agent 开工前**必须通读**。逐包开发提示词见 `dev-agents/`，编排与顺序见 `dev-agents/00-README.md`。

## 1. 项目一句话

把原 CTF 引擎 Cairn 重构为**授权渗透测试平台**：Cairn Server（协议真相源，SQLite 单写者）+ Dispatcher（调度执行器）+ 每 Engagement 精简沙箱 Worker 容器 + mitmproxy 透明代理捕获。核心差异化：**覆盖度矩阵驱动的「应测尽测」收敛**（替代原「目标达成」完成模型）+ **独立复核（verify）与确定性重放（replay）多源确认** + **捕获流量为证据真相源**。

## 2. 文档地图（权威性分级）

| 角色 | 文档 | 说明 |
|---|---|---|
| 总架构 | `docs/architecture-research-report-pentest-v2.md` | v2 唯一权威架构。§4 流程、§6 权限、§7 API/错误码、§8 模块、§12 隐藏规则 |
| **重建参考** | `docs/architecture-research-report-pentest-v3.md` | **下一代重建唯一开发参考**（12 章：分层架构/数据模型/鉴权/API/核心模块/异步机制/优缺点评估/§11 重构决策清单/§12 隐藏约束）。基于 v2 全量实现的后置分析；当前 v2 构建的权威仍是上两行（v2 架构 + DDL） |
| 数据 | `docs/database-ddl-draft.md` | **唯一权威 DDL**。建表/索引/约束/迁移/删除语义。§2.1 scope_policy、§4.1 ID 映射 |
| 覆盖引擎 | `docs/coverage-engine-implementation-spec.md` | §1 DDL+§1.1 默认测试目录、§2 伪代码、§3 输出契约、§4 热力图 |
| 捕获/复核/进度 | `docs/capture-verify-progress-spec.md` | §2 捕获、§4 verify、§6 复测、§7 进度、§8 存储、§9 安全 |
| 后端骨架 | `docs/backend-module-skeleton.md` | §1 目录、§2 API 清单、§3 服务签名、§4 校验器 |
| Dispatcher 配置 | `docs/dispatch-config-spec.md` | dispatch.yaml 完整 schema |
| Worker 沙箱 | `docs/worker-sandbox-hardening.md` | Dockerfile、容器加固、CA 注入 |
| Prompt 模板 | `docs/prompts-pentest-templates.md` | §1-§6 各 Agent prompt、§7 占位符、§8 校验器、§10 AGENTS.md |
| 探索图子域 | `docs/exploration-graph-spec.md` | **图协议唯一权威**：§3 服务签名、§4 图规则 28 条、§5 路由、§7 验收。从 0 重建 graph.py 的依据 |
| 前端 | `docs/frontend-progress-view-design.md` | 任务活动面板 + 事件流 + SSE |
| 人工流程 | `docs/human-workflow-guide.md` | 仅人工操作语义（状态升级/豁免/finalize） |
| 测试 | `docs/verify-mock-test-spec.md` | mock 驱动回归 46+ 用例（**验收测试的权威来源**） |
| 运维 | `docs/ops-runbook.md` | 部署/密钥/巡检 |
| **规则注册表** | `docs/rule-registry.md` | **规则编号（A/B/C/D/F/O/TV）唯一事实来源**。代码注释里写规则号时用它解析 |

**已废弃/对照（勿作实现依据）**：`docs/architecture-research-report.md`、`docs/specs/*.md`（v1），仅作历史参照。`docs/` 下其他未列出的 v1 文件同理。
> **唯一例外（保留模块从 0 重建）**：Worker 驱动/执行后端/心跳取消的细节只存在于 v1 `architecture-research-report.md` §8.4-8.6——`dev-agents/13-dispatcher-runtime.md` 明确以它为对照权威，其余包不要直读 v1。v1 §12 的图规则已并入 `exploration-graph-spec.md`，图子域无需直读 v1。

## 3. 黄金不变量（任何实现不得违反）

1. **Server 是唯一 DB 写者**（SQLite WAL、`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=NORMAL`）。Dispatcher 一律通过 HTTP 调 Server，绝不直连 DB。
2. **Agent 容器绝不注入 Cairn API token**（C5）；Agent 拿不到 Server 地址/凭据。Dispatcher 与捕获代理持 token。
3. **仅人工终态**：`fixed`/`closed`/`false_positive`/`accepted`/`finalize`/豁免 只能人工触发；Agent 只能建 `open` finding + 补证据。verify 只能产生 verdict 证据。
4. **捕获为证据真相源**（C2/F2）：Web 类请求/响应以捕获字节为准，digest 只喂模型（≤digest_budget），全量另存文件。
5. **覆盖度收敛替代完成判定**：不存在 `complete` 字段；reason 不能输出 `complete`，bootstrap 用 `sweep_complete` 表「初探完成」。
6. **规则编号即需求 ID**：新增/改动规则先改 `rule-registry.md`，再谈实现。
7. **枚举值必须与 DDL CHECK 完全一致**（engagement/finding/coverage/verify/task status 等）。
8. **时间戳一律 ISO8601 UTC 字符串**；ID 一律走 `counters`/`scoped_counters`/`engagement_counters`（图子域 `proj_###/f###/i###/h###` 走 `scoped_counters`，见 exploration-graph-spec §1；其余走 DDL §4.1 映射表），`test_types` 用 `tt_<slug>` 幂等键。
9. **capture 模式强制 bridge 网络**（C12 归属反查前置）；host 网络仅 local/演练且显式标注无兜底。
10. **不引入未明示依赖**：不装 Kafka/Redis/ORM。DB 用 sqlite3 stdlib；Web 用 FastAPI + pydantic；前端 Vite + Alpine.js 类轻量栈。
11. **不推翻已确认设计**：单团队/单 token、SQLite 单写者、Cairn Server+Dispatcher、沙箱+mitmproxy、覆盖度收敛。任何「更优方案」都先问，不要自行推翻。

## 4. 编码约定

- **目录结构**：严格按 `docs/backend-module-skeleton.md` §1（`src/cairn/{server,dispatcher}` 双层，server 下 routers/services/子域）。
- **服务层**：无状态函数式，每请求短事务（skeleton §3 签名即契约）。**改签名必须同步 skeleton §3**。
- **错误响应**：统一 `{"error_code": "...", "message": "...", "detail": ...}`，错误码枚举见 v2 §7.3，禁止裸 FastAPI HTTPException 明文。
- **API**：路由路径/方法/鉴权以 skeleton §2 为准；分页 `offset/limit`；列表直出 DTO。
- **现有 `cairn/src/` 是 v1 参考**：v2 按文档重建；文档标「保留」的模块（graph/heartbeat/cancellation/process/local_* 等）可迁移复用，标「改造」的必须按 v2 语义重写。
- **测试**：mock 回归以 `verify-mock-test-spec.md` 为权威用例清单；每个包至少覆盖自己验收点（各 spec「验收要点」节）。

## 5. 多 Agent 开发协议

- 每个包一个独立 Agent，工作区同一仓库；**写冲突面**（如 server/db.py、skeleton 签名、rule-registry）由 CLAUDE.md + 阶段顺序约束，见 `dev-agents/00-README.md`。
- 改他人包接口前，先确认该包已冻结（阶段顺序保证）；否则只加不改。
- 完成一个包 → 写 `dev-agents/notes/<包>.md` 交接物（实现清单、未实现、依赖、留给下游的假设），再进入下一阶段。

## 6. 验证命令

```bash
uv run --project cairn pytest -q            # 全量（mock 回归在 verify-mock-test-spec 覆盖范围内）
uv run --project cairn python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ['dispatch.example.yaml','dispatch_mock.yaml','dispatch.local.example.yaml']]"
uv run --project cairn cairn serve --host 0.0.0.0 --no-access-log   # 冒烟
```
