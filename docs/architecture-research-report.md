# 项目架构深度调研报告

> **⚠ v1 对照文档**：本文件是原始 CTF 平台的调研报告，已被 `architecture-research-report-pentest-v2.md` + 各实现 spec（coverage/capture/verify/backend-skeleton/dispatch-config）取代。仅作历史与实现参照，**勿作为新平台实现依据**。
> 文档用途：参考本项目核心思想，从零搭建新一代同类型平台底座
> 文档版本：自动生成（分析基准 commit `8f702c5`，v0.2.1）
> 分析基准：全套原始项目源码（`cairn/src` 全部 Python 源码 + `server/static/index.html` 前端 + 配置 + 文档 + 测试 + Docker/CI）

---

## 1. 项目概述

### 1.1 项目定位与业务场景

**Cairn（衍迹）** 是一个**通用问题求解引擎**，基于「事实-意图有向无环图（Fact-Intent DAG）」协作协议，将"从已知起点探索未知状态空间、直到达成目标"的过程建模为一张不断生长的知识图。

- 业务场景首个落地领域：**AI 渗透测试 / CTF 智能攻防**。给定 `Origin`（起点，如目标 IP）与 `Goal`（终点，如拿 flag / 获取 shell），多个 AI Worker 协作探索中间路径，把发现写成事实，最终共同判定 Goal 达成。
- 架构理论渊源：**黑板架构（Blackboard Architecture）** 的现代化重构（CMU Hearsay-II 范式）+ **蚁群信息素机制（Stigmergy）** + **OODA 循环** + **任务式指挥（Mission Command）**。系统不做任何推理决策，只负责图的一致性维护。

### 1.2 平台核心价值与解决痛点

| 价值点 | 说明 |
|---|---|
| **无角色、无流程编排** | 不定义任何 Agent 角色，不预设工作流；任务由调度器根据图当前状态实时生成（bootstrap/reason/explore 三类） |
| **多 Agent 协作去中心化** | Agent 之间不直接通信，只通过共享黑板间接协调，无信息孤岛 |
| **完整因果链审计** | Fact 只增不改，Intent 保留"从哪些事实出发、探向哪里"的超边关系，图既是知识库又是推理路径审计日志 |
| **事实时序列演化** | 状态变化通过追加新 Fact 表达（如"shell 已断开"是一个新事实），Agent 自行判断时效，无需失效标记 |
| **弱一致解耦** | Server 只管图一致性，调度/执行完全下沉到独立 Dispatcher，二者通过 REST 协议解耦 |
| **跨厂商 Worker 接入** | Claude Code / Codex / Pi 三种 agent CLI + 测试用 Mock，通过统一 Driver 抽象可插拔 |
| **实战验证** | 腾讯云黑客松智能渗透赛：610 队 1345 人参赛，54/54 题全解（唯一 AK 队），总排名第 3 |

### 1.3 能力边界：能干什么、天然不支持什么

**能干什么：**
- 任意"有明确起点 + 明确终点 + 未知路径"的搜索问题（渗透、漏洞研究、数学证明、CTF 等）
- 多 Agent 并行探索同一问题，图内并发认领/心跳/结论的协作互斥
- 人工随时注入 Hint（外部策略建议，不污染事实图）
- 项目状态生命周期管理：active / stopped / completed / reopen 纠错

**天然不支持 / 当前明确不支持：**
- **多 Dispatcher 实例高可用**：设计文档明确"只支持单 Dispatcher 实例连接同一服务端共同调度"，并发控制、健康状态、容器清理、bootstrap 去重均为单进程内内存态，跨进程不协调
- **Worker 历史可观测**：只记录当前 claim 持有者（`intent.worker`），停止后无法追溯"最后是谁在推进"
- **推理/决策能力**：协议层不判断 goal 是否达成，判断全部由 Worker（LLM）完成
- **Server 无鉴权**：任何人可读写，只适合受信内网/本地环境
- **分布式/大规模扩展**：SQLite 单文件存储、线程池调度，无 MQ/任务队列/分布式锁

---

## 2. 完整技术栈清单

### 2.1 后端技术框架 & 语言版本

| 组件 | 选型 | 版本 |
|---|---|---|
| 语言 | Python | `>=3.12`（容器内 3.13） |
| API 框架 | FastAPI | `>=0.115` |
| ASGI 服务器 | uvicorn[standard] | `>=0.34` |
| 数据校验 | Pydantic v2 | 随 FastAPI |
| CLI 框架 | click | `>=8.1` |
| 配置解析 | pyyaml | `>=6.0` |
| 包管理/构建 | uv（uv_build 后端） | uv.lock 锁定 |

### 2.2 前端技术栈

- **单文件 SPA**：`server/static/index.html`（约 3400 行，无构建步骤，无打包器）
- **响应式框架**：Alpine.js（`defer` 加载，`x-data="cairnApp()"`）
- **样式**：Tailwind CSS（CDN 运行时版 + 内联 `tailwind.config` 主题扩展）
- **图可视化**：Cytoscape.js + 3 种布局引擎（dagre / klay / ELK，均可切换方向 TB/LR）
- **本地状态**：`localStorage`（actor 名称、默认布局、侧栏宽度）
- **路由**：hash 路由 `#/projects/{id}`

### 2.3 数据库 & 存储方案

- **SQLite**（Python 标准库 `sqlite3`，非 ORM，手写 SQL）
- 开启 `PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`
- 默认库路径 `~/.local/share/cairn/cairn.db`，`docker-compose` 挂载持久化到 `./datas/cairn/`
- 无 Redis / 无 ES / 无对象存储

### 2.4 中间件清单（Redis/MQ/ES等）

**无任何外部中间件**。所有异步/定时/队列均为进程内实现：
- `ThreadPoolExecutor` 任务池（调度任务）+ 独立 `cleanup_executor`（容器清理）
- 心跳保活线程（`HeartbeatLease`）
- 无 Celery / RQ / Kafka / RabbitMQ

### 2.5 构建、部署、容器方案

| 项 | 内容 |
|---|---|
| 应用镜像 | `ghcr.io/astral-sh/uv:python3.13-trixie` + `uv sync --frozen`，镜像源走阿里云 PyPI 镜像，`TZ=Asia/Shanghai` |
| 编排 | `docker-compose.yaml`：`cairn-server`（8000 端口 + 健康检查 `/projects`）+ `cairn-dispatcher`（挂载 `docker.sock` 与 `dispatch.yaml`，`depends_on` server 健康后启动） |
| Worker 容器镜像 | `container/Dockerfile`：Kali Linux headless，预装全套渗透工具（nuclei/katana/dalfox/impacket/ysoserial/ripgrep/fd/awscli/tccli 等）、Playwright、Claude Code / Codex / Pi CLI，按 `home/kali/workspace` 为工作目录 |
| CI | GitHub Actions：`container/**` 变更时 buildx 构建并推送 `ghcr.io/oritera/cairn-worker-container:latest`（linux/amd64） |
| 测试 | `uv run --group dev pytest`（pytest + httpx + FastAPI TestClient） |

