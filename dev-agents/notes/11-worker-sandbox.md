# 11-worker-sandbox 交接物
- 完成 Agent：11（worker-sandbox）  日期：2026-08-06
- 阶段 0 · 与 10/12/13 并行完成。交付容器层 + 镜像 + 执行后端加固；独立于 Server。

## 1. 实现清单

| 文件 | 关键符号 | 说明 |
|---|---|---|
| `container/Dockerfile` | — | debian:bookworm-slim 精简 Worker 镜像：非 root `worker`、无 sudo/su、无 docker CLI、白名单工具 + nodejs/npm + `npm install -g` claude/codex/pi（按 hardening §3 固定版本）。`ENV HOME=/home/worker/workspace`（B6，read_only 根下唯一可写）。`USER worker` + `ENTRYPOINT` 一起用（entrypoint 以 worker 身份跑，best-effort 建目录/权限） |
| `container/AGENTS.md` | — | prompts-pentest-templates §10 模板原文（占位符 `{scope}` 保留，Dispatcher 渲染时注入） |
| `container/entrypoint.sh` | — | 容器内启动钩子：mkdir workspace/evidence + best-effort chown（非 root 下失败无害），`exec "$@"` |
| `cairn/src/cairn/dispatcher/runtime/process.py` | `LocalProcess`、`ContainerProcess`、`resolve_workspace_path`、`CONTAINER_PID_MARKER`、`ProcessError` | ExecProcess 实现。`LocalProcess`=宿主子进程 + `start_new_session=True`（进程组隔离）；`kill(None)`=SIGTERM→grace→SIGKILL，具体信号（SIGKILL）=即时（C1）。stdout/stderr 后台线程分片 drain（行缓冲），可挂 `on_line(line, stream)` 回调给进度包 24。`ContainerProcess`=包装宿主 `docker exec` 客户端子进程，kill 时额外尝试容器内 `kill` 兜底（stderr marker 解析容器 PID）。`resolve_workspace_path`=graph §4-15 防穿越 |
| `cairn/src/cairn/dispatcher/runtime/containers.py` | `ContainerBackend`、`ContainerScope`、`ContainerBackendError` | 实现 13 的 `ExecutionBackend`。加固（hardening §4）：`cap_drop=["ALL"]` + 按 `scope.network_cap` 加 NET_RAW/NET_ADMIN、`user=worker:worker`、`read_only=True`、`tmpfs /tmp 512m`、`security_opt=no-new-privileges`、mem/cpu/pids、nofile ulimit、labels `cairn.project/cairn.engagement/cairn.managed`。卷：workspace `{workspace_root}/{project_id}`↔`/home/worker/workspace`（rw，B6）、evidence `{evidence_root}/{engagement_id}`↔`/home/worker/evidence`（rw，B7 按 engagement 作用域）、tools `{tools_root}`↔`/opt/tools`（ro，按授权）。捕获注入（§4.1）：HTTPS_PROXY/HTTP_PROXY/SSL_CERT_FILE/REQUESTS_CA_BUNDLE/NODE_EXTRA_CA_CERTS/NO_PROXY + CA 绑定挂载 `/etc/cairn-capture/ca.pem`（ro）。`build_exec_process` 走 `docker exec`（CLI 子进程），加 `sh -c 'echo "__CAIRN_PID__:$$" >&2; exec "$@"' _ CMD` 包装取容器 PID。**C5 守卫**：注入 env 含 Cairn token（`CAIRN_API_TOKEN`/`security.api_token_env`/`capture_token_env`/`CAIRN_SERVER*`）→ 抛 `ContainerBackendError`。孤儿清理：`managed_container_names()` + `cleanup_orphan(known_project_ids)`（v2 §8.2）。docker SDK **惰性导入**（测试注入 fake client 不依赖 docker 包） |
| `cairn/src/cairn/dispatcher/runtime/local_backend.py` | `LocalBackend`、`LocalBackendError` | 实现 `ExecutionBackend`。仅授权环境、无 sandbox 声明。`ensure_running`=建 workspace 目录；`build_exec_process`=宿主进程组隔离子进程（`{**os.environ, **env}`）；`write_text_file` 同防穿越；`cleanup_managed_container`=completed_action=remove 时删 workspace（keep 为 no-op） |
| `cairn/src/cairn/dispatcher/runtime/__init__.py` | 追加导出 | 加 `ContainerBackend/ContainerScope/ContainerBackendError/LocalBackend/LocalBackendError/LocalProcess/ContainerProcess/ProcessError/CONTAINER_PID_MARKER/resolve_workspace_path`（仅追加，未改 13 的 backend/cancellation/context） |
| `cairn/tests/test_local_execution.py` | 28 例 | LocalProcess/LocalBackend 真实宿主进程：协议 conform、workspace 创建、communicate/stdin、timeout→timed_out、SIGKILL 即时、**进程组隔离清理**（C1）、write_text_file + 防穿越、cleanup/close |
| `cairn/tests/test_container_archives.py` | 28 例 | 纯函数（exec argv / 容器名 / 防穿越）+ fake docker client（加固 kwargs、卷挂载 B6/B7、CA 挂载、cap 授权、host 网络）+ fake docker CLI 全链路 `docker exec`（ContainerProcess 真跑）+ C5 token 拒绝 + 捕获 env 注入 + 孤儿清理 |
| `cairn/tests/scripts/fake_docker.py` | — | fake `docker exec`（PATH/argv 替换），与 13 的假 CLI 同模式 |
| `docker-compose.yaml` | — | 按 hardening §7：cairn-server + cairn-dispatcher + 可选 `cairn-executor` 侧车（P1，profile-gated，未实现）。**dispatcher 不裸挂 docker.sock**（改挂 workspace/evidence/capture-ca 卷；本地开发临时挂 docker.sock 以注释给出） |

