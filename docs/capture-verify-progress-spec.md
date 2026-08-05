# 流量捕获 / 独立复核 / 进度监控 实现规格（Capture · Verify · Progress）

> 配套：`architecture-research-report-pentest-v2.md`、`coverage-engine-implementation-spec.md`、`worker-sandbox-hardening.md`
> 用途：把「证据真相源（透明代理抓包）+ 独立复核 Agent + 全量留存 + 逐步进度可视化」落地为可实现的模块
> 三条主线：① 流量级证据捕获（HTTP 代理 + 非 HTTP tcpdump）② 独立 verify 复核 Agent ③ 每步进度监控

---

## 1. 总体架构

```
Worker 沙箱容器（每项目一个）
  ├─ 业务流量（目标 HTTP(S)）──► 捕获代理（mitmproxy，按 Engagement 独立，fail-closed 白名单）
  │        │ MITM 解密 → 全量原始请求/响应 → traffic/ 文件 + 走 API 回写 traffic_entries 索引
  ├─ 业务流量（目标非 HTTP TCP）──► tcpdump 抓包 → pcap 文件（按 target 过滤）
  ├─ 控制面/LLM 流量 ──► 白名单之外一律不落盘（fail-closed，绝不抓取模型密钥与对话）
  └─ 命令证据：Agent 主动上报 {command, cwd, exit_code, stdout, stderr} → finding_command_evidence

Dispatcher
  ├─ 发现任务 explore/bootstrap ──► 输出 finding（引用 traffic + 命令证据 + agent_severity）
  ├─ 复核任务 verify（独立 Worker，≠ 发现者）──► 两阶段盲审：先读 digest 独立观察 → 再对照 claim 出 verdict
  ├─ 重放复核 replay（确定性）──► 重放原始触发包 + payload 变体 → 响应签名比对（复测多确认）
  ├─ 覆盖审计 audit（抽样）──► 独立重测高优先覆盖项 → 覆盖自报抽查
  └─ 进度流：CLI 结构化输出/stdout ──► task_events 文件 + 索引 → SSE → 前端实时面板

Server（唯一 DB 写者）
  ├─ traffic 索引 API：代理只写文件、调 API 回写元数据（真相源单写者）
  ├─ traffic 查询/还原 API：全量（报告/审计）或 digest（模型，≤digest_budget）
  ├─ finding 状态机：open → pending_verify → verified / false_positive / needs_more_evidence
  └─ 报告：请求/响应包（以捕获为准派生）+ 命令回显 + 复核/重放记录 原样呈现
```

> **数据流关键约束（F8）**：捕获代理（mitmproxy）是独立进程，**只写流量文件**，索引通过 `POST /engagements/{id}/traffic` 回写 Server——Server 是唯一 DB 写者；代理持受限 token（仅 traffic 写入权限），不触碰 findings/coverage 等写接口。

## 2. 透明代理捕获（HTTP(S)）

### 2.1 组件

| 组件 | 说明 |
|---|---|
| 捕获代理 | mitmproxy（每 Engagement 一个实例，监听代理端口，注入 `scope_policy.capture_proxy`） |
| 专属 CA | 每 Engagement 生成一份 CA；容器内注入 `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`，让 Worker 及其 CLI 信任 |
| 全量存储 | 每个会话一个文件 `traffic/{engagement_id}/{ts}_{seq}.req` / `.resp`（**全量、不截断**）；DB 索引 |
| 豁免清单 | `no_capture_hosts`：LLM API 域名、健康检查、Cairn Server、目标外出口 —— 命中直接透传不记录（**保护自身密钥与对话**） |

### 2.2 scope_policy 扩展（F5 白名单 / F2 digest / F1·F6·F7 verify_policy）

```json
"capture_proxy": {
  "enabled": true,
  "port": 8080,
  "ca": "engagement-specific",
  "allow_capture_hosts": [],                // F5：白名单——仅这些 authorized 主机记录流量；激活时由 targets 派生
  "no_capture_hosts": ["api.anthropic.com", "api.deepseek.com", "cairn-server"],
                                            // 次级排除：白名单命中后仍可显式豁免（控制面双保险）
  "record_pcap": true,
  "capture_quota": "10GB",                  // C4：全量配额，超限归档不删除
  "digest_budget": 8192                     // F2：给模型的 digest 字节上限/会话（全量另存文件）
},
"verify_policy": {
  "max_reverify": 3,                        // F6：needs_more_evidence 循环上限，超限升级人工
  "require_two_workers": true,              // F7：推荐 2 worker 基线（单 worker 降级 cross_run）
  "verify_model": "deepseek-v4",            // F1：可选跨模型复核（硬独立性）
  "require_blind_stage": true               // F1：盲审先于对照（防锚定）
}
```

