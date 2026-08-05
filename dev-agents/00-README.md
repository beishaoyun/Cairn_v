# Cairn v2 多 Agent 开发编排

> 每个文件 = 一个可直接交给 Claude Code Agent 的提示词。用法：
> `claude` 启动后把对应 `.md` 内容整体粘贴，Agent 会自动读 CLAUDE.md + 对应 docs。并行多个 Agent 时各开一个 session。

## 阶段与依赖

```
【阶段 0 · 串行，必须先完成】
  10-server-foundation   ──►  所有人依赖（DB/鉴权/错误码/app 装配）
  11-worker-sandbox      （可并行：独立于 Server）
  12-dispatcher-config   （可并行：config.py + protocol client，按契约先行）
  13-dispatcher-runtime  （可并行：执行抽象 + claude/codex/pi 驱动 + cancellation + dispatch CLI 入口）

【阶段 1 · 可并行，依赖 10 完成】
  服务端子域（互不写对方文件，接口由 skeleton §2/§3 冻结）：
    20-engagement-scope    21-coverage-engine    22-findings
    23-capture             24-progress-timeline  25-graph-subdomain
  Dispatcher 任务（依赖服务端 API 契约，可先按契约写，联调在阶段2）：
    30-dispatcher-tasks    31-mock-adapters   （31 依赖 13 的 WorkerDriver 协议）

【阶段 2 · 依赖阶段1产物】
  40-dispatcher-loop   （主循环/guards/reconcile/进度流接线——要 30 提供任务实现 + 13 的 CLI/驱动接口）
  41-report-finalize   （要 20/22 的状态机 + 24 时间线）

【阶段 3】
  42-frontend          （按前端文档 + 服务端契约，可用契约 mock）
  50-reviewer          （全量对拍 docs vs 实现 + 跑 verify-mock-test-spec）
```

## 冲突面与守则

| 冲突文件 | 归属 | 守则 |
|---|---|---|
| `cairn/src/cairn/server/db.py`（DDL） | 10-server-foundation | 只允许该包写；后续包若要加表/索引，报给 10 或改 `database-ddl-draft.md` 后再由 10 落库 |
| `cairn/src/cairn/server/services/graph.py` + `routers/{projects,intents,hints,export}` | 25-graph-subdomain | 唯一写者；服务签名冻结于 `exploration-graph-spec.md` §3 + skeleton §3 |
| `cairn/src/cairn/dispatcher/runtime/` + `workers/`（协议层） | 13-dispatcher-runtime | 协议定义者；11（容器/local 后端）/31（mock 驱动）按协议实现，不重复定义抽象 |
| `backend-module-skeleton.md` §3 签名 | 全局 | 任何包改服务签名必须同步该文档（文档是契约源） |
| `rule-registry.md` | 全局 | 新增/改规则号先更新注册表 |
| `docs/` 任意 | 全局 | 实现与文档冲突时：先查哪个版本旧，改文档走「先列 diff 再改」，不静默改文档掩盖实现 |

## 交接协议

每包完成后写 `dev-agents/notes/<包名>.md`，包含：
1. 实现清单（文件 + 关键符号）
2. 未实现/待定（明确写出，不藏）
3. 对下游包的依赖假设
4. 自测结果（贴关键输出）

下一阶段 Agent 开工前先读 `dev-agents/notes/` 里依赖包的交接物。

## 全局验收口径

- 每包验收 = 该 spec 的「验收要点」节（coverage §5 / capture §10 / frontend §9 / worker-sandbox §8）+ `verify-mock-test-spec.md` 对应用例。
- 阶段 3 末尾跑一次全量 mock 回归（31 提供的 mock 适配器 + 50 的 reviewer）。
- 规则编号引用以 `rule-registry.md` 为准，出现不认识/冲突的编号先查注册表。