### 2.6 外部第三方依赖服务

- **LLM API 网关**：Anthropic 兼容 `/v1/messages`（DeepSeek/Claude）、OpenAI Responses API、OpenAI 兼容 `/chat/completions`（DashScope 等）
- **Agent CLI**：`claude`（Claude Code）、`codex`（OpenAI Codex）、`pi`（Pi Coding Agent）
- **CTF 平台 API**（业务外部依赖）：`TSEC_SERVER_HOST` + `TSEC_AGENT_TOKEN` 提交 flag（通过容器内 skill `tsec-actions` 定义）

---

## 3. 系统架构设计

### 3.1 分层架构说明（接口层/业务层/数据层/基础设施）

按标准四层映射，本工程由 **Server（协议真相源）** 与 **Dispatcher（调度执行器）** 两个独立进程组成，各自内部再分层：

```
┌─────────────────────────────────────────────────────────────────┐
│                        Cairn Server（进程一）                      │
│                                                                 │
│  【接入/接口层】  routers/*.py   FastAPI 路由 + Pydantic DTO      │
│  【业务领域层】   services.py    状态机/租约/超时/校验规则          │
│  【数据持久层】   db.py          SQLite Schema + WAL + 计数器      │
└─────────────────────────────────────────────────────────────────┘
                          ▲            │ REST (HTTP/JSON)
                轮询+协议写回│            │ 健康/心跳
┌─────────────────────────────────────────────────────────────────┐
│                        Dispatcher（进程二）                       │
│                                                                 │
│  【接入/接口层】  protocol/client.py  CairnClient（线程级连接池）    │
│  【业务领域层】   scheduler/loop.py   调度策略、去重、准入、清理队列  │
│                 tasks/*.py          bootstrap/reason/explore     │
│                 contracts.py        LLM 输出契约校验              │
│                 workers/            驱动抽象 + 注册表 + 健康检查   │
│  【数据持久层】   （无数据库，状态全部在内存 dict/线程池中）         │
│  【基础设施层】   runtime/*.py       容器/子进程执行、心跳、取消     │
│                 config.py/logging.py/prompting.py/output_parser  │
└─────────────────────────────────────────────────────────────────┘
                          │  Docker daemon (docker.sock) / 宿主进程
                          ▼
              ┌─────────────────────┬─────────────────────┐
              │ 项目容器A (Kali)     │ 项目容器B (Kali)     │  ← Local模式=宿主子进程
              │ claude/codex/pi CLI │ ...                  │
              └─────────────────────┴─────────────────────┘
```

### 3.2 模块依赖关系（文字架构图）

```
cli.py ──→ server.db / dispatcher.logging / scheduler.loop
         ├──→ server.app ─→ routers(settings/projects/hints/intents/export) ─→ services ─→ db
         │            └──→ server.models（Pydantic DTO）
         │
DispatcherLoop (scheduler/loop.py)
   ├──→ config.py（DispatchConfig 全量校验/默认值）
   ├──→ protocol/client.py（CairnClient）
   ├──→ runtime/backend.py（ExecutionBackend 协议）
   │      ├──→ runtime/containers.py（ContainerManager, Docker）
   │      └──→ runtime/local_backend.py（LocalBackend, 宿主）
   ├──→ runtime/process.py（ExecProcess 协议 / ManagedProcess）
   │      └──→ runtime/local_process.py（LocalProcess）
   ├──→ runtime/heartbeat.py（HeartbeatLease）
   ├──→ runtime/cancellation.py（TaskCancellation）
   ├──→ runtime/startup_healthcheck.py
   ├──→ scheduler/worker_select.py（choose_worker）
   ├──→ workers/registry.py ─→ adapters/{claudecode,codex,pi,mock} ─→ workers/base.py / health.py
   ├──→ tasks/{bootstrap,reason,explore,common}.py ─→ contracts.py / prompting.py
   └──→ prompts/{default,mock}/*.md（打包资源，启动时校验占位符）
```

**关键依赖方向约定**：`dispatcher → server.models`（复用 DTO 做协议对象），但 **server 绝不反向依赖 dispatcher**；`runtime` 内部通过 `ExecutionBackend` / `ExecProcess` 两个 `Protocol` 完成容器/本地执行后端互换；任务执行器只依赖 `container_manager` + `driver` 两个抽象面，不感知后端差异。

### 3.3 核心请求完整流转链路

**① 人工/前端操作（写图）：**

```
浏览器 (Alpine fetch) ──POST /projects/{id}/intents──▶ routers.intents
   ─▶ services.check_project_active（403非active）
   ─▶ validate_facts_exist / validate_goal_not_in_sources / validate_intent_creator_worker
   ─▶ db.get_conn() 单事务：写 intents + intent_sources + scoped_counters 自增
   ─▶ 返回 Intent DTO
```

**② Dispatcher 单轮调度主链路（每 `runtime.interval` 秒）：**

```
1. run_startup_healthchecks()                   启动时：容器模式进程内 HTTP 探活 / 本地模式 CLI --help 探测
2. _validate_server_settings()                  校验 server intent/reason_timeout > interval
3. _reap_futures()                              回收已完成任务，更新 unhealthy/rejected 冷却与 reason checkpoint
4. _reap_cleanup_futures()                      回收容器清理任务
5. list_projects() → ProjectSummary[]          轮询快照（附带服务端超时清理）
6. _initialize_reason_checkpoints()             为"有 open intents 的 active 项目"建立 reason 基线
7. _refresh_runtime_projects()                  runtime_project_ids ∩ active
8. _cancel_inactive_tasks()                     非 active 项目 → 取消本地运行任务（cancel("stopped"/"deleted")）
9. _queue_container_cleanups()                  排队 completed/stopped 容器异步清理
10. _dispatch_available()                       按"运行中项目优先→新项目"两段轮询派发
    └─ _try_dispatch_project → 初始态走 bootstrap，否则 reason(新态势) 优先、explore(未认领)次之
```

**③ 单个任务（以 explore 为例）完整流转：**