**捕获判定（fail-closed，F5）**：

```
log ⇔ (host ∈ allow_capture_hosts) AND (host ∉ no_capture_hosts)
```

不满足白名单 → **透传且不落盘**（默认安全）。LLM API / Cairn Server / 健康检查天然不在白名单——即使某工具忽略 `NO_PROXY` 也不可能被记录。

**白名单热刷新（C11）**——`allow_capture_hosts` 激活时由 `targets`（authorized）派生，**必须随 targets 增删即时刷新**：

- 代理进程持有可刷新白名单（内存加载 + 订阅 Server 的 targets 变更，或每轮调度拉 `GET /engagements/{id}/targets`，间隔 ≤ runtime.interval）；
- **bootstrap/recon 发现新资产**（auto_created target，已过 scope 校验）→ 先加入捕获白名单，**再**播种覆盖项/派发 explore——保证对新资产的探测流量一开始就有证据；
- **kill/归档/暂停** → `allow_capture_hosts` 置空（同 C3 kill 即停）；
- 时序竞态兜底：白名单未刷新时新资产流量不落盘（fail-closed 默认安全，损失的是证据而非越权），探索任务会标记 `capture_gap`（见 C2 对账）并提示补测。

### 2.3 数据模型

```sql
CREATE TABLE IF NOT EXISTS traffic_entries (
    id             TEXT PRIMARY KEY,            -- 'tr-001'
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    captured_at    TEXT NOT NULL,               -- ISO8601 UTC
    method         TEXT NOT NULL,
    url            TEXT NOT NULL,               -- 完整 URL（含 query）
    host           TEXT,
    client         TEXT,                        -- worker 名（C12：由 client_ip 反查，无法区分时为 NULL）
    client_ip      TEXT,                        -- C12：来源容器 IP（每 worker 容器独立 IP，代理据此归属）
    status         INTEGER,
    req_path       TEXT NOT NULL,               -- traffic/.../xxx.req  全量请求文件
    resp_path      TEXT,                        -- traffic/.../xxx.resp 全量响应文件
    req_bytes      INTEGER NOT NULL,
    resp_bytes     INTEGER,
    content_type   TEXT,
    sha256         TEXT,                        -- F2：全量包校验和（分片拼接后计算）
    chunk_count    INTEGER NOT NULL DEFAULT 1,  -- F2：>100MB 分片数（xxx.req.0/1/2...）
    archived       INTEGER NOT NULL DEFAULT 0,  -- C4：已归档（finalize/archive 后压缩迁移）
    archived_path  TEXT,                        -- C4：归档位置（zstd 压缩，路径按 engagement 稳定）
    finding_linked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_traffic_eng_url ON traffic_entries(engagement_id, url);
CREATE INDEX IF NOT EXISTS idx_traffic_eng_time ON traffic_entries(engagement_id, captured_at);

CREATE TABLE IF NOT EXISTS finding_traffic_links (
    id          TEXT PRIMARY KEY,
    finding_id  TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    traffic_id  TEXT NOT NULL REFERENCES traffic_entries(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('trigger','related','verification','replay')),
    source      TEXT NOT NULL DEFAULT 'captured'
                CHECK (source IN ('captured','agent_typed')),   -- C2：以捕获为准派生
    created_at  TEXT NOT NULL,
    UNIQUE (finding_id, traffic_id, role)
);
CREATE INDEX IF NOT EXISTS idx_ftl_finding ON finding_traffic_links(finding_id);

> **C2 派生规则**：`source='captured'` 时，`finding_http_evidence` 的内容**由 traffic_entries 文件派生**（客户端实际发出的字节），agent 不逐字录入；`source='agent_typed'` 仅用于无捕获会话的降级（如 MITM 不可达），需命令证据佐证 + 复核/人工确认。
>
> **C12 归属规则**：`client` 由 `client_ip` 反查 worker——每 worker 容器独立 IP，代理按源 IP 映射到 worker 名。**host 网络（共享 IP）或多 worker 共用一个代理端口时无法区分来源**，此时 `client` 置 NULL 并在 `client_ip` 记录原始 IP；归属不明确的流量仍可被 finding 关联（按 host/url/时间窗），但 verify「读独立 worker 的流量」需以明确归属为前提，归属不明时降级为 `needs_more_evidence` 并提示补证。**推荐配置（必须）**：capture 模式下 worker 容器使用 bridge 网络（独立 IP），不用 host 网络。
```

### 2.4 与 finding 关联（三种路径）

