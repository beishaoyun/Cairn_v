# 探索图子域规格（Exploration Graph Subdomain）

> **v2 权威规格**。从 0 重建图子域时**唯一实现依据**（`server/services/graph.py` + `routers/{projects,intents,hints,export}.py`）。
> 本文件把散落在 v1 报告 §12（28 条规则）、v2 §4.3-4.5（流程）、skeleton §2.4（路由）、DDL（图表）中的图协议**合并为一份自包含规格**；v1 代码不存在，不得以「原 services.py」为参考。
> 配套权威：`database-ddl-draft.md` §3（表结构）、`backend-module-skeleton.md` §2.4（路由清单）、`rule-registry.md`（A2/B2/B5）。

---

## 0. 子域职责与边界

图子域 = 探索过程的事实图协议（黑板架构）。职责：
1. 持久化事实图（Fact 只增不改 / Intent 超边 / Hint 图外输入）
2. 维护 claim / 租约 / 超时（intent 级 + 项目级 reason 双租约）
3. 提供 REST 协议 + 图快照导出

**边界（不越界）**：
- 覆盖度（`coverage_items`/`coverage_records`/`waivers`）→ `services/coverage.py`（21 包）
- 漏洞库（`findings_*`）→ `services/findings.py`（22 包）
- 图与漏洞库**双向弱引用**：`findings.source_fact_id` 仅溯源，不阻塞双方生命周期
- 图事实不参与完成判定（A2：`complete` 端点与 project 层 `completed` 已删除）

## 1. 数据模型（图专用表，DDL §3 为唯一建表源）

| 表 | 关键列 | 语义 |
|---|---|---|
| `projects` | `id`='proj_###'、`engagement_id`(可空, FK CASCADE)、`status`∈`active\|stopped`、`bootstrap_enabled`、`reason_worker`/`reason_trigger`/`reason_started_at`/`reason_last_heartbeat_at` | **A2：status 无 `completed`**；完成仅作用于 Engagement |
| `facts` | `id`(project 内)、`description`、`created_at` | **只增不改**；project 创建时播种 `origin`+`goal` 两个特殊节点 |
| `intents` | `id`、`to_fact_id`(可空)、`description`、`creator`(不可变)、`worker`、`last_heartbeat_at`、`concluded_at` | 有向超边 from(intent_sources)→to(fact)；`to_fact_id` 可空 = 开放意图 |
| `intent_sources` | `intent_id`+`project_id`+`fact_id` | 超边的 from 侧（复合 FK CASCADE） |
| `hints` | `id`、`content`、`creator` | 图外输入，最宽松写权限 |
| `scoped_counters` | `project_id`+`kind`(fact/intent/hint) | **图 ID 唯一授予**（ID 前缀 `f###`/`i###`/`h###`，`%03d`） |

> **ID 规则**（DDL §4.1 之外的 project 作用域）：`proj_001`/`f001`/`i001`/`h001` 走 `scoped_counters`，**各自独立计数**；`eng_###` 走全局 `counters`。图 ID 生成统一经 `next_scoped_id(conn, pid, kind)`，禁止裸自增。

## 2. 核心原语与不变量

### 2.1 特殊节点（project 创建时播种）
- **`origin` 事实**：根节点，`description='origin'`，bootstrap 保留 intent 的 from 源。
- **`goal` 事实**：目标陈述（A2），`description='goal'`，**报告用，不参与完成判定**。
- **禁止**：`goal` 作 intent 的 from 源（422 VALIDATION）；`to_fact_id` 指向 goal（v2 无 `to='goal'` 完成边）。

### 2.2 三原语
- **Fact 只增不改**：任何已建事实不可更新/删除；重复 description 写回幂等跳过（同内容不重复建节点）。
- **Intent 超边**：`from = intent_sources 引用的 fact 集`，`to = to_fact_id`（可空）；`creator` 不可变；`worker` 状态机见下。
- **Hint 图外输入**：active/stopped 状态皆可写（最宽松）；写 hint 不触发除 reason 重触发外的特殊行为。

### 2.3 worker 状态机（intent）
`worker = NULL`（可认领）⇄ `worker = worker名`（已认领，租约中）⇄ 释放（回 NULL）或 conclude（`concluded_at` 置位，终态）。
- 创建 intent 时 `worker` 只能是 `null` 或 `== creator`（否则 422 VALIDATION）。
- `worker` 一旦被认领，**只有持有者**能 heartbeat/release/conclude；他人请求 → 409 LEASE_CONFLICT。
- 心跳超 `intent_timeout` → 读时清理置回 NULL（重新可认领）；已 conclude 不参与。

