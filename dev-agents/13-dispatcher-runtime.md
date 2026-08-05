# Agent 13 — Dispatcher 执行抽象与 Worker 驱动（Runtime Abstractions & Drivers）

> 阶段 0 · 可与 10/11/12 并行。**独立于 Server**。你的职责是把「ExecutionBackend/ExecProcess/WorkerDriver 三抽象 + claude/codex/pi 驱动 + 取消」从 0 重建——v2 明确「必须完整保留」的执行管线底座，**无 v1 代码可迁移**。

## 0. 开工前必读（按顺序）
1. `CLAUDE.md`（黄金不变量 1/2/10）
2. `docs/architecture-research-report-pentest-v2.md` §8.4（Worker 驱动保留+scope 提示）、§8.5（执行后端加固）、§8.6（心跳与取消保留）、§11.1 第 2/6/9/10 条
3. `docs/architecture-research-report.md` §8.4-8.6（**保留模块的细节权威来源**：驱动 CLI 调用方式/session 提取/健康检查、ExecutionBackend 协议、ContainerManager/LocalBackend/ManagedProcess/LocalProcess、HeartbeatLease/TaskCancellation）——仅这四节按 v2 语义「改造后保留」读作权威，其余 v1 内容不作实现依据
4. `docs/backend-module-skeleton.md` §1（dispatcher/workers+runtime 目录）
5. `docs/dispatch-config-spec.md` §8（container 段）、§5（security/executor）、§7（tuning）
6. `docs/worker-sandbox-hardening.md` §4-§6（运行时加固、CA 注入、executor 侧车）
7. `docs/exploration-graph-spec.md` §4 规则 14/15/21/22/23/26（图快照引用/容器名/并发建模/env 合并/LLM env key/健康检查口径）
8. `docs/rule-registry.md`（C1/C5/C6）

## 1. 交付范围（创建/修改）
```
cairn/src/cairn/dispatcher/runtime/backend.py        # ExecutionBackend / ExecProcess 协议抽象（container/local 双实现已由 11 提供，这里定义协议并装配）
cairn/src/cairn/dispatcher/runtime/cancellation.py   # TaskCancellation：线程安全、首次 cancel 记 reason、幂等、attach 进程时已取消立即 cancel
cairn/src/cairn/dispatcher/workers/__init__.py
cairn/src/cairn/dispatcher/workers/base.py           # WorkerDriver 抽象：check_health/build_execute/build_conclude/prepare_session/extract_session/extract_response_text/supports_conclude/local_binary
cairn/src/cairn/dispatcher/workers/registry.py       # 按 name 注册/查询驱动；缺 key 启动报错
cairn/src/cairn/dispatcher/workers/health.py         # 进程内健康检查（startup/task 两级）+ worker 冷却状态（v2 §11.1-10）
cairn/src/cairn/dispatcher/workers/adapters/claude.py  # SeedSessionDriver（claudecode）
cairn/src/cairn/dispatcher/workers/adapters/codex.py   # RegexSessionDriver（stderr 正则提取 session）
cairn/src/cairn/dispatcher/workers/adapters/pi.py      # 事件流解析驱动（session + 最后 assistant 文本）
cairn/src/cairn/cli.py                                # dispatch 子命令装配：加载 config → 起 40 的 loop 入口（loop 本体 40 提供，你接 CLI 参数/信号/SIGTERM→grace→SIGKILL 收尾）
cairn/tests/test_worker_drivers.py + test_cancellation.py
```
> **与 11/31 的分工**：11 提供容器/local 后端与镜像（`runtime/containers.py`/`local_backend.py`/`process.py`）；31 提供 mock 驱动扩展（`adapters/mock.py`）；本包定义 `ExecutionBackend`/`ExecProcess`/`WorkerDriver` 协议并实现 claude/codex/pi 三驱动——11/31 按你定义的协议实现各自部分。