1. **Agent 上报引用**：explore 输出 `findings[].traffic_ids: ["tr-001", ...]`；**候选 id 由 Dispatcher 派发前注入**——调用 `GET /engagements/{id}/traffic?client=<worker>&since=<intent_start>` 检索本 worker 时间窗内的捕获，渲染进 explore prompt；Agent 只从候选集引用，**不能自行查询捕获索引**（C5：Agent 容器不持 token、网络不可达 Server）。Dispatcher 校验 `traffic_entries.client` 归属后写 `finding_traffic_links(role='trigger', source='captured')`
2. **复核自动关联**：verify 输出 `verified_traffic_ids`，写入 `role='verification'`
3. **重放自动关联**（F4）：replay 引擎重放触发请求产出的新流量，写入 `role='replay'`（复测证据闭环）

> **C2**：agent 上报的 `http[]` 在 `source='captured'` 时仅作语义注释（"测了什么"），**报告/复核一律以 traffic 文件字节为准**；verify 阶段对 `http[]` 与捕获字节做比对，不一致 → 标记 `http_mismatch` 并按 needs_more_evidence 处理。

### 2.5 捕获完整性对账（C2 增强 · 防"一致地错"）

> 盲审复核与探索 Agent 共享同一份捕获：若代理**静默缺抓**（MITM 失败、工具忽略代理、TLS 指纹被拒），两者会一致地看不到真实请求。digest 的 sha256 只保证"截断一致"，不保证"抓全了"。需对账兜底：

1. **explore 写回对账**：explore 声明 `http[]` 或 `traffic_ids` 时，`capture.writer` 统计本次任务时间窗内**白名单命中主机的 traffic_entries 数**；若声明数 > 捕获数（阈值 `min_capture_ratio`，如 ≥2× 且差 ≥3）→ 标记 `capture_gap` 到 finding/coverage_record + 记录 error 事件「疑似缺抓」，verify 默认 `needs_more_evidence`，报告标注证据缺口。
2. **周期对账**：每轮调度对每 active engagement 抽查 `agent_declared_traffic`（findings.source_fact_id 关联 intent 的 http[] 计数）vs `traffic_entries` 计数，产出 `capture_gap` 看板项。
3. **会话内验证**：工具走代理时 mitmproxy 记录"本次连接是否被解密"（`unverified` 标记）；单任务内 `unverified` 流量占比超阈值 → 视为降级（命令证据 + 人工），同 C6/F10。
4. 对账计数落 `scheduler_state` / `traffic_entries.archived` 无关（对账在热数据上跑）。

## 3. 非 HTTP 捕获与协议边界（原始 TCP + 命令证据）

- **容器内 tcpdump**：按 `scope_policy.record_pcap` 启动，过滤 authorized 目标网段，pcap 落 `traffic/{engagement_id}/pcap/`；仅作原始字节留档（如 SSH banner、握手时序），不做协议解析
- **命令证据（非 HTTP 漏洞主证据）**：Agent 对 SSH 弱口令/配置类漏洞上报：

```json
"commands": [
  { "command": "sshpass -p 'admin' ssh admin@10.0.0.5", "cwd": "/home/worker/workspace",
    "exit_code": 0, "stdout": "Last login: ...", "stderr": "",
    "started_at": "...", "duration_ms": 1234 }
]
```

```sql
CREATE TABLE IF NOT EXISTS finding_command_evidence (
    id          TEXT PRIMARY KEY,               -- 'ce-001'
    finding_id  TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    command     TEXT NOT NULL,
    cwd         TEXT,
    exit_code   INTEGER,
    stdout      TEXT,                           -- 回显证据（全量）
    stderr      TEXT,
    started_at  TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fce_finding ON finding_command_evidence(finding_id);
```

> 校验：`command` 非空；`stdout/stderr` 大小上限默认 1MB（超限落文件引用 `evidence_refs`）；命令不得引用禁入目标。

### 3.1 协议覆盖边界与降级（F10）

| 协议 | 捕获方式 | 证据形态 | 边界/降级 |
|---|---|---|---|
| HTTP/1.1、HTTPS | mitmproxy（CA 信任后） | traffic 文件（req/resp） | TLS 固定（pinned）客户端**无法 MITM** → 该目标标记 `no_mitm`，走命令证据 + 人工；报告标注证据缺口 |
| HTTP/2 | mitmproxy 原生 | traffic 文件 | 同上（服务端仅 h2 时正常） |
| WebSocket | 握手会话 + 帧日志 | **连接级文件**（非单 req/resp） | F10：长连接不落 `traffic_entries` 单包模型；finding 证据 = 帧日志 + 命令证据 |
| gRPC | HTTP/2 捕获 | traffic 文件（protobuf 二进制） | digest 对模型呈现原始字节（hex/转义），不做协议解码 |
| SSH/MySQL/Redis 等原始 TCP | 容器内 tcpdump pcap | pcap + 命令回显 | pcap 仅字节留档，协议证据以命令回显为准 |
| 隧道/内网代理（ssh -L、socks5、socat 跳板） | 隧道外层 = 原始 TCP → tcpdump pcap；内层 HTTP 不可 MITM | 命令回显 + pcap 字节留档 | 内层流量不可见（等同 F10 降级）；finding 证据 = 隧道建立命令 + 回显 + 内层命令链记录，报告标注「经隧道，内层证据为命令级」 |