## 2. 未实现 / 待定

- **Docker 实际构建/运行验收未执行**：本环境 docker CLI 执行被拒（`docker build`/`docker run`/`docker --version` 均 Permission denied），无法跑 §3 的构建 + 运行态冒烟 + 容器内 SIGKILL 冒烟。已用 fake docker client + fake docker CLI 在单元层覆盖等价行为（加固 kwargs、exec argv、C1 SIGKILL 路径）。**Dockerfile 本身可审查**（non-root、无 sudo/su/docker CLI、nodejs/npm、CLI 固定版本、HOME 指向 workspace）。
- **`cairn-executor` 侧车未实现**（P1，属未来包）：compose 里是 profile-gated 占位；容器模式在 executor 存在前需要 dispatcher 直接可达 docker（本地开发临时挂 docker.sock，已注释）。
- **CA 生成/轮换**：Dispatcher 侧 per-engagement CA 生成属于 23-capture；本包只做「CA 文件存在则挂载 `/etc/cairn-capture/ca.pem`(ro) + env 信任链」。
- **`docker` Python SDK 未加入 pyproject**（避免引入未明示依赖/破坏 lock）：`ContainerBackend` 惰性导入，容器模式运行时需 `pip/uv add docker`（或注入 `docker_client`）。local 模式不需要。
- **`workspace_root` 无 config 字段**：`SecurityConfig` 无 workspace_root；backend 构造默认 `/var/cairn/workspace`，可注入覆盖（30/40 装配时按部署传宿主机绝对路径）。

## 3. 对下游包的依赖假设

- **13 的冻结协议**：`ExecProcess`（pid/timed_out/poll/communicate/kill）与 `ExecutionBackend`（ensure_running/build_exec_process/write_text_file/cleanup_managed_container/close）——严格按 `runtime/backend.py` 实现，未自行发明抽象。
- **30（任务执行）**：按 13 交接物 §3 编排——`backend.ensure_running(project_id)` 先于本 project 的 `build_exec_process`（容器模式靠线程本地 current_project 路由容器）；`proc.communicate()` 拿 (stdout, stderr)；`cancellation.attach_process(proc)` 挂载。
- **40（调度主循环）**：`backend.cleanup_orphan(known_project_ids)` / `managed_container_names()` 接入孤儿清理；`cleanup_managed_container(project_id)` 接 completed_action。
- **24（进度包）**：`LocalProcess(..., on_line=lambda line, stream: ...)` 可挂分片写入回调（当前 `build_exec_process` 传 None，24 需要时改一处即可）。
- **23（capture）**：本包只消费 `scope_resolver` 返回的 `capture_proxy`（host/port/no_capture_hosts）与 CA 文件路径；CA 文件由 23 生成到 `security.capture_ca_dir/{engagement_id}/ca.pem`。

## 4. 自测结果

- `pytest cairn/tests/test_local_execution.py cairn/tests/test_container_archives.py`：**56 passed**（2.37s）
- 全量 `uv run --project cairn pytest -q`：**141 passed**（85 既有 + 56 新），无回归
- YAML：`dispatch.example.yaml / dispatch_mock.yaml / dispatch.local.example.yaml / docker-compose.yaml` 均 `yaml.safe_load` OK
- 协议 conform：`isinstance(LocalBackend(), ExecutionBackend)` == True；`LocalProcess.kill` 签名 `(self, sig: int | None = None)`

## 5. 给下游的注意事项

- **容器模式 env 语义**：`build_exec_process` 传给容器的 env = 调用方 env（即 13 驱动的 `WorkerCommand.env`）+ 捕获注入；**不含宿主 os.environ**（容器不继承宿主环境）。local 模式才 `{**os.environ, **env}`。
- **C5 守卫**：若 env 里出现 `CAIRN_API_TOKEN`/`security.api_token_env`/`capture_token_env`/`CAIRN_SERVER*` → `ContainerBackendError`（fail-loud，不静默过滤）。这是防御纵深；正常路径这些 key 不会被驱动放进 env。
- **`build_exec_process` 无 project_id**：容器模式靠「先 `ensure_running(project_id)`（设线程本地 current_project），后 `build_exec_process`」路由。跨线程混用 project 会错路由——30 保证同一 task 线程内先 ensure 再 exec。
- **`write_text_file` 路径语义**：rel_path 可为相对或容器绝对（`/home/worker/workspace/...` 前缀会被剥）；任何 `.`/`..` 段直接 `ValueError`；`/etc/...` 等非 workspace 绝对路径按「相对 workspace」解释，绝不逃逸（graph §4-15）。
- **证据卷按 engagement 作用域（B7）**：挂载是 `evidence_root/{engagement_id}`，不是 project_id；多 project 同 engagement 共享同一证据目录。`scope_resolver` 必须返回 `engagement_id`。
- **网络模式**：`container.network_mode` 默认 bridge（capture 模式必须 bridge，C12）；host 仅显式配置，文档标注「网络层无兜底」（v2 §2.5）。
- **PID/容器内 kill**：`docker exec` 客户端被 SIGKILL 时 docker daemon 会终止容器内 exec 进程；额外在已知容器 PID 时发 `docker exec NAME kill -<sig> <pid>` 兜底。C1 kill switch 走 `proc.kill(SIGKILL)` 即时路径（不经 SIGTERM→grace）。
- **uid 1000 权限**：容器 worker=uid 1000。`ensure_running` 会 best-effort chown(1000)/chmod bind source（evidence 0700、workspace 0770）；Dispatcher 非 root 时退化为 chmod。宿主目录归属不一致时容器内写入可能失败——部署时确认 dispatcher 能 chown 或目录预置。
- 未 git commit（按编排要求）。
