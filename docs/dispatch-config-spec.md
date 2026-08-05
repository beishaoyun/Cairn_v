# Dispatcher 配置规格（dispatch.yaml · v2）

> 配套：`architecture-research-report-pentest-v2.md` §8.2/§11.2、`backend-module-skeleton.md` §1、`worker-sandbox-hardening.md` §7、`ops-runbook.md` §2
> 用途：定义 `dispatch.yaml`（Dispatcher 全局配置）的 **v2 完整 schema**，取代仓库根 v1 示例（`dispatch.example.yaml` / `dispatch_mock.yaml` / `dispatch.local.example.yaml` 已同步升级为 v2 格式）。
> 关键区分：**Dispatcher 全局配置（本文件）** vs **per-engagement `scope_policy`**（`database-ddl-draft.md` §2.1）。前者管"调度器进程级"设置，后者管"每个 Engagement 的授权/捕获/复核策略"，二者以 scope_policy 覆盖同名默认值。

---

## 0. 配置分层总览

```
dispatch.yaml（本规格，Dispatcher 进程级）
├── server        # Cairn Server 地址 + 唯一 Bearer Token（env 引用）
├── common_env    # 注入所有 worker 的共享环境变量（LLM 代理/CA 信任等）
├── runtime       # 调度节拍/并发/健康检查/prompt_group
├── tasks         # 各任务类型超时（含新增 verify/audit/replay）
├── security      # ══ 新增段：Dispatcher 全局安全（token/CA/存储/加密/executor）══
├── scope         # ══ 新增段：运行时 scope 守卫开关（Kill/窗口/范围）══
├── tuning        # ══ 新增段：原硬编码魔数收敛（v2 §11.2）══
├── container     # worker 容器运行参数（capture 模式必须 bridge）
└── workers       # 驱动列表（task_types 扩展 verify/audit；replay 是引擎不走 worker）
```

> 凭证规则（`ops-runbook.md` §2）：配置内一律 `${ENV_VAR}` 引用，仓库禁明文；`dispatch.yaml` / `*.env` 均在 `.gitignore`，示例文件只放占位符。启动时 `${VAR}` 展开。

---

## 1. `server`

```yaml
server:
  url: "http://cairn-server:8000"   # Cairn Server API 基址（本地模式用 http://127.0.0.1:8000）
  api_token: "${CAIRN_API_TOKEN}"   # ══ 唯一 Bearer Token（Dispatcher 调 Server 用，env 引用）══
```

- `api_token` 由 `${CAIRN_API_TOKEN}` 注入；与 Server 侧、`docker-compose` 注入的 `CAIRN_API_TOKEN` 同一值（单 token 语义，v2 §6.2）。
- **Agent 容器绝不注入此 token**（C5，`worker-sandbox-hardening.md` §4.2）——Agent 拿不到 Server 地址/凭据。
- 捕获代理（mitmproxy）持**受限写 token**（`security.capture_token_env`），仅可写 `POST /engagements/{id}/traffic`（F8）。

## 2. `common_env`

注入每个 worker 进程的环境变量（对容器与 local 均生效）：

```yaml
common_env:
  HTTPS_PROXY: "${CAPTURE_PROXY_URL}"   # 可选；capture 模式经代理，网络层范围兜底（scope_policy.egress_proxy 覆盖）
  NO_PROXY: "cairn-server,api.anthropic.com,api.deepseek.com"
```

- 不承载 LLM 凭证（每个 worker 在 `workers[].env` 自带）；不承载 Cairn token（C5）。
- 容器模式下代理/CA 信任由 Dispatcher 在运行时按 `scope_policy.capture_proxy` 注入（`worker-sandbox-hardening.md` §4.1），本段只放进程级常量。

## 3. `runtime`

```yaml
runtime:
  execution: "container"            # container（默认，每 project 一容器）| local（主机进程，仅授权环境）
  interval: 3                       # 调度主循环节拍（秒）
  max_workers: 8                    # 全局并发上限
  max_running_projects: 3           # 同时活跃调度 engagement 上限
  max_project_workers: 4            # 单 engagement worker 上限
  healthcheck_timeout: 20           # 单次 worker 健康检查超时
  worker_healthcheck: "startup_only"  # startup_and_task | startup_only | disabled
  prompt_group: "default"           # default（渗透场景）| mock（结构化回归）
```