> **边界声明**：捕获是**尽力而为的证据真相源**，不是万能的。每个漏洞类有主证据通道（HTTP→捕获包；非 HTTP→命令回显）；抓不到/不适配的**明确降级到命令证据 + 人工确认**，绝不假装全量。

## 4. 独立复核 Agent（verify 任务）

### 4.1 触发与派发

- 新任务类型 `verify`（TaskType 扩展：`bootstrap|reason|explore|verify|audit`）
- **触发**：explore/bootstrap 产出 finding（status=open）后自动入队
- **独立 Worker 约束**：`_select_worker("verify")` 时**排除创建该 finding 的 worker**（保证独立性）
- **独立性级别（F1/F7）**：`cross_worker`（不同 worker）> `cross_model`（同 worker 不同模型池）> `cross_run`（单 worker 兜底：同一 worker 的新 run 冷读证据，最终仍需人工确认）> `human`
- **部署基线（F7）**：`verify_policy.require_two_workers=true` 时，启动校验要求 ≥2 个可复核 worker；单 worker 环境自动降级 `cross_run` 并标记降级独立性
- **C7 模型同源性上限（如实标注，不虚标独立性）**：`cross_worker` 若两个 worker 走同一模型家族（如都接 deepseek-v4），盲审与探索的**系统性偏置是共享的**，实际独立性弱于跨模型复核。因此：
  - `verify_policy.verify_model` 非空时 `independence=cross_model`（硬独立性，推荐关键漏洞/高危 finding 强制）；为空时 `cross_worker` 报告里标注「同模型族，独立性有限」；
  - 单 worker 环境（降级 `cross_run`）的价值主张退化为「自动预筛 + 人工最终确认」，应在 UI/报告明示，**不要宣称自动独立复核**；
  - 这是「成本 vs 独立性」的显式权衡：配置 2 个 worker 同一模型 ≠ 2 个独立审查者。部署基线建议至少 1 个复核 worker + 1 个人工签收点。
- **复核任务输入（F2）**：`{finding}`（title/severity/description/commands）+ `{traffic_digest}`（降采样 digest，≤digest_budget，见 §8）——**不投喂全量包**；全量留档供报告/审计

### 4.2 两阶段盲审（F1：防锚定）

一次 verify 任务 = **两次顺序模型调用**，先盲审后对照，共用同一任务 run：

| 阶段 | 模型输入 | 模型输出 |
|---|---|---|
| ① 盲审 blind | 仅 `{traffic_digest}` + `{scope}`（**不含 finding claim**） | `observations`：独立看到的漏洞 + 独立定级 |
| ② 对照 comparison | ① observations + `{finding}` + digest | `verdict` + `verified_severity` + `reason` |

- `confirmed` 要求 claim 是盲审观察的**子集或可解释差异**；claim 有观察之外的内容 → 追问或 needs_more_evidence
- `verify_policy.verify_model` 非空时，阶段 ②（或整任务）改用不同模型池 → `independence=cross_model`
- 落库 `verify_runs`：stage='blind'/'comparison' + independence + observations（报告可呈现"复核有多独立"）

### 4.3 复核输出契约（verify）

```json
{
  "accepted": true,
  "data": {
    "stage": "comparison",
    "observations": [{"vuln": "SQLi in /login", "severity": "high"}],
    "verdict": "confirmed",                    // confirmed | rejected | needs_more_evidence
    "verified_severity": "high",               // 复核后定级（可升降）
    "reason": "digest 回显 SQL 错误，与观察一致",
    "verified_traffic_ids": ["tr-001"],
    "suggested_action": "none"                 // 可选：retest_now / collect_evidence
  }
}
```

- `confirmed` → finding 置 `verified`，`verified_severity` 生效
- `rejected` → 置 `pending_false_positive`（**保留人工最终确认权**，规则引擎先落 pending，人工或二次确认后终态）
- `needs_more_evidence` → `reverify_count+1`；**若 > `verify_policy.max_reverify`（F6）→ 升级人工 `needs_review`，不再自动循环**；否则回 `open` + 自动派发"补充证据"explore（针对同覆盖项/同 URL），再入复核
- **verify 不改 finding 其他字段**，只写 verdict 相关；每次运行落一条 `verify_runs`

### 4.4 任务模型扩展

| 任务 | 触发 | 输入 | 输出 | 是否消耗覆盖项 |
|---|---|---|---|---|
| `verify` | finding=open | finding + traffic_digest（两阶段） | observations/verdict/severity/traffic_ids | 否（结果联动 findings） |