```
Dispatcher 选定 intent → client.heartbeat(claim) 认领成功
  → container_manager.ensure_running(project_id)  建/复用容器
  → (startup_and_task 时) driver.check_health() 进程内 HTTP 探活
  → render_prompt(explore.md, {graph_yaml快照, intent_id, intent_description})
  → graph_yaml 以 tar 写入容器 /tmp/cairn-prompts/<phase>-<uuid>/graph.yaml（规避 argv 长度限制）
  → driver.build_execute() → build_exec_process → container exec 启动 CLI
  → 后台线程收集 stdout/stderr + heartbeat 线程按 interval 续约
  → 超时/解析失败 → driver.build_conclude() 同 session 二阶段收尾
  → 解析 stdout 全量 → 提取 JSON → contracts.validate_explore_payload()
  → client.conclude() 写 Fact + 落定 intent
  → 失败路径 → client.release() 释放 claim
  → 收尾 → lease.stop() / TaskCancellation.attach_process(None)
```

### 3.4 跨模块调用方式、同步/异步场景划分

| 场景 | 方式 | 说明 |
|---|---|---|
| Dispatcher ↔ Server | **HTTP REST 同步**（requests 线程局部 Session，连接池 64） | 轮询、认领、心跳、写回 |
| Dispatcher → Agent CLI | **同步子进程/容器 exec + 流式收集** | ManagedProcess（docker exec demux 流）/ LocalProcess（stdout/stderr 线程 drain） |
| 调度任务执行 | **异步线程池**（`executor.submit`，`Future` 字典 + 每轮 reap） | `runtime.max_workers` 并发上限 |
| 心跳保活 | **异步守护线程**（`HeartbeatLease._thread`） | 每 `interval` 秒一次；失败时 kill 绑定的进程 |
| 容器清理 | **独立异步线程池**（`cleanup_executor`，最多 8） | completed/stopped 容器 stop/remove 不阻塞主循环 |
| 服务端数据库写 | **同步单事务**（`get_conn` 上下文管理器） | 每请求一个连接，`conn.commit()`/`rollback()` 包裹 |

---

## 4. 核心业务流程清单

### 4.1 项目全生命周期
`创建(active) → 调度探索 → completed / 人工 stopped → reopen 纠错`

### 4.2 Bootstrap 流程（初始态直接解题）
1. **触发条件**：项目 active；`bootstrap_enabled=true` 且存在支持 bootstrap 的 Worker，**或**已存在保留 bootstrap intent；facts 恰为 `{origin, goal}`；intents 为空或仅保留 bootstrap intent。
2. **保留 intent 约定**（消费者侧约定，非服务端约束）：`description="bootstrap"`、`creator="dispatcher.bootstrap"`、`from=["origin"]`、`to=null`。
3. Dispatcher 创建保留 intent → heartbeat 认领 → 派发 bootstrap（prompt 仅含 origin/goal/hints，不含图 YAML）。
4. **主阶段契约**：解决即返回 `{accepted:true, data:{fact:{description}, complete:{description}}}`（fact+complete 必须同时给出）；超时/解析失败 → 同 session 进入 `bootstrap_conclude`（只允许返回 fact）。
5. **写回**：conclude 写入 fact → 若 fact_id 已知再 `complete(from=[fact_id])` 直接完成项目；complete 写失败则 fact 保留，交后续 reason。
6. **失败**：两阶段均失败 → release 保留 intent，下轮按新项目重试。

### 4.3 Reason 流程（读图判断：是否完成 / 是否提新意图）
1. **触发条件**：项目 active；无未认领 intent；无其他 reason 在运行；首次触发或满足"新态势"。
2. **新态势定义**：Fact 数量增加 ∨ Hint 数量增加 ∨ 从"存在 open intents"变为"无 open intents"。（intent 总数增加**不**算新态势；explore 失败/掉心跳**不**触发）
3. **去重机制**：`reason_checkpoints[project_id] = ReasonCheckpoint(fact_count, hint_count, open_intent_count)`，成功完成后更新；Dispatcher 启动时为"有 open intents 的 active 项目"补建基线。
4. claim 项目级 `project.reason` 租约 → 执行中持续 reason/heartbeat → 解析输出：
   - `data.complete` → `POST /complete`（from 校验存在且不含 goal）
   - `data.intents`（兼容单数 `intent`）→ 逐条 `POST /intents`，`creator=worker名`、`worker=null`，**最多 max_intents 条**
   - `data={}` → 不写图
5. 任何失败（超时/非 0 退出/JSON 非法/字段缺失/写回失败）→ 本轮作废、不重试、仅日志；`accepted:false` → rejected 冷却 5s。
6. 收尾 always `release_reason`（除非项目已非 active）。

### 4.4 Explore 流程（执行单个已认领 intent）
1. **触发**：项目 active；存在 `to=null ∧ worker=null` 且未在本机运行的未认领 intent（选 created_at 最新的）。
2. 先 heartbeat 认领 → 再启动 Worker。
3. 正常：execute 返回 `{accepted:true, data:{description}}` → conclude 写新 fact + 落定 intent。
4. **双阶段收尾**（driver 支持 conclude 且有 session 时）：execute 超时或解析失败 → 同一 session 进入 explore_conclude（prompt 强制"立即停止、只总结已确认结论"）→ 成功则 conclude。
5. 直接失败不进入收尾：`accepted:false`、退出码非 0、无输出 → release。
6. conclude 写失败 → 作废 + release，不重试。

### 4.5 Complete（完成声明）
`POST /projects/{id}/complete`：校验 from 存在且不含 goal → 创建 `to='goal'` 的已结论 intent（creator=worker=请求 worker）→ 项目置 `completed` → 清空 reason。

### 4.6 Reopen（纠错重开）
仅 `completed` 可用：找到唯一 `to='goal'` 完成边 → 读取其 from 源 → **删除该完成边** → 新建普通 fact（纠错说明）→ 新建 `description="external_feedback"` 已结论 intent（from 继承原完成边、to=新 fact、creator=worker=请求 creator）→ 项目回 active → 清空 reason。（不保留"曾完成过一次"的图内历史）

### 4.7 停止/恢复
`PUT status=stopped`：立即清空所有 open intent 的 worker + 清空 reason → Dispatcher 下轮取消本地任务、不再派发、排队停止容器。`status=active` 恢复后按普通 active 项目重调度。

### 4.8 Hint 注入
任何状态（active/stopped/completed）都可写 Hint；属图外输入，不参与因果。

---

## 5. 数据模型设计

### 5.1 核心实体 ER 关系说明

```
Project ──< Fact (PK: id, project_id)          Fact: origin/goal 特殊节点 + f### 普通事实
Project ──< Intent (PK: id, project_id)        Intent: 有向边（超边），to→产出Fact / 特殊值 'goal'
Project ──< Hint                               Hint: 图外旁注
Intent ──< IntentSources (intent_id, project_id, fact_id)  多对多"from"关系，含 rowid 保序
Project ──< ScopedCounters (project_id, kind)  kind∈{fact,intent,hint} 项目内自增
Counter  (name='project')                      全局项目号
Settings (rowid=1 单例)                        intent_timeout / reason_timeout
```