> 健康检查已从「容器内 curl」改为「进程内执行」（见 `docs/` 与代码 healthcheck 演进），本字段只控制**时机**。

## 4. `tasks`（新增 verify/audit/replay）

```yaml
tasks:
  bootstrap:   { timeout: 300, conclude_timeout: 90 }   # 攻击面发现 + 播种 + 初探
  reason:      { timeout: 300, max_intents: 2 }         # 缺口记账员（coverage_item_ids/recommend_finalize）
  explore:     { timeout: 300, conclude_timeout: 90 }   # 覆盖项驱动 + findings + coverage_result
  verify:      { timeout: 300 }                         # ══ 新增：两阶段盲审单任务超时（capture spec §4）══
  audit:       { timeout: 300 }                         # ══ 新增：覆盖抽样复核（coverage spec §4 / F3）══
  replay:      { timeout: 60 }                          # ══ 新增：确定性重放引擎（F4；不走 LLM）══
```

- **`replay` 是确定性引擎任务**（`worker='replay-engine'`，`backend-module-skeleton.md` §3），**不是 worker task_type**——workers 列表无需声明，也不消耗 worker 并发。
- verify/audit 超时语义与 explore 一致（超时 → 同 session conclude 收尾；verify 无 conclude，超时按 needs_more_evidence 处理，见 capture spec §4.3）。

## 5. `security`（新增段 · Dispatcher 全局安全）

```yaml
security:
  api_token_env: "CAIRN_API_TOKEN"            # 唯一 Bearer Token 的 env 名（与 docker-compose 注入一致）
  capture_token_env: "CAIRN_CAPTURE_TOKEN"    # 捕获代理受限写 token（F8：仅 traffic 写入）
  capture_ca_dir: "/var/cairn/capture-ca"     # per-engagement CA 私钥存储（Dispatcher 持有，worker-sandbox §4.2/§4.1）
  evidence_root: "/var/cairn/evidence"        # 证据根（挂载进容器 /home/worker/evidence，B7）
  traffic_root: "/var/cairn/traffic"          # 流量全量文件根（F2/C4）
  archive_root: "/var/cairn/archive"          # 归档根（C4：zstd + 加密）
  static_encryption: true                     # evidence/traffic at-rest 加密或 0700 受限权限（C13）
  archive_encryption: true                    # 归档强制加密（C4/C13）
  executor_url: ""                            # 可选独立 executor 侧车（P1：唯一持 docker.sock，worker-sandbox §6/§7）
```

- 各 root 路径的宿主目录在 `docker-compose` 中由 volume 映射（见 `worker-sandbox-hardening.md` §7）。
- `static_encryption` 为 true 时实现应使用 LUKS/encfs 加密卷或文件级加密；对外行为是「Server/Dispatcher 进程外不可读」（C13 验收点 16）。

## 6. `scope`（新增段 · 运行时守卫开关）

```yaml
scope:
  enforce_scope_guard: true       # 目标白名单守卫（prohibited 命中 → 403 SCOPE_DENIED + 审计，禁止 fallback；v2 §8.2/§12 规则1）
  enforce_auth_window: true       # 授权窗口守卫（窗口外任务派发拒绝；窗口到期自动 pause；v2 §8.2/§4.2）
  enforce_kill_switch: true       # 全局 + engagement 级熔断守卫（423 KILL_SWITCH_ON；v2 §8.2）
  default_scope_policy: "{}"      # 创建 engagement 未显式给 scope_policy 时的默认模板（可与 tuning 里默认值叠加）
```

> 网络层兜底不在此段开关——`capture_proxy`/`egress_proxy` 属 per-engagement `scope_policy`（`database-ddl-draft.md` §2.1）；Dispatcher 只负责强制「capture 模式必须 bridge 网络」（见 §8）。

## 7. `tuning`（新增段 · 原硬编码魔数收敛）

> 所有值均有文档出处；scope_policy 同名项覆盖（如 `digest_budget`、`capture_quota`、`audit_sampling`、`reason_escalation` 在 scope_policy.coverage，**不在此段**）。