## 5. 发现状态机（扩展）

```
open(agent_severity) ──派发verify──▶ pending_verify
pending_verify ──confirm──▶ verified(verified_severity)
pending_verify ──reject──▶ pending_false_positive ──人工/二次确认──▶ false_positive
pending_verify ──needs_more──▶ reverify_count+1
  ├─ ≤max_reverify ──▶ open（+补证 explore 重新入队）
  └─ >max_reverify ──▶ needs_review（F6：升级人工，不再自动循环）
verified ──修复──▶ fixed
fixed ──自动复测（replay 确定性 + verify 复核，多源确认）──▶ closed / 重新 open
```

- `findings` 表扩展列：`agent_severity`、`verified_severity`、`verify_status`（none/pending/confirmed/rejected）、`reverify_count`（F6）、`retest_pass`（F4）
- 生效 severity：`verified_severity`（存在时）> `agent_severity`
- `status` 枚举含 `pending_verify` / `pending_false_positive` / `needs_review`（DDL 已同步）

## 6. 自动复测（多次确认 · 重放优先）

> F4：把「多次确认」从**单证据多读**改为**多源确认**——确定性重放 + LLM 复核 + 人工签收，杜绝"两个 verify 读同一份 post-fix 流量"的假多确认。

**时序（C10 明确）**——fixed 后三条确认通道**并行独立**产生证据，无先后依赖；人工签收通常最后：

```
fixed（人工标记）
  ├─▶ 立即：rebuild 覆盖项（retest_round+1，A5）+ 入队 replay（确定性引擎，秒级）
  ├─▶ 并行：入队 retest explore（AI 自动复测，新流量捕获）→ 完成后 verify 复核一次
  └─▶ 人工：任意时刻可尝试签收 closed，但受确认门槛约束（HTTP 类未过 replay → 403）
```

1. finding 置 `fixed`（人工）→ `findings.retest_round+1` + 重建对应覆盖项（复用原行）→ 入队重放/复测
2. **确定性重放 replay**（HTTP 类 finding）：
   - `replay_runs` 重放**原始触发请求全量字节** + payload 变体集，比对响应签名（status + 指纹）
   - `matched_original == 0` 且响应符合修复特征 → `result='remediated'` → 写入 `finding_retest_confirmations(kind='replay', retest_round=N)`
   - `matched_original > 0` → `result='unchanged'` → finding 回 `open`/`verified` + P0 告警
   - replay 请求**经捕获代理**发送 → replay 自身落 `role='replay'` 流量证据（证据闭环）
   - 非 HTTP 类（命令回显型）走**命令确定性重放**（F4 对应物，见 §6.1），不作「仅 verify + 人工」——否则非 HTTP 主证据（Agent 自报 stdout）无确定性通道，与「证据真相源」承诺相悖
3. retest explore（AI 自动）完成且新流量不触发 → verify 复核 `confirmed` → 写入 `kind='verify'`
4. **计数与门槛（C10 明确，账本见 `finding_retest_confirmations`）**：
   - `retest_pass = 当前 retest_round 下确认账本行数`（≤3：replay/verify/human 各至多 1）
   - 同轮同类型确认幂等（`UNIQUE(finding_id, retest_round, kind)`）——**重复触发不再 +1**；
   - `closed` 门槛：`retest_pass >= 2` **且**含 ≥2 种不同类型；HTTP 类必须含 `kind='replay'`（确定性重放），非 HTTP 类必须含**命令重放**确认（`kind='replay'`，同一确定性语义）；未过 replay 门槛人工 closed → 403（TV-31/TV-46）；
   - **任何通道确认"仍存在"**（replay unchanged / verify confirmed 仍触发 / 人工复测发现）→ 立即回 `verified`/`open` + P0 告警，`retest_pass` 归零并 `retest_round+1` 进入新轮，**不留假通过**；
   - 轮次递增时旧轮确认不继承（"上一轮通过过"不得当本轮证据）。
5. 人工终签收（`closed` 不可由 AI 直接写）

### 6.1 命令确定性重放（非 HTTP 类 · F4 对应物）

命令回显型 finding 的主证据是 Agent 自报 stdout（**可伪造**，无 MITM 兜底），复测不能只有「verify 重读同一份 stdout」这一单源。实现：

- **wrapper 执行**：Dispatcher 通过**受控执行器**（复用 replay-engine 通道，不经过 Agent 会话）在沙箱内重放原始 `command`，用 pty/`script` 包装捕获真实 stdout/stderr + sha256，写入 `finding_command_evidence`（或命令重放记录），杜绝 Agent 逐字造假；
- **门控**：仅 authorized 目标、仅 active/fixed 状态、受 kill switch 约束（C1 即时性）；
- **判定**：重放输出与原始回显强签名比对——仍成功（如弱口令仍可登录）→ 回 `open` + P0；命令失败/目标已修复 → `result='remediated'` → `finding_retest_confirmations(kind='replay')`；
- **报告**：以 wrapper 捕获的真实回显为准，标注「命令重放证据」，Agent 原始回显仅作参考。

