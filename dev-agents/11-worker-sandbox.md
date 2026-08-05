# Agent 11 — Worker 沙箱镜像与容器运行时

> 阶段 0 · 可与 10/12 并行。**独立于 Server**，交付容器层 + 镜像构建 + 执行后端加固。

## 0. 开工前必读（按顺序）
1. `CLAUDE.md`（黄金不变量 2/9）
2. `docs/rule-registry.md`（C1/C5/C6/C12/B6/B7 等容器相关）
3. `docs/worker-sandbox-hardening.md` —— **全文**（你的规格就是它）
4. `docs/architecture-research-report-pentest-v2.md` §2.5、§4.2、§8.4、§8.5、§10.3
5. `docs/capture-verify-progress-spec.md` §2.4、§3、§9（CA 注入、no_mitm 降级、at-rest）
6. `docs/dispatch-config-spec.md` §8（container 段语义）、§5（security 路径）

## 1. 交付范围
```
container/Dockerfile            # 按 worker-sandbox-hardening §3 重写精简 Worker 镜像（debian:bookworm-slim、非 root、白名单工具）
container/AGENTS.md             # 按 prompts-pentest-templates §10 模板（占位符保留，Dispatcher 渲染时注入）
container/entrypoint.sh         # （如需）容器内启动钩子
cairn/src/cairn/dispatcher/runtime/containers.py   # 运行时加固（§4）：cap_drop=ALL / user / read_only / mem/cpu/pids / labels / 卷挂载（实现 13 的 ExecutionBackend 协议）
cairn/src/cairn/dispatcher/runtime/local_backend.py # local 模式保留（授权环境）（实现 13 的 ExecutionBackend 协议）
cairn/src/cairn/dispatcher/runtime/process.py       # stdout/stderr 分片 drain（配合进度包 24 的 task_events）（ExecProcess 实现）
cairn/tests/test_container_archives.py + test_local_execution.py   # 沿用并加固
docker-compose.yaml             # 升级为 worker-sandbox-hardening §7 结构（可含可选 executor 侧车）
```

## 2. 必须满足的契约
- **镜像**：非 root（`worker`）、无 sudo、无 docker CLI、无 setuid、`ENV HOME=/home/worker/workspace`（read_only 根下 CLI 配置可写）；`nodejs npm` 必须装（Agent CLI 为 Node 包）；`npm install -g claude/codex/pi` 装进镜像。
- **挂载**（§4）：workspace 卷 `{workspace_root}/{project_id}` ↔ `/home/worker/workspace`（rw）；**证据按 engagement 作用域** `{evidence_root}/{engagement_id}` ↔ `/home/worker/evidence`（rw，不是 project_id！）；`{tools_root}` ↔ `/opt/tools`（ro，按 engagement 授权挂载）。
- **捕获注入**（§4.1）：`HTTPS_PROXY/HTTP_PROXY/SSL_CERT_FILE/REQUESTS_CA_BUNDLE/NODE_EXTRA_CA_CERTS` 环境变量注入；`NO_PROXY`=no_capture_hosts；CA 由 Dispatcher 生成并挂载（`/etc/cairn-capture/ca.pem`）。**capture 模式必须 bridge 网络**（C12），host 网络仅 local 且标注无兜底。
- **运行时加固**：`cap_drop=["ALL"]` + 按 `scope_policy.network_cap` 加 `NET_RAW/NET_ADMIN`；`security_opt=["no-new-privileges"]`；`mem_limit/cpu_quota/pids_limit` 取 scope_policy.resources；`tmpfs /tmp 512m`；labels `cairn.project/cairn.engagement`。
- **熔断即时性**（C1）：kill switch → 对 Agent 进程**直接 SIGKILL**（不经 SIGTERM→grace），并停捕获代理/tcpdump（proxy 停止由 capture 包 23 负责，你只保证容器/进程可被即时杀）。
- **孤儿清理**：`cleanup_orphan`/`managed_container_names` 接线（v2 §8.2 要求，原死代码修复）。
- **local 模式**：`local_backend` 保留（`runtime.execution: local`），仅授权环境；无 sandbox 声明。

## 3. 验收标准（可执行）
1. `docker build -f container/Dockerfile .` 成功；`docker scout`/`trivy` 无 HIGH+ 已知漏洞（跑不了就记录，不阻塞）。
2. 运行态冒烟：`docker run --user worker --cap-drop ALL <img> id -u` → 1000；`sudo -l` 失败；`find / -perm -4000 -type f` 无 setuid（白名单外）。
3. `pytest test_container_archives.py test_local_execution.py` 通过。
4. SIGKILL 冒烟：起容器内 sleep，`kill -9` 后 1s 内进程组被清（基线清单 §5 第 4 项）。
5. compose 文件与 worker-sandbox-hardening §7 一致；dispatcher 不再裸挂 docker.sock（或走 executor）。

## 4. 硬约束
- **不在镜像内注入 Cairn token / LLM key**（C5/§4.2）；LLM key 走环境变量运行时注入。
- **`ExecutionBackend`/`ExecProcess` 协议由 13-dispatcher-runtime 定义**：你的 containers/local_backend/process 是实现方，先读 13 的交接物/协议签名对齐，不要自行发明抽象（见 `dev-agents/13-dispatcher-runtime.md`）。
- **不实现捕获代理本身**（那是 23-capture 的 mitmproxy 编排）；你只负责容器侧环境注入与代理 CA 的信任链。
- 改动 `dispatch-config-spec.md` 的 container/security 段字段前先列 diff。

## 5. 交接物
写 `dev-agents/notes/11-worker-sandbox.md`：镜像构建结果、容器运行参数签名、local 模式行为、compose 变更、未做项。