### 2.4 错误码语义（图子域）
| 码 | HTTP | 含义 |
|---|---|---|
| `VALIDATION` | 422 | from 含 goal、worker≠creator、空文本（业务校验） |
| `PROJECT_INACTIVE` | 403 | 项目非 active（stopped / engagement 非 active） |
| `LEASE_CONFLICT` | 409 | 租约被他人持有 / 幂等冲突 |
| `NOT_FOUND` | 404 | pid/iid/fact 不存在 |
| `ENGAGEMENT_INVALID_STATE` | 409 | engagement 状态不允许建项目/探索 |

**403/409 语义（Dispatcher 侧）**：403 = 项目已非 active，reason 的 intent 创建/写回视作**成功收场**，release 对 403/409 静默跳过不告警；409 = 竞争丢失，跳过该条继续（见 §4.4）。

## 3. 服务函数签名（`services/graph.py`）——契约冻结

> 无状态函数式、每请求短事务；签名以本文件为准，改签名先改这里再改 skeleton §3。

```python
# services/graph.py

def create_project(conn, *, engagement_id, title, bootstrap_enabled=True) -> Project
    # 播种 origin+goal 特殊事实；engagement 非 active → ENGAGEMENT_INVALID_STATE

def get_project(conn, pid) -> Project | None
def list_projects(conn, *, engagement_id=None, status=None) -> list[Project]   # 读前先跑超时清理
def delete_project(conn, pid) -> None                                           # 物理级联（facts/intents/hints/scoped_counters）
def set_project_title(conn, pid, title) -> Project
def set_project_status(conn, pid, status) -> Project                            # active|stopped（A2 无 completed）

def next_scoped_id(conn, pid, kind) -> str                                      # f###/i###/h###（scoped_counters，%03d）

def create_fact(conn, pid, *, description) -> Fact                              # 只增不改；重复 description 幂等
def create_intent(conn, pid, *, description, creator, from_fact_ids,
                  to_fact_id=None) -> Intent
    # 校验：from_fact_ids 全部存在且不含 goal；to_fact_id 存在且非 goal；creator 非空
def claim_intent(conn, pid, iid, *, worker) -> Intent                           # 403 非 active；409 他人持有；刷新 last_heartbeat_at
def heartbeat_intent(conn, pid, iid, *, worker) -> Intent                       # 403/409 同上；刷新 last_heartbeat_at
def release_intent(conn, pid, iid, *, worker) -> None                           # 仅持有者可释放；403/409 抛错（Dispatcher 侧静默处理）
def conclude_intent(conn, pid, iid, *, worker, facts=None) -> Intent            # 双阶段收尾：写 facts(只增) + concluded_at + 释放租约
def create_hint(conn, pid, *, content, creator) -> Hint                         # active/stopped 皆可

def claim_reason(conn, pid, *, worker) -> None                                  # 项目级租约：403 非 active；409 他人持有；写 reason_worker/started_at/trigger
def heartbeat_reason(conn, pid, *, worker) -> None                              # 409 他人持有；刷新 reason_last_heartbeat_at
def release_reason(conn, pid, *, worker) -> None                                # 清 reason_worker/started_at/last_heartbeat_at

def freeze_project_leases(conn, pid) -> None                                    # B5：清全部 open intent 的 worker + reason 租约（窗口到期/paused）
def intent_timeout_cleanup(conn, pid=None) -> list[str]                         # 读时清理：open intent 心跳超 intent_timeout → worker=NULL
def reason_timeout_cleanup(conn, pid=None) -> None                              # reason 心跳超 reason_timeout → 清租约
def export_graph_yaml(conn, pid) -> str                                         # 图快照 YAML（含 origin/goal/全部 fact/intent/hint）

def list_facts(conn, pid, *, after_ts=None, limit=200) -> list[Fact]            # after_ts 供 D3 时间线增量
def list_intents(conn, pid, *, open_only=True) -> list[Intent]
def list_hints(conn, pid) -> list[Hint]
```

## 4. 图规则全集（原 v1 §12 适配 v2，A2/B5 已并入）

> **编号说明**：本节 1-28 沿用 v1 §12 的图子域内部编号，**与 `rule-registry.md` 的 A/B/C/D/F/O/TV 编号体系无关**（那是 v2 需求 ID）。代码注释引用本节规则用「graph §4-<N>」；引用 v2 规则用 rule-registry 编号。