## 2. 必须满足的契约
- **ExecutionBackend 协议**：`ensure_running`/`build_exec_process`/`write_text_file`/`cleanup_*`/`close`；`ExecProcess`：`communicate`/`poll`/`kill`/`timeout` 语义。11 的容器/本地实现须实现本协议（你在交接物里写清协议签名，11 对齐）。
- **WorkerDriver 抽象**（v1 §8.4 方法面，v2 语义）：
  - `claude`（SeedSessionDriver）：预生成 UUID session，`claude --session-id <s> --dangerously-skip-permissions -p -- <prompt>`；二阶段 `claude -r <s> ...`；健康检查打 `/v1/messages`。
  - `codex`（RegexSessionDriver）：从 stderr 正则 `session id:\s*([0-9a-fA-F-]+)` 提取；`codex exec --dangerously-bypass-approvals-and-sandbox --model ... -c model_provider="cairn" ...`；二阶段 `codex exec resume <s>`。
  - `pi`：shell 包装脚本注入 `models.json`（provider cairn）+ `--session-dir`；从 stdout JSONL 事件提取 session（`type:session`）与最后 assistant 文本（`turn_end`/`agent_end`）；支持三种 wire API（openai-completions/responses/anthropic-messages）；可选 `PI_MODEL_CONTEXT_WINDOW`。
  - **local 变体**：codex/pi 在 `runtime.execution: local` 时省略所有 provider 注入，直接调宿主机原生 CLI。
- **scope 提示注入（v2 §8.4）**：prompt 渲染注入 scope 边界说明（「仅允许目标集 X，禁止 Y」）——驱动不感知，你在交接物里注明 30 在 prompt 组装时注入。
- **env/并发（exploration-graph-spec §4 规则 21-23）**：一个 Worker = 一个 LLM 并发配额单元；container 模式必须提供全部 LLM env key（claudecode 3/codex 3/pi 4），缺则加载报错；local 模式禁止要求任何 key。
- **健康检查（v2 §11.1-10）**：startup 级 + task 级两级；只看 HTTP 2xx（不解析 body）；local 模式对 CLI `--help` 探测，返回 0 即「可运行」。健康状态驱动 worker 冷却（`worker_unhealthy_until`，40 消费）。
- **TaskCancellation**：线程安全、首次 cancel 记录 reason、后续幂等；attach 进程时若已取消立即 cancel；kill switch（C1）走**即时 SIGKILL**（不经 SIGTERM→grace）。
- **CLI 装配**：`cairn dispatch` 加载 dispatch.yaml（12 的 config）→ 起 40 的 loop 入口（你定义回调签名，40 实现 loop 注入）→ 信号处理 SIGTERM→grace→SIGKILL 收尾。

## 3. 验收标准（可执行）
1. `pytest test_worker_drivers.py`：claude/codex/pi 三驱动的 session 提取/健康检查/execute/conclude 命令构造用**假 CLI 脚本**跑通（无需真实 LLM）；local 变体不注入 provider。
2. `pytest test_cancellation.py`：首次 cancel 记 reason、幂等、attach 后 cancel 立即 kill 绑定进程。
3. 缺 LLM env key：container 模式加载报错（构造缺 key 的 config 断言）；local 模式不要求。
4. CLI：`cairn dispatch --help` 正常；传入 loop 回调（用假 loop 冒烟）能起能停（SIGTERM 收尾）。
5. 与 11 协议对齐：`ExecutionBackend` 协议签名双方一致（写交接物核对）。

## 4. 硬约束
- **不在驱动内注入 Cairn token**（C5）：驱动只处理 LLM CLI；Agent 侧一切 Cairn API 访问经 Dispatcher（30）代理。
- **不实现任务逻辑**（30）；不实现调度循环/worker 选择/心跳（40）；不实现 mock 驱动扩展（31）。
- **不碰 Server**：纯 Dispatcher 侧代码，无 DB 直连。
- `--dangerously-*` 等 flag 仅在**授权环境 + 沙箱加固**前提下由 11 的容器配置兜底，代码注释标注 C5/C1。
- 驱动细节以 v1 §8.4 为权威（改造后保留）；与 v2 冲突时按 v2 语义（scope 注入/加固）改，改前先列 diff。

## 5. 交接物
写 `dev-agents/notes/13-dispatcher-runtime.md`：三驱动实现清单、协议签名（给 11/31）、CLI 装配接口（给 40）、健康检查/冷却接口（给 40）、env 需求清单、未做项。
