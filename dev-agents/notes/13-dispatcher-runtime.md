# 13-dispatcher-runtime 交接物

- 完成 Agent：13（dispatcher-runtime）  日期：2026-08-06
- 阶段 0 · 与 10/11/12 并行完成。纯 Dispatcher 侧，不碰 Server / DB。

## 1. 实现清单

本包拥有 `cairn/src/cairn/dispatcher/runtime/`（backend/cancellation/context）+ `workers/`（base/registry/health/adapters/*）+ `dispatcher/cli.py`。

### runtime/
| 文件 | 关键符号 | 说明 |
|---|---|---|
| `runtime/backend.py` | `ExecProcess`（Protocol）、`ExecutionBackend`（Protocol） | 执行抽象协议（见 §3）。**11 按此实现容器/local 后端与进程模型** |
| `runtime/cancellation.py` | `TaskCancellation` | 线程安全；首次 cancel 记 reason、幂等；attach 已取消立即 kill；cancel 时 kill 全部绑定进程；`kill_switch()` 走即时 SIGKILL（C1） |
| `runtime/context.py` | `DispatcherContext` | 13 CLI → 40 loop 的装配对象（config/drivers/health/shutdown/grace_seconds/force_kill/log） |

### workers/
| 文件 | 关键符号 | 说明 |
|---|---|---|
| `workers/base.py` | `WorkerDriver`（ABC）、`WorkerCommand`、`WorkerDriverError`/`MissingEnvError`/`UnknownDriverError` | 驱动抽象：`prepare_session`/`build_execute`/`build_conclude`/`extract_session`/`extract_response_text`/`supports_conclude`/`check_health`；env 合并按 graph §4-22；container 模式缺 key 构造即抛 `MissingEnvError`（graph §4-23） |
| `workers/registry.py` | `DRIVER_CLASSES`、`register_driver`、`get_driver_class`、`build_worker_driver`、`normalize_type`/`is_local_variant` | 按 name 查/注册；`*_local`/`local_*` 变体归一化并强制 local；`register_driver` 供 31 注入 mock |
| `workers/health.py` | `WorkerHealth` | startup/task 两级健康检查 + 冷却（`worker_unhealthy_until` 墙钟，可 `snapshot`/`load_snapshot` 落 scheduler_state）；mode=startup_and_task/startup_only/disabled |
| `workers/adapters/claude.py` | `ClaudeDriver` | SeedSessionDriver：`claude --session-id <s> --dangerously-skip-permissions -p -- <p>`；二阶段 `claude -r <s> ...`；健康 GET `<base>/v1/messages`（2xx） |
| `workers/adapters/codex.py` | `CodexDriver` | RegexSessionDriver：`codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --full-auto [-c model_provider=...] <p>`；stderr 正则 `session id:\s*([0-9a-fA-F-]+)`；二阶段 `codex exec resume <s>` |
| `workers/adapters/pi.py` | `PiDriver` | 事件流驱动：shell wrapper 注入 `models.json`（provider cairn）+ `--session-dir`；stdout JSONL 提取 session（`type:session`）与最后 assistant 文本（`turn_end`/`agent_end`）；三种 wire API；可选 `PI_MODEL_CONTEXT_WINDOW` |

### CLI 装配
| 文件 | 关键符号 | 说明 |
|---|---|---|
| `dispatcher/cli.py` | `main_dispatch(argv=None, *, loop_runner=None, config_loader=None, driver_factory=None, health_factory=None) -> int` | 加载 12 config → registry 建驱动（缺 key 报错退出 1）→ 建 WorkerHealth → SIGTERM/SIGINT→`ctx.shutdown`→grace 后 `ctx.force_kill`（SIGKILL）→ 调 40 loop 入口。`--config`/`--version`。另导出 `build_drivers(config)`/`build_health(config)` |
| `runtime/context.py` | `DispatcherContext` | 40 loop 的输入对象 |

测试：`tests/test_worker_drivers.py`（25 例）、`tests/test_cancellation.py`（9 例）、`tests/test_dispatch_cli.py`（8 例）；假 CLI 脚本 `tests/scripts/{claude,codex,pi}`（PATH 前置即可替换真 CLI）。

## 2. 未实现 / 待定

- **顶层 `cairn/src/cairn/cli.py` 归 10**，不属本包；10 已建 `dispatch` 占位并懒转调 `main_dispatch`（已核对，装配完成）。
- **任务逻辑（30）**、**调度主循环 / worker 选择 / 心跳（40）**、**mock 驱动（31）** 均未实现——纯协议/驱动/CLI 装配。
- **真实 loop 未接线**：`cairn dispatch`（无 `loop_runner`）会懒导入 `cairn.dispatcher.scheduler.loop.run_dispatch_loop`，导入失败返回退出码 2 并打印提示。
- **容器 HTTP 健康探针的路径选择有真实环境风险**：claude 用 GET `<base>/v1/messages`、codex 用 GET `<base>/v1/models`、pi 按 wire API 选 `/v1/models` 或 `/v1/messages`；只接受 2xx。真实网关对 GET 可能返回 404/405 → worker 被判 unhealthy。**40 联调时如遇此情况，应在交接/issue 中决定是否放宽为「base 可达即健康」**（测试用 stub 固定返 200，未覆盖真实网关）。
- **12 的 `test_protocol_client.py` 4 例失败**（`test_bearer_header_sent_and_valid_auth_ok`/`test_business_409_raises_corresponding_error_code`/`test_lease_conflict_409`/`test_scope_denied_403`）——其 stub 对有效 token 返 401 [AUTH_INVALID]，属 12 并行进行中的测试/桩问题，与本包无关（本包测试全绿）。未改 12 文件。
- **`dispatch_mock.yaml` 的 `type: mock`**：容器模式当前报 `UnknownDriverError`（预期）——31 注册 mock 驱动后即通（mock 的 `required_env_keys` 应为空）。

## 3. 协议签名（给 11 / 30 / 40 —— 冻结契约）

### ExecutionBackend / ExecProcess（`runtime/backend.py`，11 实现）
```python
class ExecProcess(Protocol):
    @property
    def pid(self) -> int | None: ...
    @property
    def timed_out(self) -> bool: ...   # 超时被杀后置 True
    def poll(self) -> int | None: ...
    def communicate(self, input: str | None = None, timeout: float | None = None) -> tuple[str, str]: ...  # (stdout, stderr)
    def kill(self, sig: int | None = None) -> None: ...  # None=grace(SIGTERM→grace→SIGKILL)；具体信号=即时(C1 SIGKILL)

class ExecutionBackend(Protocol):
    def ensure_running(self, project_id: str) -> None: ...
    def build_exec_process(self, command: list[str], *, env: dict[str, str] | None = None,
                           cwd: str | None = None, session_id: str | None = None,
                           timeout: float | None = None) -> ExecProcess: ...
    def write_text_file(self, project_id: str, rel_path: str, content: str) -> None: ...  # 禁 .. / . 穿越（graph §4-15）
    def cleanup_managed_container(self, project_id: str, reason: str = "completed") -> None: ...  # completed_action stop/remove
    def close(self) -> None: ...
```

### WorkerDriver（`workers/base.py`，30 使用 / 31 扩展）
```python
WorkerCommand = dataclass(argv: list[str], env: dict[str, str])

class WorkerDriver:
    driver_type: ClassVar[str]          # registry key
    required_env_keys: ClassVar[tuple[str, ...]]   # container 必填（graph §4-23）
    local_binary: ClassVar[str | None]
    base_url_env: ClassVar[str]
    health_path: ClassVar[str]
    def __init__(self, *, execution="container", common_env=None, worker_env=None, binary_path=None): ...
    def prepare_session(self) -> str | None      # claude=uuid；codex/pi=None（CLI 自建）
    def extract_session(self, stdout, stderr) -> str | None
    def build_execute(self, prompt, *, session_id=None, **kw) -> WorkerCommand
    def build_conclude(self, prompt, *, session_id=None, **kw) -> WorkerCommand
    def extract_response_text(self, stdout) -> str | None
    def supports_conclude(self) -> bool
    def check_health(self, *, timeout=None) -> bool
```

### 30 的执行编排（建议）
```python
sid = driver.prepare_session()                        # 首阶段
cmd = driver.build_execute(prompt, session_id=sid)
proc = backend.build_exec_process(cmd.argv, env=cmd.env, timeout=task_cfg.timeout)
stdout, stderr = proc.communicate()
sid = driver.extract_session(stdout, stderr)          # 非 seed 驱动从此取 session
text = driver.extract_response_text(stdout)
# 超时/取消时: cancellation.attach_process(proc) 全程挂载，取消即 kill
```

### 31（mock 驱动）
`register_driver("mock", MockDriver)`；`MockDriver(WorkerDriver)` 且 `required_env_keys=()`（container 模式下无 key 要求），`prepare_session` 返回 seed，`supports_conclude` 保留（phase 语义由 31 定义）。

## 4. CLI 装配接口（给 40）

- **loop 入口（40 实现）**：`def run_dispatch_loop(ctx: DispatcherContext) -> int`，放在 `cairn/dispatcher/scheduler/loop.py`。
- `DispatcherContext` 字段：`config`（12 的 DispatcherConfig）、`drivers`（`{worker_name: WorkerDriver}`）、`health`（WorkerHealth）、`shutdown`（threading.Event，SIGTERM/SIGINT 置位）、`grace_seconds`（float，默认 10.0，可用 env `CAIRN_DISPATCH_GRACE` 覆盖）、`force_kill(reason)`（40 接线为 SIGKILL 所有运行 worker）、`log(msg)`。
- 13 已做：config 加载、驱动构建与 env 校验、健康检查对象、启动级健康检查（mode≠disabled 时逐 worker 跑 `startup_check`）、信号处理（SIGTERM→shutdown→grace 后 force_kill）、`--config`/`--version`。
- 40 负责：主循环轮询 `ctx.shutdown`、任务级健康检查（`health.check(name, driver)` 走冷却）、`health.snapshot()`/`load_snapshot()` 落/读 scheduler_state、把 `ctx.force_kill` 接上真实 SIGKILL。
- `worker_healthcheck` 枚举与 12 contracts 一致（startup_and_task/startup_only/disabled）；`build_drivers` 从 `config.runtime.execution` + `config.common_env` + `worker.env` 装配。

## 5. 健康检查 / 冷却接口（给 40）

`WorkerHealth(mode, cooldown_seconds, timeout)`：
- `startup_check(name, driver) -> bool`：启动级（失败→`mark_unhealthy(name, "startup_healthcheck_failed")`）。
- `check(name, driver) -> bool`：任务级，冷却期内直接 False 不重探。
- `is_unhealthy(name)` / `unhealthy_until(name)` / `unhealthy_reason(name)` / `mark_healthy(name)`。
- `snapshot() -> {name: wall_clock_deadline}` / `load_snapshot(dict)`：供 scheduler_state 持久化。
- 冷却时长取自 `tuning.worker_unhealthy_cooldown_seconds`（默认 5s），健康检查超时取 `runtime.healthcheck_timeout`。

## 6. env 需求清单（container 模式必填；local 模式禁要求）

| 驱动 | 必填 key | 说明 |
|---|---|---|
| claudecode | `ANTHROPIC_MODEL` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | provider 走 env，无 CLI 注入；local 变体同 argv |
| codex | `CODEX_MODEL` / `CODEX_BASE_URL` / `OPENAI_API_KEY` | provider 走 `-c model_provider=...`（仅 container）；local 省略全部 provider 注入 |
| pi | `PI_MODEL` / `PI_BASE_URL` / `PI_API_KEY` / `PI_PROVIDER_API` | 可选 `PI_MODEL_CONTEXT_WINDOW`；`PI_PROVIDER_API` ∈ openai-completions/responses/anthropic-messages；local 省略 models.json 注入 |

缺失任一必填 key 且 execution=container → 构造 `MissingEnvError` → CLI 退出码 1。local 变体（`local_*`/`*_local` 或 runtime.execution=local）一律不校验 key。

## 7. 自测结果

- `pytest cairn/tests/test_worker_drivers.py`：**25 passed**（三驱动 session 提取/健康/execute/conclude 假 CLI 跑通 + local 不注入 provider + 缺 key 报错 + 注册表）
- `pytest cairn/tests/test_cancellation.py`：**9 passed**（首 cancel 记 reason/幂等/attach 后 cancel 立即 kill/杀全/attach 已取消立即 kill/SIGKILL 即时）
- `pytest cairn/tests/test_dispatch_cli.py`：**8 passed**（--help 0 / config 错 1 / 缺 key 1 / 未接线 2 / SIGTERM 优雅收尾 0 / grace 后 force_kill）
- 控制台冒烟：`uv run --project cairn cairn dispatch --help` 与 `--version` 均 exit 0（10 占位转调已验证）。
- 示例配置集成：`dispatch.local.example.yaml` 加载+建驱动 OK（execution=local，无 key）；`dispatch_mock.yaml` 仅因 `type: mock`（31 未注册）报 UnknownDriverError，符合预期。
- 全量 `cairn/tests`：**81 passed / 4 failed**，4 failed 全在 12 的 `test_protocol_client.py`（stub 有效 token 返 401），与本包无关。

## 8. 给下游的注意事项

- **C5**：驱动不注入 Cairn token、不接触 Server；`--dangerously-*` 仅在授权 + 沙箱加固容器内使用（注释标注 C5/C1）。
- **graph §4-22 env 合并**：container=`{**common_env, **worker.env}`；local=`{**os.environ, **common_env, **worker.env}`。`build_execute` 返回的 `WorkerCommand.env` 即最终进程 env（30 原样传给 `backend.build_exec_process`）。
- **scope 提示注入**（v2 §8.4）：驱动不感知 scope；30 在 prompt 组装时注入「仅允许目标集 X，禁止 Y」。
- **timeout**：驱动构造不含超时参数；任务超时由 30 在 `backend.build_exec_process(timeout=...)` 传入（来源 `tasks.*.timeout`）。
- **`execution` 语义**：local 变体既可由 `runtime.execution: local` 触发，也可由 `type: codex_local` 显式强制（registry 归一化 `*_local`/`local_*`）。
- **信号处理**：`_install_signal_handlers` 仅主线程生效（非主线程安全跳过）；SIGINT 与 SIGTERM 同路径。测试中 SIGTERM 由辅助线程 `os.kill` 触发，用 `CAIRN_DISPATCH_GRACE` 控制短 grace。
- 未 git commit（按编排要求）。