1. **`goal` 永远不能作为 Intent 的 from 源**（`validate_goal_not_in_sources`）→ 422 VALIDATION。
2. **创建 intent 时 `worker` 只能是 `null` 或 `== creator`** → 否则 422 VALIDATION。
3. **reason 输出规则（v2 收敛版）**：覆盖未收敛时 reason **必须**出非空 intents **或** `recommend_finalize=true`，二者缺一 → 校验失败任务 failed（等价 v1「open_intents 空必须返回 intent」）。`complete` 字段**已删除**，输出 `complete` 一律拒绝。
4. **reason 单次最多 `max_intents` 条 intent**；若创建 0 条（全部 403/409/写失败）整个任务判 failed。
5. **保留 bootstrap intent 三重标识**（`description='bootstrap'` + `creator='dispatcher.bootstrap'` + `from=['origin']`）是 Dispatcher 与前端**共同识别**的硬约定（调度 `_is_bootstrap_intent`/`_get_bootstrap_intent` 取「未认领优先」）。
6. **多个 bootstrap intent 只告警不纠错**，取「未认领且创建最早」——潜在重复是容忍项。
7. **403 = 项目已非 active**：reason 的 intent 创建遇 403 视作**成功收场**；`release` 对 403/409 静默跳过。
8. **409 = 他人持有**：heartbeat/reason claim 遇 409 本机**不启动任务**；reason intent 创建遇 409 竞争丢失，跳过该条继续。
9. **心跳宽限 = `max(interval, 2×interval)`**：403/409 立即判死并 kill 进程；其他失败容忍到宽限（Dispatcher 侧，40 实现）。
10. **`intent_timeout`/`reason_timeout` 必须 `> interval`**（否则启动 RuntimeError），`< 2×interval` 仅告警。
11. **进程超时判定**：`timed_out` 或返回码 124/137（且非取消）→ timeout；`cancel` 优先于 timeout。
12. **conclude 收尾三重前置**：`driver.supports_conclude()` + 有 session + 项目仍 active（`project_allows_conclude_fallback` 再查一次）；心跳已失或已取消则跳过。
13. **healthcheck `startup_and_task` 时任务启动前再查一次**；explore/bootstrap 的 conclude 阶段不再查。
14. **图快照写容器 `/tmp/cairn-prompts/<phase>-<12hex>/graph.yaml`**，prompt 中只给文件引用路径（大图不内联），每次 phase 独立目录。
15. **容器名 `cairn-{project_id}`**（`dispatcher/runtime/containers.py` 实现；早期约定 `cairn-dispatch-<project_id 的 / → ->` 的 `/ → ->` 替换因 `proj_###` 不含 `/` 而失效，统一采用更短前缀 `cairn-{project_id}`，唯一性无碍）；容器内写文件路径必须绝对路径且禁 `..`/`.`（tar 构造时校验，防穿越）。
16. **completed 容器（v2 engagement 级）**：`completed_action=stop` 只停止（保现场），`remove` 删除。
17. **stopped 项目（v2 = paused 语义，B5）**：server 立即清空 open intent worker + reason lease（`freeze_project_leases`）；Dispatcher 视为硬停止——取消本地任务、不再进入 conclude fallback、排队停容器。
18. **（v2 已删除 reopen）** 复测走 engagement `completed→active(retest=true)`，图内不保留「曾完成」记录。
19. **hint 写权限最宽松**：active/stopped 皆可写；写 hint 不触发 reason 之外的特殊行为（hint 数量增加是 reason 重触发条件之一）。
20. **reason 重触发只看「数量增加」不看内容**：fact/hint **数量**变多才触发；内容变化但数量不变不触发。
21. **Worker 并发建模**：一个 Worker = 一个独立 LLM 并发配额单元；同一 key 不拆成多个 Worker。
22. **common_env 合并优先级**：`common_env` 先合并，`worker.env` 再覆盖（`{**common_env, **worker.env}`）；local 模式叠加宿主 `os.environ` 为基底。
23. **container 模式必须提供全部 LLM env key**（claudecode 3 键 / codex 3 键 / pi 4 键），缺则加载报错；local 模式禁止要求任何 key。
24. **mock 概率必须精确 sum=1.0**（Decimal 严格相等），delay 非负且 `[0]≤[1]`；未知 `MOCK_*` 键校验失败。
25. **`.gitignore` 排除 `dispatch.yaml`**：实际配置不入库，示例文件才是唯一受控配置。
26. **健康检查只看 HTTP 状态码 2xx**，不解析响应体；local 模式改对 CLI 执行 `--help` 探测，返回 0 即「可运行」。
27. **`extract_json_object`**：返回首个合法 JSON 对象；普通文本/围栏代码块均可；找不到抛 `ValueError` → 任务 failed → 双阶段 Worker 进 conclude。
28. **协议客户端连接池**：每线程一个 Session（thread-local），连接池 64，靠 `client.close()` 统一关。