- 图方向：`Fact(from) →[Intent]→ Fact(to)`；Intent `to='goal'` 表示完成边。
- 复合外键：`intent_sources → intents(id, project_id) ON DELETE CASCADE`；`facts/intents/hints → projects(id) ON DELETE CASCADE`。

### 5.2 关键数据表结构说明

| 表 | 字段 | 索引/约束 | 说明 |
|---|---|---|---|
| `projects` | `id`(PK), `title`, `status`(active/stopped/completed, default active), `bootstrap_enabled`(INT default 1), `created_at`, `reason_worker`, `reason_trigger`, `reason_started_at`, `reason_last_heartbeat_at` | id 主键 | 项目级 reason 租约内联 4 列 |
| `facts` | `id`, `project_id`(FK), `description` | **复合主键(id, project_id)** | 只增不改 |
| `intents` | `id`, `project_id`(FK), `to_fact_id`, `description`, `creator`(不可变), `worker`(当前 claim), `last_heartbeat_at`, `created_at`, `concluded_at` | 复合主键(id, project_id) | worker 语义随状态变化（见 6.2） |
| `intent_sources` | `intent_id`, `project_id`, `fact_id` | 三列复合主键 + 复合 FK | 超边 from；`ORDER BY rowid` 保插入顺序 |
| `hints` | `id`, `project_id`(FK), `content`, `creator`, `created_at` | 复合主键 | |
| `counters` | `name`(PK), `value` | | `project` 全局自增 |
| `scoped_counters` | `project_id`, `kind`, `value` | 复合主键 | fact/intent/hint 项目内自增 |
| `settings` | `intent_timeout`, `reason_timeout` | rowid=1 单例 | 通过 `INSERT OR IGNORE` 保证存在 |

**ID 生成规则**（均 `%03d` 三位补零）：`proj_001`、`f001`、`i001`、`h001`，各自独立计数。

**迁移逻辑**：`_ensure_project_columns()` 为旧库补 `bootstrap_enabled` 列，并将历史 `bootstrap_mode`（'disabled'/'enabled'）映射为 0/1（兼容两层历史 schema）。

### 5.3 数据读写规则、分表/缓存策略、事务边界

- **无分表、无缓存**；SQLite WAL 模式保证读写并发。
- **事务边界**：每个 FastAPI 路由 handler 内 `with get_conn()` 一个事务（提交/回滚/关闭）；conclude/complete/reopen 均为原子操作。
- **软删除**：无软删除标记；项目删除走物理级联 `ON DELETE CASCADE`。事实永不删除。
- **超时清理（写路径上的读时清理）**：
  - `expire_workers`：open intent（`to_fact_id IS NULL`）`worker` 有值且心跳超 `intent_timeout` → 清空 worker 重新可认领；**已结论 intent 不参与**。
  - `expire_reason_leases`：reason 心跳超 `reason_timeout` → 清空租约。
  - 清理在 `list_projects` / `get_project` / export / 各写接口调用前执行，因此"读到的状态即清理后状态"。
- **时间比较**：server 侧用 `julianday(?) - julianday(last_heartbeat_at))*86400 > timeout`（UTC 字符串 `%Y-%m-%dT%H:%M:%SZ`）；Dispatcher 侧心跳失败判活用 `time.monotonic()`。
- **并发安全**：Dispatcher 进程内通过 per-container 锁（`_ensure_running_locks`）防并发建容器；Server 靠 SQLite 事务 + 409 状态码做租约冲突仲裁（非乐观锁行版本，而是"读当前值→比对→更新"）。

---

## 6. 认证 & 权限体系设计

### 6.1 登录、会话、Token 完整流程

**本系统没有认证。** 详细说明：

- Server 无任何登录、无会话、无 Token、无中间件鉴权。所有接口匿名可访问。
- 前端"actor"（`localPrefs.actor_name`）仅是**浏览器 localStorage 中的自称标识**，随请求体 `creator`/`worker` 提交，服务端不校验、不关联任何身份。
- Dispatcher 与 Server 之间也无认证（`CairnClient` 无 header）。
- 唯一的"凭证"存在于**外部 CTF 平台**：容器环境变量 `TSEC_AGENT_TOKEN` 用于调用提交 flag 的第三方 API（`tsec-actions` skill），与 Cairn Server 无关。

### 6.2 权限模型（RBAC/数据权限/功能权限）

**无 RBAC、无用户体系。** 权限模型退化为一套**状态机写保护 + 租约互斥**规则：

| 主体 | 权限规则 |
|---|---|
| 项目状态 | 探索写操作（intent/reason/complete）仅 `active`；Hint 写任何状态均可；title 任何状态可改；completed 仅可 reopen/删除 |
| Intent claim | `worker=null` 可被任何人 heartbeat 认领；被他人占用且未超时 → 409；超时自动释放 |
| Intent release | 仅当前 worker 本人可释放；已未认领则幂等 |
| Reason 租约 | 全局唯一；他人占用未超时 → 409；同 worker 幂等 |
| worker 字段约束 | 创建 intent 时 `worker` 必须为 null 或等于 `creator` |

### 6.3 鉴权拦截逻辑、黑白名单规则

- **无中间件/无拦截器**。鉴权逻辑全部内联在 `services.py` 的 helper（`check_project_active`/`check_project_hint_writable`/`check_project_completed`/`get_claimable_open_intent_or_404` 等）。
- 无 IP 黑白名单、无速率限制、无 CORS 配置。
- 冲突语义：**403** = 项目状态禁止（inactive/completed）；**409** = 租约被他人持有/幂等冲突；**404** = 资源不存在；**400** = 业务校验失败（from 含 goal、worker≠creator）；**422** = Pydantic 校验失败（空文本、缺字段）。

---

## 7. API全局规范

### 7.1 路由命名规范

```
/settings                                     GET/PUT
/projects                                     GET/POST
/projects/{project_id}                        GET/DELETE
/projects/{project_id}/title                  PUT
/projects/{project_id}/status                 PUT (active<->stopped)
/projects/{project_id}/reason/claim           POST
/projects/{project_id}/reason/heartbeat       POST
/projects/{project_id}/reason/release         POST
/projects/{project_id}/intents                POST
/projects/{project_id}/intents/{intent_id}/heartbeat|release|conclude   POST
/projects/{project_id}/complete               POST
/projects/{project_id}/reopen                 POST
/projects/{project_id}/hints                  POST
/projects/{project_id}/export?format=yaml|timeline   GET
```

规范：RESTful 资源式 + 动词子资源；项目级与 intent 级分别挂载；写操作全 POST；快照查询用 GET 带 format 参数。

