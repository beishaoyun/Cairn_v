# Agent 25 — 探索图子域（Graph Subdomain）

> 阶段 1 · 与 20-24 并行。你的职责是重建**探索图协议**（Fact 只增不改 / Intent 超边 / Hint / 双租约）——v2 保留的核心能力，**从 0 重建，无 v1 代码可迁移**。

## 0. 开工前必读（按顺序）
1. `CLAUDE.md`（黄金不变量 1/7/8）
2. `docs/exploration-graph-spec.md` —— **全文（你的规格，唯一实现依据）**：§2 原语、§3 服务签名（契约冻结）、§4 图规则 28 条、§5 路由、§7 验收
3. `docs/backend-module-skeleton.md` §1（server 目录）、§2.4（探索图路由清单）、§3（服务签名——本文件 §3 与 skeleton 须一致）
4. `docs/database-ddl-draft.md` §3（projects/facts/intents/intent_sources/hints/scoped_counters 表）、§4.1（ID 规则）
5. `docs/architecture-research-report-pentest-v2.md` §4.3-4.5（bootstrap/reason/explore 流程如何消费图）、§5.2（ER）、§12 规则 12/20
6. `docs/rule-registry.md`（A2/B2/B5）

## 1. 交付范围（创建/修改）
```
cairn/src/cairn/server/services/graph.py     # §3 全部函数：状态机/租约/超时/校验/播种
cairn/src/cairn/server/routers/projects.py   # /projects CRUD + reason 租约 + export
cairn/src/cairn/server/routers/intents.py    # intent 创建 + claim/heartbeat/release/conclude
cairn/src/cairn/server/routers/hints.py      # hint 写入
cairn/src/cairn/server/routers/export.py     # 图 YAML/timeline 导出（保留）
cairn/tests/test_graph.py
```

## 2. 必须满足的契约
- **播种**：`create_project` 时播种 `origin`+`goal` 特殊事实（description 固定）；ID 走 `next_scoped_id`（scoped_counters，`f###/i###/h###` 各自 `%03d` 计数），禁裸自增。
- **Fact 只增不改**：无更新/删除路径；重复 description 写回幂等跳过；`facts.created_at` 供 D3 时间线（24 读）。
- **Intent 校验**：`from_fact_ids` 全部存在且**不含 goal**；`to_fact_id` 存在且**非 goal**（A2：无 `to='goal'` 完成边）；`worker∈{null,creator}`；creator 不可变。违例 → 400 `VALIDATION`。
- **租约仲裁**：认领后仅持有者能 heartbeat/release/conclude；他人 → 409 `LEASE_CONFLICT`；项目非 active → 403 `PROJECT_INACTIVE`；不存在 → 404 `NOT_FOUND`。
- **conclude 三子域编排**（§5）：conclude 路由收 `{worker, facts[], coverage_result?, findings[]?}`——facts 写图；`coverage_result` 转发 21 的 `services.coverage.write_coverage_result`；`findings[]` 转发 22 的 `services.findings.create_finding`（agent 只能 open）。**同请求同事务**。三个服务均为同仓库同事务内调用（Server 单写者，无 HTTP）。
- **reason 租约**：`claim/heartbeat/release` 写 `projects.reason_*` 列；409 他人持有。
- **冻结（B5）**：`freeze_project_leases(conn, pid)` 清全部 open intent 的 worker + reason 租约——被 20 的 `expire_engagements` 调用（你提供签名，20 接线）。
- **超时清理**：`intent_timeout_cleanup`/`reason_timeout_cleanup` 在 `list_projects/get_project/export` 等读前执行（读到的即清理后状态）；时间比较统一 UTC 字符串（ISO8601），宽限语义见规格 §4.9-10。
- **导出**：`export_graph_yaml` 输出 origin/goal/全部 fact/intent/hint 的 YAML 快照，可被 13/30 的图快照逻辑消费；`format=timeline` 输出事实增量的 JSON。
- **路由挂载**：`routers/__init__.py` 已有（10 建），你把 4 个 router 挂进 `app.py` 注册点（10 留的）。

## 3. 验收标准（可执行）
1. `pytest test_graph.py`：播种（origin/goal 存在，f001 计数）；ID 独立计数（f/i/h 不串）；只增不改（重复 fact 幂等）。
2. 校验：from 含 goal → 400；to=goal → 400；worker≠creator → 400。
3. 租约：A 认领→B heartbeat/release 409；A release 后 B 可认领；conclude 后不可再 heartbeat。
4. conclude：携带 facts/coverage_result/findings 时正确转发（stub 21/22 服务验证同事务）。
5. `freeze_project_leases`：清空 open intent worker + reason 租约。
6. 超时清理：伪造超时心跳 → 读后 worker=NULL 重新可认领；已 conclude 不参与。
7. 403：stopped 项目上 claim → PROJECT_INACTIVE。
8. `GET /projects/{pid}/export?format=yaml` 输出合法 YAML，含全部节点。

## 4. 硬约束
- **不实现覆盖/漏洞子域**（21/22 负责）；conclude 编排里只**调用**它们的服务函数，不复制逻辑。若它们的服务签名与 skeleton §3 不一致，先核对 skeleton，别自行改。
- **不建表**：需要加列/索引 → 报 10 或改 `database-ddl-draft.md`（先列 diff）。
- 枚举与 DDL CHECK 逐字符一致（`active|stopped`，无 `completed`）。
- 服务签名以 `exploration-graph-spec.md` §3 + skeleton §3 为准，改签名必须同步两处。
- **不接受 v1 `services.py` 代码**（不存在）；只按规格从 0 写。

## 5. 交接物
写 `dev-agents/notes/25-graph-subdomain.md`：函数清单、租约仲裁实现、conclude 三子域编排细节、与 20（B5 接线）/21/22/24（时间线源）/30/40（客户端调用面）的接口契约、未做项。