## 5. 路由契约（skeleton §2.4 展开）

> 鉴权全部 `T`（同一 Bearer）。分页 `offset/limit`。错误码按 §2.4 表。

| 方法 | 路径 | 请求 | 成功响应 |
|---|---|---|---|
| GET | `/projects?engagement_id=&status=` | query | `[{proj_###, engagement_id, title, status, bootstrap_enabled}]` |
| POST | `/projects` | `{engagement_id, title}` | Project |
| GET | `/projects/{pid}` | — | Project + facts/intents/hints 摘要 |
| DELETE | `/projects/{pid}` | — | 204（级联清理） |
| PUT | `/projects/{pid}/title` | `{title}` | Project |
| PUT | `/projects/{pid}/status` | `{status ∈ active\|stopped}` | Project |
| POST | `/projects/{pid}/reason/claim` | `{worker}` | 204 / 409 LEASE_CONFLICT |
| POST | `/projects/{pid}/reason/heartbeat` | `{worker}` | 204 / 409 |
| POST | `/projects/{pid}/reason/release` | `{worker}` | 204 |
| POST | `/projects/{pid}/intents` | `{description, creator, from_fact_ids, to_fact_id?}` | Intent（校验 422/404） |
| POST | `/projects/{pid}/intents/{iid}/heartbeat` | `{worker}` | 204 / 403 / 409。**首次心跳即认领**（`worker=NULL` → 置为请求者）；已认领者刷新；他人 409（12 客户端的 `claim_intent` 与 `heartbeat_intent` 都映射此路由） |
| POST | `/projects/{pid}/intents/{iid}/release` | `{worker}` | 204 / 403 / 409 |
| POST | `/projects/{pid}/intents/{iid}/conclude` | `{worker, facts[]?, coverage_result?, findings[]?}` | 204（server 端编排三子域写） |
| POST | `/projects/{pid}/hints` | `{content, creator}` | Hint |
| GET | `/projects/{pid}/export?format=yaml\|timeline` | — | yaml 文本 / timeline JSON |

> **conclude 三子域编排**：facts 写图（本服务）；`coverage_result` 转发 `services.coverage.write_coverage_result`（21）；`findings[]` 转发 `services.findings.create_finding`（22，agent 只能 open）。**Dispatcher 写回统一走此端点**（12 客户端 `conclude_intent`；30 的 coverage/findings writers 组装 payload）——避免 explore 结论拆多次 HTTP 写。`facts` 的 `from_fact_ids`/`to_fact_id` 同样校验 goal。

## 6. 联动（与相邻子域）

- **覆盖格互斥（B1）**：explore 认领格子走 `coverage.claim_item_for_intent`（`current_intent_id=iid`）；conclude 写回 `write_coverage_result` 时释放；意图对应的覆盖格由 21 维护，本子域只在其 conclude 编排中转发。
- **reason 触发（v2 扩展）**：reason 输入 = 21 的 `compute_gaps(exclude_in_progress=True)`；intent 创建后不自动关格子（等 explore claim）。
- **时间线（D3）**：`facts.created_at` 是六源之一（24 聚合，只读）。
- **冻结（B5）**：`expire_engagements`（20）调 `freeze_project_leases` 清本子域租约；40 启动 reconcile 同时清覆盖格认领。
- **findings 溯源**：conclude 编排内把 `findings[].source_fact_id` 关联到本次产出的关键 fact（只溯源，不阻塞）。

## 7. 验收要点

1. project 创建 → 播种 origin/goal 特殊事实；ID 走 scoped_counters（f001/i001/h001 各自计数，无裸自增）。
2. create_intent 校验：from 含 goal → 422；worker≠creator → 422；to_fact_id=goal → 422。
3. 租约仲裁：A 认领后 B 心跳/释放 → 409 LEASE_CONFLICT；B 静默放行后 A 可释放。
4. conclude：写 facts（只增，重复幂等）+ `concluded_at` + 释放租约；携带 coverage_result/findings 时正确转发（可 stub 21/22）。
5. `freeze_project_leases`：paused 后全部 open intent worker 清空、reason 租约清空。
6. 超时清理：伪造超时心跳 → 读前清理后 worker=NULL、重新可认领；已 conclude 不参与。
7. `export_graph_yaml` 含 origin/goal/全部节点，格式可被 13/30 的图快照逻辑消费。
8. 403 语义：stopped 项目上 claim/conclude → PROJECT_INACTIVE。