### 7.2 统一请求、统一响应格式

- **请求**：JSON body，Pydantic `BaseModel` 校验，文本字段统一 `strip()` 后非空（否则 422）；`from` 数组 min_length=1 且逐元素 strip。
- **响应**：**无统一包装结构**（无 code/message/data），直接返回资源对象（`response_model`）；状态码语义化（201 创建、204 删除、403/409/422 错误）。
- **错误体**：FastAPI 默认 `{"detail": "..."}`，detail 为纯文本原因（部分 422 为数组）。
- **前端约定**：前端 `api()` helper 统一解析 detail（string 或数组 msg 拼接）抛错，toast 展示。

### 7.3 全局错误码定义

无集中错误码枚举；用 **HTTP 状态码 + detail 文本** 表达。Dispatcher 侧对 403/409 有特殊语义处理（403=项目失效视为"成功收场"，409=竞争失败，均不重试）。

### 7.4 请求校验策略

| 层 | 策略 |
|---|---|
| 结构校验 | Pydantic 模型 + `field_validator` 去空白非空 |
| 存在性校验 | `validate_facts_exist`（from 中每个 fact 必须存在）；`get_project_or_404` / `get_intent_or_404` |
| 语义校验 | `validate_goal_not_in_sources`（from 禁含 goal）；`validate_intent_creator_worker`（worker∈{null, creator}） |
| 状态校验 | 所有写操作前 `check_project_active`（403）；completed 写操作禁；title 例外 |
| 冲突校验 | 认领/释放/结论前先 `expire_*` 超时清理，再比对当前持有者 |
| Dispatcher 侧 | 配置加载时全量静态校验（见 config.py 字段约束）；LLM 输出做 JSON 提取 + 契约级结构校验（contracts.py） |

---

## 8. 核心模块详细说明

### 8.1 Cairn Server（协议真相源）
- **职责**：持久化图数据、维护 claim/租约/超时、提供 REST 协议、导出快照。
- **内部实现**：FastAPI 5 个 router + `services.py`（约 260 行）承载全部状态机规则 + `db.py` schema。无 service 类，函数式 helper + 每请求短连接事务。
- **依赖**：SQLite 标准库、Pydantic、yaml。
- **输入/输出**：REST 请求 → 图状态 / 资源对象。

### 8.2 Dispatcher 调度器（scheduler/loop.py，935 行）
- **职责**：主循环、准入控制、任务派发、Worker 选择、健康检查编排、容器生命周期、reason 去重、日志降噪。
- **关键状态**：`futures`（运行中任务）、`reason_checkpoints`、`runtime_project_ids`、`worker_unhealthy_until`、`worker_rejected_until`、`_cleanup_pending`、`_inactive_cleanup_done`、`project_cursor`（轮转游标）。
- **派发优先级**：已运行项目优先（bootstrap→reason→explore），运行中无任务再开新项目（受 `max_running_projects` 约束）。
- **Worker 选择**：过滤 `task_types`/`max_running`/unhealthy 冷却/rejected 冷却 → `choose_worker` 按 `(priority, 运行数, random)` 升序。
- **输出**：返回 "success"/"cancelled"/"failed"/"unhealthy"/"rejected" 结果字符串，驱动冷却与 checkpoint 更新。

### 8.3 任务执行器（tasks/{bootstrap,reason,explore,common}.py）
- **职责**：渲染 prompt → 起进程 → 解析输出 → 按契约写回。共用 `run_worker_process`（attach heartbeat + cancellation）、`write_graph_snapshot_reference`（图快照落盘引用）、`write_conclude_result`、`best_effort_release`。
- **双阶段模式**：bootstrap/explore 均支持"execute + 同 session conclude 收尾"；reason 仅单阶段。
- **超时语义**：进程超时通过 `timeout -k 5s <N>s`（容器）或 Python 强制 `SIGTERM→grace→SIGKILL`（本地）；`communicate` 额外 +15s grace；返回码 124/137 视为超时。

### 8.4 Worker 驱动抽象（workers/）
- **WorkerDriver** 抽象：`check_health` / `build_execute` / `build_conclude` / `prepare_session` / `extract_session` / `extract_response_text` / `supports_conclude` / `local_binary`。
- **claudecode**（SeedSessionDriver，预生成 UUID session）：执行 `claude --session-id <s> --dangerously-skip-permissions -p -- <prompt>`；二阶段 `claude -r <s> ...`；健康检查打 `/v1/messages`。
- **codex**（RegexSessionDriver，从 stderr 正则 `session id:\s*([0-9a-fA-F-]+)` 提取）：执行 `codex exec --dangerously-bypass-approvals-and-sandbox --model ... -c model_provider="cairn" ...`（通过 CLI `-c` 注入自定义 provider 指向自有 base_url）；二阶段 `codex exec resume <s>`。
- **pi**（事件流解析）：通过 shell 包装脚本注入 `models.json`（provider cairn 配置）+ `--session-dir`；从 stdout JSONL 事件提取 session（`type:session`）与最后 assistant 文本（`turn_end`/`agent_end`）；支持三种 wire API（openai-completions/responses/anthropic-messages）；可选 `PI_MODEL_CONTEXT_WINDOW`。
- **mock**（SeedSessionDriver）：纯脚本模拟 6 个 phase（healthcheck/bootstrap/bootstrap_conclude/reason/explore_execute/explore_conclude），按 `MOCK_<PHASE>` JSON 环境变量配延迟/概率/rules（`fact_ids_gte/lte`、`open_intents_empty` 强制结果）。
- **local 变体**：codex/pi 在 `execution=local` 时**省略所有 provider 注入**，直接调用宿主机原生 CLI（复用本机已登录配置）。

### 8.5 执行后端（runtime/）
- **ExecutionBackend 协议**：`ensure_running`/`build_exec_process`/`write_text_file`/`cleanup_*`/`close`。
- **ContainerManager**：Docker SDK；容器命名 `cairn-dispatch-<project_id 的 / 替换为 ->`；`containers.run(image, ["sleep","infinity"], detach)` 常驻；执行走 `exec_create/exec_start(demux=True)`；写文件走 `put_archive`（tar 流，含路径穿越防护）；completed 后按 `completed_action` stop 或 remove（timeout=1s）；孤儿清理方法存在但**未接入调度循环（死代码，见 10.2）**。
- **LocalBackend**：每项目一个 `<workspace_root>/<project_id>/` 目录；进程继承宿主环境（`{**os.environ, **worker.env}`）；容器生命周期方法惰性化。
- **进程抽象**：`ExecProcess` 协议 → `ManagedProcess`（容器 exec，kill 通过容器内 `kill -KILL <pid>`，多命令兜底）/ `LocalProcess`（`start_new_session=True` 独立进程组，SIGTERM→grace→SIGKILL）。

