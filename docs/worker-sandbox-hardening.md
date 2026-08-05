# Worker 沙箱镜像加固方案（精简渗透 Worker 镜像）

> 配套：`architecture-research-report-pentest-v2.md` §2.5 / §8.5 / §10.3、`database-ddl-draft.md`
> 目标：替换原 Kali 重型渗透镜像，改为**最小化、非 root、无 sudo、无 docker、资源受限**的专用 Worker 沙箱；"能打什么"由 Engagement 授权 + 覆盖矩阵决定，"怎么安全地打"由镜像基线保证
> 原则：**镜像只提供工具与 CLI，不提供提权面**；Agent 越权风险由容器层（cap/user/网络）兜底，不依赖 Agent 自觉

---

## 1. 设计目标与风险模型

| 目标 | 手段 |
|---|---|
| 最小攻击面 | 精简基础镜像 + 白名单工具，避免 Kali 全家桶 |
| 提权隔离 | 非 root 用户、无 sudo、无 setuid 工具、无 docker CLI |
| 资源失控兜底 | `mem_limit` / `cpu_quota` / `pids_limit` / 磁盘配额 |
| 网络越权兜底 | 默认 `cap_drop=ALL` + 按需 add；可选 egress 代理白名单 |
| 证据留存 | 工作区只读以外的证据目录可写，统一 `evidence_root` 挂载 |
| 密钥隔离 | LLM 凭证只注入环境变量，不落盘镜像 |

## 2. 基础镜像选型

- **首选**：`debian:bookworm-slim`（~70MB）或 `python:3.13-slim`（含 pip/标准工具）
- 弃用：`kalilinux/kali-rolling`（镜像膨胀、含大量武器化载荷、默认 root + 可 sudo）

## 3. Dockerfile 草案

```dockerfile
# syntax=docker/dockerfile:1
FROM debian:bookworm-slim

# ── 工具集（白名单，按需裁剪；安装后清理缓存）────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl wget unzip zip jq \
        iputils-ping dnsutils netcat-openbsd \
        nmap sqlite3 python3 python3-pip \
        ripgrep fd-find git openssh-client \
        # Agent CLI 运行时（claude/codex/pi 均为 Node CLI，必须装 node+npm）
        nodejs npm \
        # 常用语言运行时（Agent 可能执行 PoC/脚本）
        python3-requests && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ── 非 root 运行用户（去 sudo）─────────────────────────────────────
RUN useradd -m -s /bin/bash worker
# 不加入 sudo 组；不写 /etc/sudoers

# ── 按 Engagement 授权的补充工具（可挂载卷覆盖，避免镜像膨胀）────────
# 例如：nuclei/katana/dalfox 等，仅在 scope_policy.tools 声明时经卷挂载到 /opt/tools
# 镜像默认只含上面最小集；重型工具走「按需挂载」而非打进镜像
# 挂载源：宿主机 tools_root/ 目录由运维维护（见 ops-runbook §5）；二进制须有版本/sha256 清单，
# 挂载只读（ro）；Java 工具（如需）必须一并提供 trustStore 注入（C6），否则其流量视为 unverified

# ── Agent CLI（保留原接入方式）─────────────────────────────────────
# 说明：CLI 以 Node 包安装，非 root 用户可执行；凭证走环境变量
RUN npm install -g @anthropic-ai/claude-code@2.1.98 \
               @openai/codex@0.118.0 \
               @mariozechner/pi-coding-agent@0.73.0 && \
    npm cache clean --force

# ── 工作区与证据目录──────────────────────────────────────────────
RUN mkdir -p /home/worker/workspace /home/worker/evidence && \
    chown -R worker:worker /home/worker
WORKDIR /home/worker/workspace
USER worker

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# read_only 根下 $HOME 必须指向可写卷：claude/codex/pi 会在 ~/.claude、~/.codex 写配置/会话，
# 指向 workspace 卷（B6）保证运行时不因只读根而崩溃
ENV HOME=/home/worker/workspace
```

## 4. 容器运行时加固（Dispatcher 侧 containers.py）