## 7. 进度监控（每步可视化）

### 7.1 数据模型

```sql
> 权威 DDL 见 `database-ddl-draft.md` §9.5 —— 此处为对齐副本：**`project_id` 可空**（B2：verify/audit/replay 为 engagement 级任务，不挂 project），`engagement_id` 带级联 FK，task_type 含 `audit`/`replay`。

CREATE TABLE IF NOT EXISTS task_runs (
    id           TEXT PRIMARY KEY,              -- 'task-001'
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    project_id   TEXT,                          -- B2：可空（verify/audit/replay 不挂 project）
    task_type    TEXT NOT NULL,                 -- bootstrap/reason/explore/verify/audit/replay
    worker       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('queued','running','success','failed','cancelled','unhealthy','rejected')),
    started_at   TEXT,
    finished_at  TEXT,
    outcome_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_eng ON task_runs(engagement_id, task_type, status);

CREATE TABLE IF NOT EXISTS task_events (
    id          TEXT PRIMARY KEY,               -- 'ev-001'
    task_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('step','tool','command','output','status','error')),
    level       TEXT NOT NULL DEFAULT 'info',   -- debug/info/warn/error
    message     TEXT,                           -- 摘要（≤512B，原始流落文件）
    raw_path    TEXT,                           -- 原始 stdout/stderr 分片文件
    raw_offset  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_task_events_run ON task_events(task_run_id, seq);
```

### 7.2 流式采集（F9：结构化流优先）

- **首选：CLI 结构化输出**。驱动支持时用 `claude-code --output-format stream-json`、codex/pi 的对应 JSON/debug 输出 —— 事件**天然结构化**（step/tool/command/output 直接映射），进度面板信号可靠
- **兜底：自由文本分类**。无结构化输出时，`classify_line` 只对**控制面严格模式**分类（`$ ` 命令前缀 / 工具调用 JSON 行 / Dispatcher 注入 `⚑` 前缀 / stderr 流 / traceback）；其余非空行一律 `output`
- **F9 防噪声**：stdout 里含 "error"/"failed" 字样**不算 error**（scanner 输出常含）；仅 stderr 流或严格错误签名（traceback / `command not found` / exit≠0 标记）才产生 error 事件
- Dispatcher 的 `ManagedProcess/LocalProcess` 已有 stdout/stderr drain 线程 → 改造成**分片写文件**（`logs/{task_run_id}/{seq}.chunk`）+ 摘要行写 `task_events`
- Server 提供 `GET /tasks/{task_run_id}/events?after_seq=`（长轮询/SSE）→ 前端活动面板实时滚动
- 展示粒度：每行 = 一条 step/tool/command/output；Agent CLI 打印的工具调用即天然步骤
- **成本控制**：摘要入库（512B/行上限），原始流文件按 task 清理（保留最近 N 天，pcap 与 traffic 全量单独保留）

### 7.3 前端

> 完整前端视图设计（任务活动面板 + 事件流渲染 + SSE 接线 + 业务联动）见 `docs/frontend-progress-view-design.md`；此处只列要点：

- 每个运行中任务一个"活动行"：状态徽标 + 最近事件摘要 + 展开实时日志
- Engagement 看板顶部：各 worker 当前在做什么（最后一条事件）+ 覆盖率 + 进行中任务数
- 事件类型着色：step=蓝、tool=紫、command=琥珀、output=灰、error=红
- 实时通道：`POST /tasks/{id}/events/ticket` 一次性 ticket → SSE（after_seq 断点续传 + 15s 心跳）→ 长轮询降级
- 展开行才走 SSE，其余 2s 汇总轮询（浏览器连接数上限）
- verify running → Findings 面板「复核中」脉冲；confirmed → severity 徽标更新（双轨标注 `8.1→9.0`）

### 7.4 Engagement 统一时间线（D3）

> 把散落四处的证据流（图时间线 / task_events / findings 历史 / traffic 捕获 / 覆盖写回 / 报告版本）**串成一条可回放、可审计的 engagement 级时间轴**——报告"方法流程"章节与人工回溯都从这里聚合。为聚合视图，不加新表：各事件源已有时间戳，服务端按 ts 归并。