### 8.6 心跳与取消（runtime/heartbeat.py, cancellation.py）
- **HeartbeatLease**：守护线程每 `interval` 秒 POST heartbeat；403/409 → 立即 fail；其他失败 → 2×interval 宽限期容忍；超宽限 → fail 并 kill 绑定进程。`for_intent` / `for_reason` 两个工厂。
- **TaskCancellation**：线程安全，首次 cancel 记录 reason，后续幂等；attach 进程时若已取消立即 cancel。

### 8.7 契约校验（contracts.py）与输出解析（output_parser.py）
- **extract_json_object**：从整段 stdout 提取首个合法 JSON 对象（支持 ```json 围栏剥离 + `{` 位置 raw_decode 逐段探测，去重）。
- **契约校验**：`validate_reason_payload` / `validate_bootstrap_execute_payload` / `validate_bootstrap_conclude_payload` / `validate_explore_payload`。统一支持 `{accepted:true,data:...}` 包装与非包装两种形态；**向后兼容单数 `intent` 键**；reason 的 `complete` 与 `intents` 互斥；`open_intents` 为空时强制非空 intents；`max_intents` 截断。

### 8.8 前端图看板（static/index.html）
- **职责**：项目列表 + 事实-意图图可视化（Cytoscape）+ 侧栏（Detail/Hints/Log）+ 时间线回放（Replay）+ 全量 YAML 快照预览/复制 + 项目管理。
- **关键能力**：5s 轮询增量渲染（节点位置锚定、新增节点淡入）；事实血缘高亮（上游推导链）；时间线事件拓扑排序（同时间戳按依赖消解）；回放模式从时间线事件重建图（暂停/变速/重开）；hash 路由。

---

## 9. 异步、定时任务、事件机制

### 9.1 所有定时任务清单与执行逻辑

| 定时任务 | 周期 | 实现 | 执行逻辑 |
|---|---|---|---|
| Dispatcher 主调度循环 | `runtime.interval` 秒 | `while True: ... time.sleep(interval)` | 见 3.3 主链路 |
| Intent/Reason 心跳 | 同 `interval` 秒 | `HeartbeatLease` 守护线程 | 每任务独立线程 |
| Worker 冷却窗口 | unhealthy/rejected 各 5s | `worker_unhealthy_until` / `worker_rejected_until` 时间戳 | 冷却期内不参与派发 |
| Server 侧超时清理 | **惰性（读时触发）** | 每次 list/get/写接口前调用 `expire_*` | 无独立后台任务 |

> 注意：`runtime.interval` 被刻意复用作"主循环节拍 + claim 任务心跳周期"双用途，是明确设计决策而非耦合。

### 9.2 异步消费队列、消息结构、重试、失败处理策略

**无消息队列。** 等价机制：
- **任务队列**：`ThreadPoolExecutor`（max_workers 上限），`Future` 字典；每轮 `_reap_futures` 消费完成事件。
- **容器清理队列**：独立 `cleanup_executor`，`_cleanup_pending` 集合去重防重复入队，失败从 pending 移除允许下轮重试。
- **重试策略**：**几乎无自动重试**——任务失败仅记日志 + 冷却（unhealthy/rejected 5s）+ 交由下一轮主循环重新评估图状态自然重试；写回失败不重试。容器清理失败可在后续轮次重试（pending 移除后重新入队）。
- **失败等级语义**（任务返回值）：`success` / `cancelled`（项目停止/删除触发）/ `failed`（超时、命令失败、解析失败、写回失败）/ `unhealthy`（健康检查失败 → 5s 冷却）/ `rejected`（LLM 拒绝 → 5s 冷却）。`cancelled` 状态**不再进入 conclude 收尾**。

---

## 10. 现有项目优缺点评估

### 10.1 优秀设计、值得继承的架构思想

1. **极简协议 + 强语义分离**：Server 只保证图一致性，不做推理；Dispatcher 是唯一协议写入者，Agent 不直接调 API。职责边界清晰，协议天然可被多端消费。
2. **双抽象接口解耦后端与驱动**：`ExecutionBackend`（container/local 互换）+ `ExecProcess`（进程抽象）+ `WorkerDriver`（厂商 CLI 抽象）三层接口化，新增执行方式或模型只需实现对应 Protocol/Driver。
3. **黑板 + 信息素协作模型**：无中心编排、无 Agent 直接通信，天然规避多智能体通信瓶颈与上下文污染。
4. **Fact 只增不改 + 意图超边**：完整因果链审计，图即推理日志；多源 intent 完整保留"多事实共同支撑一次探索"。
5. **租约 + 心跳 + 超时清理**：服务端读时清理 + 409 冲突仲裁，简单可靠的弱一致互斥，无需分布式锁。
6. **reason 去重按"态势"而非"总变化"**：checkpoint 三元组避免反复触发与死循环。
7. **Prompt 作为可校验资源**：模板占位符启动时强制校验，杜绝运行时渲染漏参；prompt_group 可切换（default/mock）。
8. **Mock 驱动 + 全链路测试**：可概率化/规则化模拟任意成败路径，端到端测试无需真实 LLM/容器，回归成本低。
9. **健康检查进程内化**（最新提交）：用 in-process HTTP 探活替代容器内 curl，去掉对容器依赖与 argv 注入面。
10. **双阶段 conclude 收尾**：超时后同 session 收尾，最大限度抢救已有探索成果，降低"超时即丢失"损失。
11. **状态化日志降噪**（`_log_changed`）：稳定轮询/重复 skip 不刷屏，关键事件必可见。

### 10.2 现有缺陷、耦合问题、技术债务、性能隐患

| 类别 | 具体问题 |
|---|---|
| **死代码/未接线功能** | `ContainerManager.cleanup_orphan` / `needs_orphan_cleanup` / `managed_container_names` 已实现但**未接入 DispatcherLoop**：删除项目（`deleted`）后只会 cancel 本地任务，孤儿容器不会实际被清理（与设计文档承诺矛盾） |
| 单点/规模 | SQLite 单文件 + 单 Dispatcher 进程 + 线程池，无 HA、无持久化调度状态（重启后 admission/冷却/checkpoint 全部重置，reason checkpoint 虽会重建但运行中状态丢失） |
| 调度竞态窗口 | claim 与派发间无分布式原子性，依赖 409 事后仲裁 + "运行中 intent 集合"本地去重；多 Dispatcher 并行时**明确不支持** |
| 时钟一致性隐患 | Server 超时用 `julianday`（wall clock 字符串比较），Dispatcher 心跳判活用 `time.monotonic()`；跨机时钟漂移可能导致租约提前/滞后失效 |
| 时间格式不一致 | 存储 `%Y-%m-%dT%H:%M:%SZ`（UTC），导出转换 `fromisoformat(...replace("Z","+00:00")).astimezone()` 依赖本地时区，且 `format_export_timestamp` 对非法值静默透传 |
| 生产代码用 `assert` 做控制流 | 多处 `assert task.xx is not None`、`assert updated_project is not None`，`python -O` 下静默失效 |
| 失败无重试与补偿 | 写回失败即丢（仅日志），无死信/补偿队列；LLM 端输出不可靠但无结构化重试预算 |
| 前端单体膨胀 | index.html 3400 行内联 JS/CSS，无模块化/构建，多人维护与测试困难 |
| 配置/业务偶合 CTF | 默认示例、AGENTS.md、tsec-actions skill、容器镜像均深度绑定渗透/CTF 场景；prompt 硬编码英文；`ANTHROPIC_MODEL: "deepseek-v4-pro"` 等示例直接指向特定模型 |
| 并发/性能细节 | 主循环每轮同步 `list_projects` + 逐项目 `get_project`（N+1 查询）；`expire_*` 每请求全表扫描式 UPDATE；心跳线程与主循环均按 interval，interval 过小时网络放大 |
| 配置硬编码 | 冷却时长（5s）、通信 grace（15s）、kill grace（5s）、DETAIL_PREVIEW_LIMIT 等散落常量无配置化 |

### 10.3 安全薄弱点

1. **Server 完全无鉴权**：匿名可读可写、可删项目；任何本地/内网用户可篡改图。
2. **无 CORS 配置**：若跨域访问依赖浏览器默认同源限制，功能上受限但同源攻击面敞口。
3. **Agent CLI 全部绕过安全机制**：`--dangerously-skip-permissions` / `--dangerously-bypass-approvals-and-sandbox` / `NOPASSWD:ALL`（容器内 kali 用户），Agent 在项目容器内有 root 级权限。
4. **Dispatcher 挂载 `docker.sock`**：容器模式 Dispatcher 对宿主 Docker 有完全控制权，配合 Agent 提示注入存在逃逸/横向风险。
5. **Local 模式无沙箱**：worker 以宿主用户全权限运行（文档已明示仅限授权环境）。
6. **Prompt 注入面**：图内容/外部 hint 作为 prompt 上下文注入 Agent，恶意内容可操纵 Agent 行为；Server 侧对文本长度无上限。
7. **凭证静态落盘**：`dispatch.yaml` 明文存放 LLM API key（gitignore 但仓库内示例含占位 key）。
8. **容器镜像含大量真实渗透载荷**（ysoserial/PoC 仓库），镜像一旦外泄即构成武器库扩散。

---

## 11. 重构参考决策清单【重点！后续开发直接使用】

### 11.1 ✅ 必须完整保留的底层底座核心能力（不能删减）

1. **协议/执行双层分离**：Server（图一致性）与 Dispatcher（调度执行）解耦 + REST 协议写回。这是整个系统可扩展的根基。
2. **三大执行后端抽象**：`ExecutionBackend` / `ExecProcess` / `WorkerDriver` 三套 Protocol 接口化，保留 container 与 local 双模式。
3. **Fact 只增不改 + Intent 超边因果链** + Hint 图外输入三原语模型（含 goal 禁做 from 源、creator 不可变、worker 语义状态机）。
4. **租约机制**：heartbeat claim/release/conclude + 服务端读时超时清理 + 409 冲突仲裁（intent 级 + 项目级 reason 双租约）。
5. **reason 态势去重 checkpoint**（fact/hint/open_intent 三元组）+ 初始基线补建。
6. **双阶段 conclude 收尾**：execute 超时/解析失败 → 同 session conclude 抢救成果。
7. **Prompt 资源化 + 占位符启动校验** + prompt_group 切换。
8. **Driver 输出契约统一解析**：JSON 提取（围栏/裸 JSON）+ accepted/data 包装兼容 + 单数/复数兼容 + max_intents 截断。
9. **进程安全收尾**：独立进程组、SIGTERM→grace→SIGKILL、容器 exec PID kill、流 drain 关闭兜底。
10. **健康检查进程内 HTTP 探活** + worker 冷却窗口（unhealthy/rejected）+ startup/task 两级模式。
11. **Mock 驱动 + 全链路回归测试**模式（配置化概率/规则/rules 强制结果）。
12. **状态化日志降噪**（重复 skip 不刷屏、关键事件必现）+ 图快照落盘引用（规避 argv 限制）。

### 11.2 ⚙️ 需要重构优化的逻辑

1. **补上孤儿容器清理**：把 `cleanup_orphan`/`managed_container_names` 接入调度循环，按设计文档删除项目即删容器。
2. **去 `assert` 做控制流**：替换为显式异常/返回值；`assert` 仅留不可达不变量。
3. **统一时间源与格式**：全部使用 UTC `datetime` 对象 + ISO8601 存储；超时比较统一用 epoch 数值或同一时钟源；导出时间格式集中化。
4. **配置化运行参数**：冷却时长、grace、重试预算、preview 长度等收敛进 config（或环境变量），消灭散落魔数。
5. **失败重试与补偿**：写回失败增加有限重试（带退避）或 dead-letter 日志；对 LLM 输出增加 schema 级重试预算（如重试 1 次再进 conclude）。
6. **N+1 查询优化**：`list_projects` 的统计子查询在大数据量下改 JOIN/物化列；Dispatcher 轮询改为批量快照接口。
7. **多 Dispatcher 支持的准入层**：若需 HA，将 admission/冷却/checkpoint 迁移到 Server 侧（或外部 KV），否则明确文档化单实例限制。
8. **前端工程化**：将 3400 行单文件拆分为可构建的模块化前端（或保留单文件但抽离纯逻辑层）。
9. **server 与 dispatcher 的 settings 校验**：`intent/reason_timeout > interval` 目前是运行时警告/报错，改为部署前静态校验。

### 11.3 ⏸️ 可选、非必需扩展功能（可后期迭代）

1. **Intent `worker_history`**：记录历史 claim 者，补足停止后可观测性（设计文档已给出方向）。
2. **项目级 Fact/Intent 全文检索**：引入 SQLite FTS5 或轻量检索，支持图内关键词回溯。
3. **导出格式扩展**：JSON/GraphML/可视化图片导出；timeline 已具备事件流，可做结构化事件导出。
4. **Replay 增强**：将回放导出为视频/分享链接；按事件类型筛选。
5. **多 prompt 组管理后台**：可视化编辑 prompt 模板并在线切换/校验。
6. **统计仪表盘**：每项目/每 worker 成功率、耗时分布、超时率指标（当前只有日志）。
7. **Worker 队列策略扩展**：优先级抢占、task 亲和性、成本感知调度（按模型单价）。
8. **通知集成**：项目完成/停滞事件推送到 Webhook/IM。

### 11.4 ❌ 原始项目不合理设计，新版本直接摒弃

1. **Server 无鉴权** → 新版本必须引入最小鉴权（至少共享 Token / API Key 中间件），Dispatcher 与前端均携带。
2. **生产代码使用 `assert` 做状态守卫** → 摒弃。
3. **时间处理混用 UTC 字符串与 monotonic、本地时区转换** → 摒弃，统一。
4. **`--dangerously-skip-permissions` / `--dangerously-bypass-approvals-and-sandbox` 无条件默认开启** → 应改为显式、可审计、按 task 最小化授权（例如仅 `NET_RAW` 等 cap 按需开，而非全放开）。
5. **Dispatcher 裸挂 `docker.sock`** → 改用受限 Docker API 代理 / 独立 executor 服务，或默认 Local 模式。
6. **Config 中明文 API key** → 改为环境变量 / secret 注入，禁止入库与示例文件。
7. **单 Dispatcher 不可协调** → 要么文档化并加启动互斥（如 Server 侧 dispatcher 租约），要么重构为协调式。
8. **死代码孤儿清理未接线** → 新版本要么实现要么删除。
9. **前端单体 3400 行内联** → 不再延续无构建单体模式。
10. **业务绑定 CTF/渗透**（AGENTS.md、tsec-actions、Kali 镜像、中文场景）→ 作为"示例应用"与核心引擎解耦，镜像与 skill 移出核心仓库。

---

## 12. 隐藏业务约束与隐性规则

> 以下均为**代码/测试强制但无显式文档（或仅散落在文档角落）**的边界条件，重构时必须遵守：

1. **`goal` 永远不能作为 Intent 的 from 源**（`validate_goal_not_in_sources`），否则 400。
2. **创建 intent 时 `worker` 只能是 `null` 或 `== creator`**，否则 400。
3. **reason 输出规则**：`open_intents` 为空且无 `complete` 时**必须**返回非空 intents，否则校验抛错 → 任务 failed；`complete` 与 `intents` 互斥不能同存。
4. **reason 单次最多创建 `max_intents` 条 intent**；若创建 0 条（全部 403/409/写失败）则整个任务判 `failed`。
5. **reserved bootstrap intent 三重标识**（description="bootstrap" + creator="dispatcher.bootstrap" + from=["origin"]）是 Dispatcher 与前端**共同识别**的硬约定（前端 `isBootstrapIntent`、调度 `_is_bootstrap_intent`、`_get_bootstrap_intent` 排序取"未认领优先"）。
6. **`_get_bootstrap_intent` 在多个 bootstrap intent 时只告警不纠错**，取"未认领且创建最早"的那个——潜在重复 bootstrap intent 是容忍项。
7. **403 语义是"项目已非 active"**：reason 的 complete/intent 创建遇 403 视作**成功收场**（返回 success，不再写图）；`release` 对 403/409 静默跳过不告警。
8. **409 语义**：heartbeat/reason claim 遇 409 = 他人持有，本机**不启动任务**；reason intent 创建遇 409 = 竞争丢失，跳过该条继续。
9. **心跳失败宽限 = `max(interval, 2×interval)`**；403/409 立即判死并 kill 进程；其他失败容忍到宽限。
10. **server `intent_timeout`/`reason_timeout` 必须 `> interval`**（否则启动即 RuntimeError），`< 2×interval` 仅告警。
11. **进程超时判定**：`timed_out` 或返回码 124/137（且非取消）→ timeout；`cancel` 优先于 timeout（`did_timeout` 要求 `not cancelled`）。
12. **conclude 收尾三重前置**：`driver.supports_conclude()` + 有 session + 项目仍 active（`project_allows_conclude_fallback` 再查一次）；心跳已失或已取消则跳过。
13. **healthcheck `startup_and_task` 时任务启动前再查一次**；而 **explore/bootstrap 的 conclude 阶段不再查**。
14. **图快照写容器 `/tmp/cairn-prompts/<phase>-<12hex>/graph.yaml`**，prompt 中只给文件引用路径（大图不内联），且每次 phase 独立目录。
15. **容器名 `cairn-dispatch-<project_id 的 / → ->`**；容器内写文件路径必须绝对路径且禁 `..`/`.`（tar 构造时校验，防穿越）。
16. **completed 容器**：`completed_action=stop` 只停止（保现场），`remove` 删除；**deleted 项目容器设计上应删除但当前未接线**（见 10.2）。
17. **`stopped` 项目**：server 立即清空 open intent worker + reason；Dispatcher 视为硬停止——取消本地任务、**不再进入 conclude fallback**、排队停容器。
18. **`reopen` 删除完成边不留历史**：重新打开后图内不保留"曾 completed"记录，timeline 也不再有那次 COMPLETED 事件。
19. **hint 写权限最宽松**：active/stopped/completed 都可写；写 hint 不触发 reason 之外的特殊行为（但 hint 数量增加是 reason 重触发条件之一）。
20. **reason 重触发只看"数量增加"不看内容**：fact/hint **数量**变多才触发；内容变化但数量不变不触发。
21. **Worker 并发建模约定**：一个 Worker = 一个独立 LLM 并发配额单元；**同一 key 不应拆成多个 Worker**，否则并发无法正确控制。
22. **common_env 合并优先级**：`common_env` 先合并进每个 worker，`worker.env` 再覆盖（`{**common_env, **worker.env}`）；local 模式再叠加宿主 `os.environ` 为基底。
23. **container 模式必须提供全部 LLM env key**（claudecode 3 键 / codex 3 键 / pi 4 键），缺则加载报错；**local 模式禁止要求任何 key**。
24. **mock 概率必须精确 sum=1.0**（Decimal 严格相等），delay 非负且 `[0]≤[1]`；未知 `MOCK_*` 键直接校验失败。
25. **`.gitignore` 排除 `dispatch.yaml`**：实际配置不入库，示例文件才是唯一受控配置。
26. **健康检查只看 HTTP 状态码 2xx**，不解析响应体；`local` 模式改为对 CLI 执行 `--help` 探测，`--help` 返回 0 即"可运行"。
27. **`extract_json_object` 行为**：返回首个合法 JSON 对象；普通文本/围栏代码块均可；找不到则抛 `ValueError` → 任务 failed → 双阶段 Worker 进 conclude。
28. **协议客户端连接池**：每线程一个 Session（thread-local），连接池 64，线程结束不自动清理（靠 `client.close()` 统一关）。