```python
def run_worker_container(self, project_id: str, *, engagement_tools: list[str] | None = None):
    caps = []                                        # 默认全 drop
    if scope_policy["network_cap"]:                  # 仅授权时放开
        caps = ["NET_RAW", "NET_ADMIN"]
    self._client.containers.run(
        self._config.image,
        ["sleep", "infinity"],
        detach=True,
        name=self.container_name(project_id),
        network_mode=self._config.network_mode,
        user="worker:worker",                        # 非 root
        read_only=True,                              # 根只读（两个卷可写）
        tmpfs={"/tmp": "size=512m"},                 # 临时盘限容
        cap_drop=["ALL"],
        cap_add=caps or None,
        security_opt=["no-new-privileges"],
        mem_limit=scope_policy["resources"]["mem_limit"] or "2g",
        cpu_quota=scope_policy["resources"]["cpu_quota"] or 100_000,
        pids_limit=scope_policy["resources"]["pids_limit"] or 512,
        ulimits=[{"Name": "nofile", "Soft": 1024, "Hard": 2048}],
        volumes={
            # B6：workspace 必须持久卷（recon 结果/session 状态跨重启保留；
            #      read_only 根下唯一可写工作区 + $HOME，Agent 需要它落文件/CLI 配置）
            f"{workspace_root}/{project_id}": {"bind": "/home/worker/workspace", "mode": "rw"},
            # B7：证据目录，容器内 /home/worker/evidence ↔ 服务端 evidence_root/{engagement_id}/
            # 注意：证据按 engagement 作用域挂载（多 project/engagement 共享同一证据目录），不用 project_id
            f"{evidence_root}/{engagement_id}": {"bind": "/home/worker/evidence", "mode": "rw"},
            f"{tools_root}": {"bind": "/opt/tools", "mode": "ro"},   # 按需挂载工具
        },
        labels={"cairn.project": project_id, "cairn.engagement": engagement_id},
    )
```

### 4.1 捕获代理注入（透明旁路捕获 · F5 白名单）

Worker 出网流量经捕获代理（mitmproxy，每 Engagement 一个）旁路截取，**HTTP(S) 全量记录**。

> **网络模式（C12 前置）**：capture 模式必须用 **bridge 网络 + 每 worker 独立 IP**，否则 `client_ip` 归属反查（C12）失效、verify 全部降级 needs_more；host 网络仅限 local/演练场景（见 v2 §2.5）。

```python
env = {
    "HTTPS_PROXY": f"http://{capture_host}:{scope_policy['capture_proxy']['port']}",
    "HTTP_PROXY":  f"http://{capture_host}:{scope_policy['capture_proxy']['port']}",
    # CA 信任：让 CLI 及其底层 SDK（requests/Node/Go）信任本 Engagement CA
    "SSL_CERT_FILE":  "/etc/cairn-capture/ca.pem",
    "REQUESTS_CA_BUNDLE": "/etc/cairn-capture/ca.pem",
    "NODE_EXTRA_CA_CERTS": "/etc/cairn-capture/ca.pem",
}
# F5 fail-closed 白名单：记录 ⇔ (host ∈ allow_capture_hosts) 且 (host ∉ no_capture_hosts)。
# allow_capture_hosts 激活时由 targets 派生（仅 authorized 目标）；白名单之外透传不落盘。
# NO_PROXY 仅作性能双保险，不是安全边界——即使工具忽略它，代理端白名单也会拦截。
env["NO_PROXY"] = ",".join(scope_policy["capture_proxy"]["no_capture_hosts"])
```

**CA 信任语言级差异（C6）**——同一环境变量对不同的语言/工具效果不同，信任不到位 → 该流量**不会**被代理解密记录（将视为未走代理、unverified）：

| 语言/工具 | 信任方式 | 说明 |
|---|---|---|
| curl / libcurl | `SSL_CERT_FILE` ✅ | 环境变量即生效 |
| Python requests / urllib | `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE` ✅ | 环境变量即生效 |
| Go（net/http） | `SSL_CERT_FILE` ✅ | Go 尊重该环境变量 |
| Node.js / 基于 Node 的 CLI（claude-code/codex/pi） | `NODE_EXTRA_CA_CERTS` ✅ | 只认这个，不读系统 CA |
| Java（`java -jar` 工具、Burp 插件类） | ❌ 忽略 env | 需 `-Djavax.net.ssl.trustStore=...` 导入 CA，或把 CA 合并进 bundled cacerts |
| 静态编译/自实现 TLS | 视实现而定 | 可能完全不信任自定义 CA → 该目标标记 `no_mitm`，降级命令证据 |

> 降级规则：若某工具产生 `no_mitm`/`unverified` 流量（无法解密），其证据通道自动回退到命令回显 + 人工确认，报告标注"可能未走代理"（capture-verify-progress-spec §9）。

> 非 HTTP（SSH/MySQL 等原始 TCP）：容器内按 `scope_policy.record_pcap` 启动 tcpdump，pcap 落证据目录；协议证据以 Agent 上报的**命令 + 回显**为准（见 capture-verify-progress-spec §3）。

### 4.2 Agent 凭证与 Cairn API token 策略（C5）

Server 鉴权是"**单 Bearer Token + 业务规则**"（T/H 同一 token）。若该 token 注入 Agent 容器，Agent 即可直调 `finalize`、finding 状态升级等**仅人工**接口，把授权约束架空。因此凭证按最小化分配：