```python
# services/timeline.py（聚合只读，无写权限面）
def engagement_timeline(conn, eid, *, after_ts=None, limit=200) -> list[dict]:
    """归并六类事件源，按 ts 升序返回统一事件结构：
    {ts, source: graph|task|finding|traffic|coverage|report, kind, actor, summary, ref}
    每源取（ts, actor, 摘要, 关联 id），不跨源做归一化计算。"""
    rows = []
    rows += graph_events(conn, eid)        # fact/intent/hint 变化（facts.created_at / intents.concluded_at）
    rows += task_events(conn, eid)         # task_runs/task_events（已按 seq 分片）
    rows += finding_events(conn, eid)      # finding_history（状态流转 + actor）
    rows += traffic_events(conn, eid)      # traffic_entries（捕获时间点 + 关联 finding）
    rows += coverage_events(conn, eid)     # coverage_records / waivers / audit_runs
    rows += report_events(conn, eid)       # reports.created_at
    rows.sort(key=lambda r: r["ts"])
    return rows[:limit]
```

- API：`GET /engagements/{id}/timeline?after_ts=&limit=`（前端时间轴视图：按 source 着色、按类型过滤、点击跳转源详情）
- 报告「方法流程」章节 = 时间线渲染为有序步骤列表（谁在什么时候做了什么 → 产出什么 → 关联哪些流量/漏洞）
- 只读聚合，禁止直写；供审计导出（YAML/JSON）

## 8. 全量留存、降采样与超大包处理

> 用户要求：全量保留、不脱敏、不截断。**证据三层分离（F2）**：
> **全量文件（报告/审计/replay 用，不投喂模型）** → **digest（给模型的降采样，≤digest_budget）** → **DB 元数据**。

### 8.1 全量文件存储

| 规模 | 策略 |
|---|---|
| ≤ 1MB | 直接存 traffic 文件（全量，不截断） |
| > 1MB | 仍全量存文件；DB 索引不变；读取时按需流式 |
| 超大（>100MB） | 分片存储 `xxx.req.0/1/2...` + 记录分片数；**每分片 `.sha256` 侧车**，还原时校验并依序拼接（F2） |
| 校验 | `traffic_entries.sha256` = 拼接后全量包校验和；校验失败 → 标记 `traffic_corrupt`，模型走 digest，报告提示人工核对 |

### 8.2 给模型的 digest（F2）

`resolve_traffic(..., for_model=True)` 按 digest_budget 生成（每会话默认 ≤8KB）：

- 请求行 + 全部请求头 + 请求体前缀 2KB + 后缀 512B
- 响应 status + 响应头 + 响应体前缀 2KB + 后缀 512B
- 截断处标注 `... [truncated, sha256=<全量校验和>]` —— 模型可确认"我看到的与全量一致"而无需加载全量
- **digest 只用于 LLM 消费**；报告/审计/replay 永远读全量文件

### 8.3 归档分级（C4）

| 层级 | 触发 | 存储 | 语义 |
|---|---|---|---|
| Hot | engagement active | 本地磁盘（配额内） | 实时读写 |
| Archive | finalize/archive 时 | zstd 压缩迁移 `archive/{engagement_id}/`（或对象存储） | **不删除全量**；DB 索引路径稳定，报告再生透明读归档 |
| 销毁 | `DELETE engagement`（显式） | 清除 hot + archive | 全量保留直至显式销毁 |

- 磁盘保护：每 engagement 配额 `capture_quota`（默认 10GB）；超限**告警 + 滚动归档**，绝不截断流量
- 归档校验：归档块含 sha256 清单，恢复/报告读取时校验

**容量核算（D6，真实量级）**——全量不截断意味着配额必须按真实规模规划：

| 场景 | 单请求/响应量级 | 数量 | 体积 |
|---|---|---|---|
| 目录爆破（dirsearch/ffuf） | ~1-4KB | 10k-100k 请求 | 几十 MB ~ 几百 MB |
| Web 应用扫描（nuclei 默认模板集） | ~2-8KB | 数千 | 数十 MB |
| 大响应（文件下载/全文页面） | 100KB-10MB | 少量 | 可达 GB |
| 一次性大文件上传/下载测试 | 1-100MB | 数条 | 数百 MB |

→ 单个中大型 engagement 全量流量可到 **1-5GB**；含大文件测试或长周期（窗口数周）可突破 10GB。建议：`capture_quota` 按预期测试深度显式设置（默认 10GB 对目录爆破型偏紧），并在 planning 阶段让 `capture_proxy.capture_quota` 进入 scope_policy 由人工确认；超出即告警 + 滚动归档（归档加密见 §9.9），**绝不静默丢弃**。磁盘规划按「并发 engagement 数 × quota × 1.5（归档缓冲区）」预留。

## 9. 安全要点