```yaml
tuning:
  writeback_retries: 1                  # 覆盖/漏洞写回失败退避重试次数（v2 §8.3「写失败退避 1 次再放弃」）
  reconcile_intent_timeout_multiplier: 2  # intent 超时判定：>2×interval 无心跳（v2 §8.2 启动 reconcile）
  min_capture_ratio: 2.0                # capture_gap 判定：声明数 ≥ 捕获数×此值（capture spec §2.5）
  min_capture_abs_diff: 3               # capture_gap 判定：且差 ≥ 此值（capture spec §2.5）
  event_summary_max_bytes: 512          # task_events.message 摘要上限（DDL §9.5）
  command_evidence_max_bytes: 1048576   # 命令回显 stdout/stderr 上限，超限落文件引用（capture spec §3）
  event_raw_retain_days: 7              # task_events 原始流文件保留天数（capture spec §7.2 / frontend §8）
  sse_heartbeat_seconds: 15             # SSE 心跳间隔（frontend §3.3）
  longpoll_hold_seconds: 20             # 长轮询服务端 hold 上限（frontend §3.3）
  worker_rejected_cooldown_seconds: 5   # 任务 rejected 后该 worker 冷却（v1 语义保留）
  worker_unhealthy_cooldown_seconds: 5  # worker unhealthy 冷却（v1 语义保留）
```

## 8. `container`

```yaml
container:
  image: "ghcr.io/oritera/cairn-worker-container:latest"   # 精简渗透 Worker 镜像（worker-sandbox-hardening §2/§3）
  network_mode: "bridge"        # ══ capture 模式必须 bridge（C12 归属反查前置，worker-sandbox §4.1）══
  completed_action: "stop"      # stop（保留现场）| remove（清理）
  cap_add: []                   # 默认全 drop；仅 scope_policy.network_cap 授权时加 NET_RAW/NET_ADMIN
```

- **`network_mode: "host"` 仅限 local/演练场景**，且该 Engagement 必须显式标注「网络层无兜底」（v2 §2.5/§4.2）——host 网络下 `client_ip` 归属反查失效，verify 全部降级 `needs_more_evidence`（C12）。
- `cap_add` 为运行时基线；真正放行由 per-engagement `scope_policy.network_cap` 决定（`worker-sandbox-hardening.md` §4）。

## 9. `workers`

```yaml
workers:
  - name: "claudecode_deepseek-v4-pro"     # 唯一标识（task_runs.worker / traffic_entries.client 取值）
    type: "claudecode"                     # claudecode | codex | pi | mock（+ 各 local 变体）
    task_types: [bootstrap, reason, explore, verify, audit]   # ══ v2：已扩展（含 verify/audit）══
    max_running: 2                         # 单 worker 并发
    priority: 0                            # 越小越优先
    verify_eligible: true                  # ══ 新增：可承担 verify 复核（派发排除 finding 创建者）══
    env:
      ANTHROPIC_MODEL: "deepseek-v4-pro"
      ANTHROPIC_BASE_URL: "https://api.deepseek.com/anthropic"
      ANTHROPIC_AUTH_TOKEN: "sk-xxx"       # 占位符
```

- **`task_types` 枚举 = `bootstrap|reason|explore|verify|audit`**（LLM 任务，worker 驱动）；`replay` 不在其中（引擎任务，§4）。
- **`verify_eligible`（默认 true）**：标记该 worker 可执行 verify。派发时除排除「创建该 finding 的 worker」（capture spec §4.1）外，还跳过 `verify_eligible=false` 的 worker。`verify_policy.require_two_workers=true` 时启动校验要求 ≥2 个 verify_eligible worker（单 worker 降级 cross_run，capture spec §4.1）。
- mock 驱动：`task_types` 可含 verify/audit，配合 `MOCK_VERIFY` / `MOCK_REPLAY` 等环境变量（见 `verify-mock-test-spec.md` §2）。

## 10. 配置一致性对照

| 主题 | 属于本文件（dispatch.yaml） | 属于 scope_policy（per-engagement） |
|---|---|---|
| Server 地址/Token | `server.*` | — |
| 捕获代理/CA/配额/digest | — | `scope_policy.capture_proxy.*`（DDL §2.1） |
| verify 循环上限/独立性 | `workers[].verify_eligible` | `scope_policy.verify_policy.*` |
| 收敛策略/抽样/空转升级 | — | `scope_policy.coverage.*`（coverage spec §2） |
| 网络兜底/能力 | `container.network_mode`（模式） | `scope_policy.network_cap` / `egress_proxy`（授权） |
| 魔数/重试/对账阈值 | `tuning.*` | scope_policy 同名键可覆盖 |
| 熔断/窗口/范围守卫 | `scope.*`（开关） | engagement 窗口与 targets（数据） |