| 凭证 | 注入对象 | 说明 |
|---|---|---|
| LLM CLI 凭证（ANTHROPIC_API_KEY 等） | Agent 容器 | 必要；环境变量注入，不落镜像层 |
| **Cairn API token** | **仅 Dispatcher + 捕获代理（受限写 token）** | **Agent 容器绝不注入** |
| 流量 digest 读取 | Agent ← Dispatcher prompt 注入 | verify 盲审读 digest 由 Dispatcher 渲染进 prompt，容器内无 Server 地址/凭证 |
| Cairn Server 可达性 | Agent 容器 **不可达** | 容器网络不配置 Server 路由/端口，纵深防御 |

> 例外路径（如需 agent 直接查询捕获索引）必须走**受限只读 token + 最小 scope**，并纳入审计；默认禁止。

### 4.3 熔断即时性（C1）

- kill switch 触发时 Dispatcher/executor 对 Agent 进程执行 **`SIGKILL`（即时，不走 SIGTERM→grace→SIGKILL 的普通取消路径）**——interval + grace 期间 Agent 仍在向目标发包，对渗透作业不可接受；
- 同步停止捕获代理与 tcpdump（capture spec C3）；
- 容器资源上限（mem/cpu/pids）确保被 SIGKILL 兜底前不拖垮宿主；
- 基线检查清单 §5 增加校验：kill 后 1 秒内 Agent 进程组被清除、容器内无残留扫描进程。

## 5. 安全基线检查清单

| # | 项 | 检查 |
|---|---|---|
| 1 | 非 root | 容器内 `id -u != 0`；无 `sudo`/`su` setuid |
| 2 | 无提权面 | `find / -perm -4000 -type f` 为空（或仅白名单必需） |
| 3 | 无 docker 通道 | 镜像内无 `/var/run/docker.sock`、无 docker CLI |
| 4 | 根只读 | 除 workspace/evidence/tmp 外不可写 |
| 5 | 全 drop cap | `capsh --print` 无 NET_RAW/NET_ADMIN（除非授权） |
| 6 | no-new-privileges | `security_opt` 已设 |
| 7 | 资源限制 | mem/cpu/pids 均有上限 |
| 8 | 凭证 | LLM key 仅环境变量，镜像层无 `ENV KEY=` |
| 9 | 工具白名单 | `/opt/tools` 只读挂载，按 engagement 生效 |
| 10 | egress 可选 | scope_policy.egress_proxy 非空时注入 `HTTPS_PROXY`（网络层范围兜底） |

## 6. 可选：独立 executor 侧车（进一步收敛 docker.sock 风险）

> 原方案 Dispatcher 裸挂 docker.sock。加固优先级：
> - **P0（内部工具必做）**：非 root 容器 + 全 drop cap + 资源限制 + 镜像精简（本文件 3/4/5）
> - **P1（推荐）**：Dispatcher 通过 **HTTP Docker API over TLS + 客户端证书** 或 **独立 executor 服务**（唯一持有 docker.sock 的进程，暴露受限接口：`create/exec/attach/logs/stop/remove`，且按 project 标签授权）
> - **P2（可选）**：executor 侧车运行在独立安全域，Dispatcher 只能调 executor，无法直达 docker daemon

## 7. docker-compose 改造示例

```yaml
services:
  cairn-server:
    build: .
    image: cairn-app
    ports: ["8000:8000"]
    environment:
      - CAIRN_API_TOKEN=${CAIRN_API_TOKEN}        # 环境注入，不入仓库
    volumes:
      - ./datas/cairn:/root/.local/share/cairn/
      - ./datas/evidence:/var/cairn/evidence
    healthcheck: { test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/projects',timeout=5)"], interval: 10s, timeout: 5s, retries: 12 }
    restart: unless-stopped

  cairn-executor:                                  # P1：唯一持 docker.sock 的进程（可选）
    image: cairn-executor
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./datas/evidence:/var/cairn/evidence
      - ./datas/workspace:/var/cairn/workspace     # B2：每项目持久工作区
    restart: unless-stopped

  cairn-dispatcher:
    build: .
    image: cairn-app
    environment:
      - CAIRN_API_TOKEN=${CAIRN_API_TOKEN}
      - CAIRN_EXECUTOR_URL=http://cairn-executor:9000   # 不再直连 docker.sock
    depends_on:
      cairn-server: { condition: service_healthy }
      cairn-executor: { condition: service_started }
    volumes:
      - ./dispatch.yaml:/cairn/dispatch.yaml
    restart: unless-stopped
```

## 8. 验收

1. 镜像 `docker scout`/`trivy` 扫描：无 HIGH+ 已知漏洞、无 sudo/docker 二进制。
2. 运行态 `docker exec <c> id` → `uid=1000(worker)`；`sudo -l` → 报错。
3. 资源攻击：`fork bomb`/内存耗尽被 pids_limit/mem_limit 兜住，不拖垮宿主。
4. 未授权 Engagement 的容器 `capsh --print` 无 NET_RAW；授权后可见。
5. 凭证不出现在 `docker history` 镜像层。