1. **控制面 fail-closed（F5）**：捕获判定 `host ∈ allow_capture_hosts 且 ∉ no_capture_hosts`；白名单之外**透传不落盘**——LLM API/Server/健康检查即使工具忽略 NO_PROXY 也不可能被记录
2. **CA 管理**：CA 私钥仅 Dispatcher 持有，注入容器的只有证书；Engagement 结束即吊销
3. **verify 独立性（F1/F7）**：派发排除创建者；独立性分四级（cross_worker/cross_model/cross_run/human），单 worker 降级 cross_run 且**最终仍需人工确认**；报告呈现独立级别
4. **severity 双轨**：Agent 初判可写，生效值以复核为准；报告标注两者差异
5. **复测不可 AI 终态（F4）**：`closed` 仅人工；AI/重放只产生"pass/fail 证据"；replay 仅针对 authorized 目标、仅 active 状态、受 kill switch 约束
6. **进度流只读**：task_events 只增，前端只读，无写权限面
7. **kill 即停捕获（C3）**：熔断/归档时**同步停止** per-engagement 代理与 tcpdump（或将 allow_capture_hosts 置空），杜绝 kill 后仍持续抓包
8. **CA 信任语言级差异（C6）**：见 `worker-sandbox-hardening.md` §4.1（Java 需 trustStore、Go/Node/curl 走 env）——信任不到位的工具产生的流量**自动视为 unverified**，报告标注"可能未走代理"
9. **捕获数据 at-rest 保护（C13）**：fail-closed 只保护了 LLM key，但**捕获流量本身含目标系统的真实凭据/session/token**，等同敏感数据。要求：`evidence_root/` 与 `traffic/` 目录**静态加密或受限权限**（推荐 LUKS/encfs 加密卷，或文件级 age/openssl 加密归档；归档层强制加密）；Server 与 Dispatcher 进程外任何用户/进程不可读；仅报告/审计/replay 经授权通道读取。归档（C4）时对加密后的归档再压 zstd，目录权限 0700。
10. **捕获完整性对账（C2 增强）**：见 §2.5——静默缺抓时 `capture_gap` 标记 + verify 默认 needs_more + 报告标注，防止"两个 Agent 一致地错"。
11. **模型同源性（C7）**：`cross_worker` 同模型族时报告标注"独立性有限"；关键 finding 建议 `verify_model` 跨模型硬复核（见 §4.1）。

## 10. 验收要点

1. 透明代理能还原"客户端实际发出的原始请求"（method/url/头/体一字不差），超大包分片校验后可完整拼回（F2）
2. **fail-closed 生效**：白名单外主机流量（含 LLM API）不出现在 traffic 索引；模拟"工具忽略 NO_PROXY"仍不落盘（F5）
3. verify 派发 worker ≠ 创建 worker；独立性级别正确落库（cross_worker/cross_model/cross_run/human）；单 worker 降级 cross_run（F7）
4. **两阶段盲审生效**：盲审阶段模型未见过 claim；对比阶段确认"claim ⊆ 观察"逻辑正确（F1）
5. **digest 生效**：verify 输入 ≤digest_budget，截断处含 sha256 引用；报告仍读全量（F2）
6. **max_reverify 生效**：needs_more 超过上限 → needs_review 升级人工，无无限循环（F6）
7. **replay 确定性**：复测中原始触发包重放，matched_original=0 且响应符合修复特征 → remediated；仍触发 → 回 open+P0（F4）
8. finding 状态机全路径（open→verify→verified/false_positive/needs_more/needs_review）有测试覆盖 —— 见 `docs/verify-mock-test-spec.md`（mock 驱动 45 用例）
9. 进度面板实时可见 agent 每一步工具调用与命令回显 —— 前端视图见 `docs/frontend-progress-view-design.md`
10. 非 HTTP 漏洞有 command 证据 + 回显，HTTP 漏洞有请求/响应包（以捕获为准派生），报告两者原样呈现
11. **C12 归属**：bridge 网络下 traffic 可反查 worker（client 非空）；host 网络下 client=NULL 且归属不明流量 verify 降级 needs_more
12. **C11 热刷新**：targets 增删/auto_created 建 target 后代理白名单在 ≤1 个 interval 内生效；kill 后白名单置空
13. **C2 对账**：explore 声明 http[]/traffic_ids 数远超捕获数 → `capture_gap` 标记 + verify needs_more + 报告证据缺口标注；unverified 占比超阈 → 命令证据降级
14. **C7 同源性**：cross_worker 同模型族时独立性标注「有限」；verify_model 非空 → independence=cross_model
15. **C10 复测**：retest_round 归零/递增语义正确；同轮同类型确认幂等；HTTP 类无 replay 确认人工 closed 被 403 拦截
16. **C13 at-rest**：evidence/traffic 目录静态加密或 0700 受限权限；归档强制加密；Server/Dispatcher 外进程不可读
17. **D3 时间线**：`GET /engagements/{id}/timeline` 归并六源且按 ts 有序，报告"方法流程"章节可由其渲染
