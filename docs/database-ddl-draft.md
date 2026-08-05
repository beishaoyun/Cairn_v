# 新平台数据库 DDL 草案（全量）

> 配套：`architecture-research-report-pentest-v2.md` §5、`coverage-engine-implementation-spec.md`
> 用途：新平台（渗透测试版）完整建库脚本与迁移思路 —— 覆盖 **探索图 + 授权范围 + 漏洞闭环 + 覆盖度 + 调度状态** 五大子域
> 存储引擎：SQLite（WAL / `foreign_keys=ON` / `busy_timeout=5000` / `synchronous=NORMAL`）

---

## 0. 连接与基础 PRAGMA

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;
```

## 1. 全局配置

```sql
CREATE TABLE IF NOT EXISTS settings (
    rowid            INTEGER PRIMARY KEY CHECK (rowid = 1),  -- 单例
    intent_timeout   INTEGER NOT NULL DEFAULT 15,
    reason_timeout   INTEGER NOT NULL DEFAULT 15,
    global_kill_switch INTEGER NOT NULL DEFAULT 0,           -- 全局熔断（1=所有项目停止派发）
    coverage_policy  TEXT NOT NULL DEFAULT '{}'              -- 收敛策略 JSON（见覆盖规格 §2）
);
INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout, global_kill_switch, coverage_policy)
VALUES (1, 15, 15, 0, '{}');
```

## 2. 授权范围子域（Engagement / Target）

```sql
CREATE TABLE IF NOT EXISTS engagements (
    id                  TEXT PRIMARY KEY,        -- 'eng_001'
    title               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'planning'
                        CHECK (status IN ('planning','active','paused','completed','archived')),
    authorized_start_at TEXT,                    -- 授权窗口（ISO8601 UTC）
    authorized_end_at   TEXT,
    scope_policy        TEXT NOT NULL DEFAULT '{}',  -- {targets_rules, coverage:{...}}
    kill_switch         INTEGER NOT NULL DEFAULT 0,
    created_by          TEXT NOT NULL DEFAULT 'human',
    created_at          TEXT NOT NULL,
    completed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_eng_status ON engagements(status);

CREATE TABLE IF NOT EXISTS targets (
    id             TEXT PRIMARY KEY,             -- 't-001'
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    value          TEXT NOT NULL,                -- 域名 / IP / CIDR / URL
    kind           TEXT NOT NULL CHECK (kind IN ('domain','ip','cidr','url','hostname')),
    scope_status   TEXT NOT NULL CHECK (scope_status IN ('authorized','prohibited')),
    criticality    REAL NOT NULL DEFAULT 0.5 CHECK (criticality BETWEEN 0 AND 1),
    auto_created   INTEGER NOT NULL DEFAULT 0,     -- 1=findings 写回时自动创建（已过 scope 校验）
    note           TEXT,
    added_by       TEXT NOT NULL DEFAULT 'human',
    added_at       TEXT NOT NULL,
    UNIQUE (engagement_id, value)
);
CREATE INDEX IF NOT EXISTS idx_targets_eng_scope ON targets(engagement_id, scope_status);
```

### 2.1 `scope_policy` JSON Schema（统一各文档引用）

```json
{
  "tools": ["nuclei", "nmap", "dalfox"],            // 工具白名单，仅这些工具可挂载进沙箱 /opt/tools
  "network_cap": false,                              // true 才授予 NET_RAW/NET_ADMIN（cap_add）
  "resources": {
    "mem_limit": "2g", "cpu_quota": 100000,          // ≈1 核
    "pids_limit": 512
  },
  "egress_proxy": "http://127.0.0.1:7897",           // 可选；非空时注入 HTTPS_PROXY（网络层范围兜底）
  "depth_default": "standard",                       // baseline | standard | deep
  "coverage": {                                      // 收敛策略（覆盖 settings 全局默认）
    "min_priority_threshold": 0.30,
    "target_coverage": 0.95,
    "require_all_findings_triaged": true,
    "require_depth": "standard",
    "auto_created_closure": {                        // F11：auto_created 目标不阻塞收敛
      "max_extra_depth": "baseline",                 // 自动目标覆盖项只需测到 baseline
      "excluded_from_report_ready": true             // 不进收敛阻塞，只进 findings 分诊
    },
    "audit_sampling": {                              // F3：覆盖质量抽样复核（自报的独立抽查）
      "enabled": true,
      "high_priority_sample_rate": 0.10,             // 高优先格子 10% 抽样复核
      "discrepancy_trigger": true                    // 声称 finding_created 却无 finding 时强制复核
    }
  },
  "capture_proxy": {                                 // F5：透明代理 —— fail-closed 白名单
    "enabled": true,
    "port": 8080,
    "ca": "engagement-specific",
    "allow_capture_hosts": [],                       // 白名单：仅这些 authorized 主机记录流量；激活时由 targets 派生
    "no_capture_hosts": ["api.anthropic.com", "api.deepseek.com", "cairn-server"],  // 次级排除（白名单之外必不记录）
    "record_pcap": true,
    "capture_quota": "10GB",                         // C4：全量配额，超限归档不删除
    "digest_budget": 8192                            // F2：给模型的最大 digest 字节/会话（全量另存）
  },
  "verify_policy": {                                 // F1/F6/F7：独立复核策略
    "max_reverify": 3,                               // needs_more_evidence 循环上限，超限升级人工
    "require_two_workers": true,                     // 推荐 2 worker 基线（单 worker 降级 cross_run）
    "verify_model": "deepseek-v4",                   // 可选：复核用不同模型池（跨模型硬独立性）
    "require_blind_stage": true                      // 盲审先于对照（防锚定）
  }
}
```
> 测试项目录不在本 schema 内 —— 由 `test_types` 表按 engagement 维护，创建 engagement 时预置默认目录模板（清单见 `coverage-engine-implementation-spec.md` §1.1）。

## 3. 覆盖度子域（TestType / CoverageItem / CoverageRecord / Waiver）

```sql
CREATE TABLE IF NOT EXISTS test_types (
    id             TEXT PRIMARY KEY,             -- 'tt_web_sqli'
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    category       TEXT NOT NULL CHECK (category IN
                    ('recon','scan','webapp','network','config','osint','auth','other')),
    risk           REAL NOT NULL DEFAULT 0.5 CHECK (risk BETWEEN 0 AND 1),
    default_depth  TEXT NOT NULL DEFAULT 'standard'
                   CHECK (default_depth IN ('baseline','standard','deep')),
    enabled        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (engagement_id, name)
);

CREATE TABLE IF NOT EXISTS coverage_items (
    id             TEXT PRIMARY KEY,             -- 'c-001'
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    target_id      TEXT NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    test_type_id   TEXT NOT NULL REFERENCES test_types(id) ON DELETE CASCADE,
    depth_required TEXT NOT NULL DEFAULT 'standard'
                   CHECK (depth_required IN ('baseline','standard','deep')),
    priority_score REAL NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'untested'
                   CHECK (status IN ('untested','in_progress','tested_no_issue',
                                     'tested_with_finding','not_applicable','waived')),
    seed_source    TEXT NOT NULL DEFAULT 'auto' CHECK (seed_source IN ('auto','human')),
    last_result    TEXT,
    tested_at      TEXT,
    tested_by      TEXT,
    retest_round   INTEGER NOT NULL DEFAULT 0,     -- A5：复测轮次（finding fixed 触发 rebuild 时 +1，原行重置，不新建行）
    current_intent_id TEXT,                        -- B1：格子互斥——正在测该格的 intent（认领时置，写回时清空）
    created_at     TEXT NOT NULL,
    UNIQUE (engagement_id, target_id, test_type_id)  -- A5：UNIQUE 保持；复测重建复用原行（retest_round+1 + 状态重置），不违反约束
);
CREATE INDEX IF NOT EXISTS idx_cov_eng_status ON coverage_items(engagement_id, status);
CREATE INDEX IF NOT EXISTS idx_cov_eng_prio   ON coverage_items(engagement_id, priority_score);
CREATE INDEX IF NOT EXISTS idx_cov_eng_intent ON coverage_items(current_intent_id);

CREATE TABLE IF NOT EXISTS coverage_records (
    id             TEXT PRIMARY KEY,             -- 'cr-001'
    item_id        TEXT NOT NULL REFERENCES coverage_items(id) ON DELETE CASCADE,
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    depth_achieved TEXT NOT NULL CHECK (depth_achieved IN ('baseline','standard','deep')),
    outcome        TEXT NOT NULL CHECK (outcome IN ('no_issue','finding_created','not_applicable')),
    source_fact_id TEXT,
    intent_id      TEXT,
    evidence_refs  TEXT,                         -- JSON 数组
    tested_scope   TEXT,                         -- C9：声明实际覆盖的具体范围（JSON：端点/参数/深度边界）；空=覆盖不明确
    partial        INTEGER NOT NULL DEFAULT 0,   -- C9：1=仅部分覆盖（未充分覆盖，热力图半色而非全绿）
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cov_rec_item ON coverage_records(item_id);

CREATE TABLE IF NOT EXISTS waivers (
    id            TEXT PRIMARY KEY,              -- 'w-001'
    item_id       TEXT NOT NULL REFERENCES coverage_items(id) ON DELETE CASCADE,
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('not_applicable','out_of_scope','risk_accepted')),
    reason        TEXT NOT NULL,                 -- 必填
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waiver_item ON waivers(item_id);
```

## 4. 探索图子域（Project / Fact / Intent / Hint —— 保留 + 挂 engagement）

```sql
CREATE TABLE IF NOT EXISTS projects (
    id                  TEXT PRIMARY KEY,        -- 'proj_001'
    engagement_id       TEXT REFERENCES engagements(id) ON DELETE CASCADE,  -- 新增列（可 NULL）
    title               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','stopped')),   -- A2: 移除 completed，完成仅作用于 Engagement
    bootstrap_enabled   INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    reason_worker       TEXT,
    reason_trigger      TEXT,
    reason_started_at   TEXT,
    reason_last_heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_projects_eng ON projects(engagement_id);

CREATE TABLE IF NOT EXISTS facts (
    id          TEXT NOT NULL,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,                   -- D3：统一时间线按此排序（graph 事件源）
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id                 TEXT NOT NULL,
    project_id         TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id         TEXT,
    description        TEXT NOT NULL,
    creator            TEXT NOT NULL,
    worker             TEXT,
    last_heartbeat_at  TEXT,
    created_at         TEXT NOT NULL,
    concluded_at       TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id  TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id    TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id          TEXT NOT NULL,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    creator     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO counters (name, value) VALUES ('engagement', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                     -- fact/intent/hint
    value      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

-- A1：engagement 作用域计数器（kind ↔ ID 前缀完整映射见 §4.1）
CREATE TABLE IF NOT EXISTS engagement_counters (
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN
                    ('finding','evidence','http_evidence','command_evidence',
                     'finding_traffic_link','coverage_item','coverage_record',
                     'waiver','report','traffic','verify_run','replay_run',
                     'audit_run','retest_confirmation',
                     'target','finding_history')),
    value         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (engagement_id, kind)
);
```

### 4.1 ID 前缀 ↔ 计数器 kind 映射表（A4 统一）

> 除注明外，ID 均为 engagement 作用域三位补零自增，经 `engagement_counters` 表（kind 列）唯一授予；
> `engagements` 用全局 `counters` 表（kind='engagement'）；`task_runs` / `task_events` 由 Dispatcher 侧全局生成——三者均不进 `engagement_counters`。

| 前缀 | 表 | 计数器 kind | 说明 |
|---|---|---|---|
| `fd-###` | `findings` | `finding` | 漏洞 |
| `fe-###` | `finding_evidence` | `evidence` | 证据文件引用 |
| `he-###` | `finding_http_evidence` | `http_evidence` | 请求/响应包证据 |
| `ce-###` | `finding_command_evidence` | `command_evidence` | 非 HTTP 命令回显证据 |
| `ftl-###` | `finding_traffic_links` | `finding_traffic_link` | 漏洞↔流量关联 |
| `c-###` | `coverage_items` | `coverage_item` | 覆盖格 |
| `cr-###` | `coverage_records` | `coverage_record` | 覆盖结论 |
| `w-###` | `waivers` | `waiver` | 豁免 |
| `rpt-###` | `reports` | `report` | 报告版本 |
| `tr-###` | `traffic_entries` | `traffic` | 捕获流量 |
| `vr-###` | `verify_runs` | `verify_run` | 独立复核 |
| `rp-###` | `replay_runs` | `replay_run` | 确定性重放 |
| `ar-###` | `audit_runs` | `audit_run` | 覆盖抽样复核 |
| `rc-###` | `finding_retest_confirmations` | `retest_confirmation` | 复测分类型确认 |
| `t-###` | `targets` | `target` | 授权/禁入目标 |
| `fh-###` | `finding_history` | `finding_history` | 漏洞状态流转审计 |
| `eng_###` | `engagements` | —（全局 `counters` 表，非 engagement_counters） | 授权项目 |
| `task-###` | `task_runs` | —（Dispatcher 全局） | 任务实例 |
| `ev-###` | `task_events` | —（Dispatcher 全局） | 事件（也可 `ev-<run_id>-<seq>`） |

## 5. 漏洞子域（Finding / Evidence / History）

```sql
CREATE TABLE IF NOT EXISTS findings (
    id               TEXT PRIMARY KEY,            -- 'fd-001'（engagement_counters.kind='finding' 自增）
    engagement_id    TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    target_id        TEXT NOT NULL REFERENCES targets(id) ON DELETE CASCADE,   -- 应用层 gate：DELETE /targets/{tid} 前检查引用 findings/coverage，未结算返回 409（human-workflow §2）；勿用 RESTRICT——会与 DELETE engagement 级联顺序冲突
    title            TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low','info')),  -- 生效 severity（初=agent_severity，复核后=verified_severity）
    agent_severity   TEXT NOT NULL CHECK (agent_severity IN ('critical','high','medium','low','info')),
    verified_severity TEXT CHECK (verified_severity IN ('critical','high','medium','low','info')), -- 复核定级（存在时生效）
    verify_status    TEXT NOT NULL DEFAULT 'none'
                     CHECK (verify_status IN ('none','pending','confirmed','rejected')),
    retest_pass      INTEGER NOT NULL DEFAULT 0,  -- 复测通过确认数（当前轮；≥2 且含不同类型才允许人工 closed）
    retest_round     INTEGER NOT NULL DEFAULT 0,  -- 复测轮次（fixed 触发 rebuild 时 +1；同轮同类型确认仅首次计 retest_pass）
    reverify_count   INTEGER NOT NULL DEFAULT 0,  -- F6：needs_more_evidence 循环计数，> max_reverify 升级人工
    cvss_score       REAL CHECK (cvss_score BETWEEN 0 AND 10),
    cvss_vector      TEXT,
    cwe_id           TEXT,                        -- 'CWE-521'
    category         TEXT,
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','pending_verify','pending_false_positive','verified','needs_review','fixed','false_positive','accepted','closed')),
    description      TEXT NOT NULL,
    remediation      TEXT,
    references_      TEXT,                        -- JSON 数组
    detected_by      TEXT NOT NULL,               -- worker名 / human
    source_fact_id   TEXT,                        -- 弱关联探索图
    coverage_item_id TEXT,                        -- 弱关联覆盖项（可选）
    evidence_summary TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    fixed_at         TEXT,
    closed_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_eng_status ON findings(engagement_id, status);
CREATE INDEX IF NOT EXISTS idx_findings_eng_target ON findings(engagement_id, target_id);
CREATE INDEX IF NOT EXISTS idx_findings_title_hash  ON findings(engagement_id, target_id, title);  -- 去重

CREATE TABLE IF NOT EXISTS finding_evidence (
    id          TEXT PRIMARY KEY,                 -- 'fe-001'
    finding_id  TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('screenshot','file','command_log','raw')),
    path        TEXT NOT NULL,                    -- 相对 evidence_root/{engagement_id}/...
    mime        TEXT,
    size        INTEGER,
    created_at  TEXT NOT NULL
);

-- 请求/响应包证据（Web 类漏洞必备：触发漏洞的完整 HTTP 请求 + 响应）
-- C2：source='captured' 时内容由 traffic_entries 派生（以捕获字节为准，agent 不逐字录入）；
--     source='agent_typed' 仅用于「无捕获会话」的降级（如 MITM 不可达），需命令证据佐证 + 人工/复核确认。
CREATE TABLE IF NOT EXISTS finding_http_evidence (
    id               TEXT PRIMARY KEY,            -- 'he-001'
    finding_id       TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL DEFAULT 1,  -- 同 finding 多组请求排序（按触发顺序）
    traffic_id       TEXT REFERENCES traffic_entries(id),  -- 溯源：captured 时必填；FK 无 ON DELETE——依赖级联顺序（同 §5 findings.target_id：DELETE engagement 时 finding_http_evidence 经 findings 级联先行删除，再删 traffic_entries；实现须用 DELETE engagement 冒烟验证，勿改 RESTRICT）
    source           TEXT NOT NULL DEFAULT 'captured'
                     CHECK (source IN ('captured','agent_typed')),
    method           TEXT NOT NULL,               -- GET/POST/PUT/DELETE/...
    url              TEXT NOT NULL,
    request_headers  TEXT,                        -- 原始请求头文本
    request_body     TEXT,                        -- 原始请求体（可为空）
    response_status  INTEGER,                     -- 响应状态码
    response_headers TEXT,                        -- 原始响应头文本
    response_body    TEXT,                        -- 原始响应体（截断后；全量在 traffic 文件）
    note             TEXT,
    captured_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_http_ev_finding ON finding_http_evidence(finding_id);

CREATE TABLE IF NOT EXISTS finding_history (
    id           TEXT PRIMARY KEY,
    finding_id   TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    note         TEXT,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_find_hist_finding ON finding_history(finding_id);

-- retest_pass 分类型明细账本（F4：不同类型确认才累计，同类型同轮重复不计）
-- retest_pass = 当前 retest_round 下该 finding 的确认行数；replay/verify/human 各类型每轮最多各计 1 次
CREATE TABLE IF NOT EXISTS finding_retest_confirmations (
    id           TEXT PRIMARY KEY,               -- 'rc-001'（engagement 作用域，kind='retest_confirmation'）
    finding_id   TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    retest_round INTEGER NOT NULL DEFAULT 0,     -- 与 findings.retest_round 对应
    kind         TEXT NOT NULL CHECK (kind IN ('replay','verify','human')),
    note         TEXT,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (finding_id, retest_round, kind)      -- 同轮同类型确认幂等（同类型重复不计）
);
CREATE INDEX IF NOT EXISTS idx_frc_finding ON finding_retest_confirmations(finding_id);
```

## 6. 交付子域（Report）

```sql
CREATE TABLE IF NOT EXISTS reports (
    id            TEXT PRIMARY KEY,               -- 'rpt-001'
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    format        TEXT NOT NULL CHECK (format IN ('markdown','html','pdf')),
    path          TEXT NOT NULL,
    generated_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_eng ON reports(engagement_id);
```

## 7. 调度状态子域（进程外恢复）

```sql
CREATE TABLE IF NOT EXISTS scheduler_state (
    key        TEXT PRIMARY KEY,                  -- 'reason_checkpoints' | 'worker_unhealthy_until' | ...
    value      TEXT NOT NULL,                     -- JSON
    updated_at TEXT NOT NULL
);
```

## 8. 全文检索（FTS5）

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS fts_facts USING fts5(
    fact_id UNINDEXED, project_id UNINDEXED, description
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_findings USING fts5(
    finding_id UNINDEXED, engagement_id UNINDEXED, title, description, remediation
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_coverage USING fts5(
    item_id UNINDEXED, target_value, test_type_name
);
```

## 9. 证据·复核·进度子域（capture / verify / replay / audit / progress）

> 配套：`capture-verify-progress-spec.md` §2/§4/§6/§7；F1/F2/F3/F4/C2/C4/C5 在此落地。

### 9.1 捕获流量（traffic_entries + 关联）

```sql
-- F2/C4：全量文件存储，DB 只存元数据；sha256 校验（F2）、分片计数（F2）、归档标记（C4）
CREATE TABLE IF NOT EXISTS traffic_entries (
    id             TEXT PRIMARY KEY,            -- 'tr-001'
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    captured_at    TEXT NOT NULL,               -- ISO8601 UTC
    method         TEXT NOT NULL,
    url            TEXT NOT NULL,               -- 完整 URL（含 query）
    host           TEXT,
    client         TEXT,                        -- worker 名（C12：由 client_ip 反查；无法区分时为 NULL）
    client_ip      TEXT,                        -- C12：来源容器 IP（bridge 网络每 worker 独立 IP，代理据此归属）
    status         INTEGER,
    req_path       TEXT NOT NULL,               -- traffic/.../xxx.req  全量请求文件
    resp_path      TEXT,                        -- traffic/.../xxx.resp 全量响应文件
    req_bytes      INTEGER NOT NULL,
    resp_bytes     INTEGER,
    content_type   TEXT,
    sha256         TEXT,                        -- F2：全量包校验和（分片拼接后计算）
    chunk_count    INTEGER NOT NULL DEFAULT 1,  -- F2：>100MB 分片数（xxx.req.0/1/2...）
    archived       INTEGER NOT NULL DEFAULT 0,  -- C4：已归档（finalize/archive 后压缩迁移）
    archived_path  TEXT,                        -- C4：归档位置（zstd 压缩，路径仍按 engagement 稳定）
    finding_linked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_traffic_eng_url ON traffic_entries(engagement_id, url);
CREATE INDEX IF NOT EXISTS idx_traffic_eng_time ON traffic_entries(engagement_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_traffic_eng_client ON traffic_entries(engagement_id, client);  -- C12：按 worker 归属检索

-- C2：finding↔流量关联；source=captured 时 finding_http_evidence 由此派生（以捕获字节为准）
CREATE TABLE IF NOT EXISTS finding_traffic_links (
    id          TEXT PRIMARY KEY,
    finding_id  TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    traffic_id  TEXT NOT NULL REFERENCES traffic_entries(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('trigger','related','verification','replay')),
    source      TEXT NOT NULL DEFAULT 'captured'
                CHECK (source IN ('captured','agent_typed')),
    created_at  TEXT NOT NULL,
    UNIQUE (finding_id, traffic_id, role)
);
CREATE INDEX IF NOT EXISTS idx_ftl_finding ON finding_traffic_links(finding_id);

-- 非 HTTP 命令回显证据（SSH/配置类漏洞主证据）
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

### 9.2 独立复核（verify_runs · F1/F2/F7）

```sql
-- 两阶段盲审（blind → comparison）+ 独立性级别 + 循环上限
-- stage/independence 供报告与审计呈现「复核有多独立」；
-- verdict 落定后同步写回 findings.verify_status / verified_severity / reverify_count。
CREATE TABLE IF NOT EXISTS verify_runs (
    id                    TEXT PRIMARY KEY,     -- 'vr-001'
    finding_id            TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    task_run_id           TEXT,                 -- 关联 progress.task_runs
    stage                 TEXT NOT NULL DEFAULT 'blind'
                          CHECK (stage IN ('blind','comparison','escalated')),  -- F1：blind→comparison 两阶段；escalated=needs_more 超限升级人工后的记录
    independence          TEXT NOT NULL DEFAULT 'none'
                          CHECK (independence IN ('cross_worker','cross_model','cross_run','human','none')),
    input_traffic_digest  TEXT,                 -- F2：降采样 digest 引用（≤digest_budget；全量在 traffic 文件）
    observations          TEXT,                 -- 盲审阶段独立观察（JSON：见到的漏洞+定级）
    verdict               TEXT CHECK (verdict IN ('confirmed','rejected','needs_more_evidence')),
    verified_severity     TEXT CHECK (verified_severity IN ('critical','high','medium','low','info')),
    reason                TEXT,
    verified_traffic_ids  TEXT,                 -- JSON 数组
    suggested_action      TEXT,
    created_at            TEXT NOT NULL,
    finished_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_vr_finding ON verify_runs(finding_id);
```

### 9.3 确定性重放复核（replay_runs · F4）

```sql
-- 复测多确认的证据基础：重放原始触发请求 + payload 变体，比对响应签名。
-- matched_original>0 → 仍触发（漏洞在）；result='remediated' 才计入 retest_pass。
CREATE TABLE IF NOT EXISTS replay_runs (
    id                  TEXT PRIMARY KEY,       -- 'rp-001'
    engagement_id       TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    finding_id          TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    trigger_traffic_id  TEXT NOT NULL REFERENCES traffic_entries(id),
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','success','failed','blocked')),
    payload_variants    INTEGER NOT NULL DEFAULT 0,
    matched_original    INTEGER NOT NULL DEFAULT 0,  -- 仍与原始响应签名一致的变体数
    result              TEXT CHECK (result IN ('unchanged','remediated','ambiguous','error')),
    evidence_traffic_id TEXT,                   -- replay 自身经捕获代理留档（证据闭环）
    started_at          TEXT,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_rp_finding ON replay_runs(finding_id);
```

### 9.4 覆盖抽样复核（audit_runs · F3）

```sql
-- 覆盖自报的独立抽查：不一致 → 覆盖项回退 untested + reason 重新优先级排序
CREATE TABLE IF NOT EXISTS audit_runs (
    id               TEXT PRIMARY KEY,          -- 'ar-001'
    engagement_id    TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    coverage_item_id TEXT NOT NULL REFERENCES coverage_items(id) ON DELETE CASCADE,
    reason           TEXT NOT NULL CHECK (reason IN ('sampling','discrepancy','manual')),
    auditor          TEXT,                      -- worker 名（≠ 原测试者）
    verdict          TEXT CHECK (verdict IN ('covered_matches','coverage_discrepancy')),
    depth_reached    TEXT CHECK (depth_reached IN ('baseline','standard','deep')),
    note             TEXT,
    created_at       TEXT NOT NULL,
    finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ar_item ON audit_runs(coverage_item_id);
```

### 9.5 进度监控（task_runs / task_events）

```sql
CREATE TABLE IF NOT EXISTS task_runs (
    id           TEXT PRIMARY KEY,              -- 'task-001'
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,  -- B2/FK：级联删除与 DDL §11 语义一致
    project_id   TEXT,                          -- B2：可空——verify/audit/replay 为 engagement 级任务，可能不挂 project
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

## 10. 迁移思路（自 v1 库升级）

| 步骤 | 内容 |
|---|---|
| 1 | 建以上全部新表（`CREATE TABLE IF NOT EXISTS` 幂等，老库无冲突） |
| 2 | `ALTER TABLE projects ADD COLUMN engagement_id`（先查 `PRAGMA table_info(projects)` 是否存在） |
| 3 | 兼容历史：`bootstrap_mode → bootstrap_enabled` 迁移（沿用 v1 `_ensure_project_columns` 思路） |
| 4 | 历史 `settings` 补 `global_kill_switch`、`coverage_policy` 列（或通过重建 settings 单例行迁移） |
| 5 | 计数器：`counters` 增 `engagement` 行；新建 `engagement_counters`（findings/coverage/evidence 等 kind）；`scoped_counters` 收敛回 project 图专用 kind |
| 6 | 空库回填：`INSERT OR IGNORE` settings / counters 初始行 |
| 7 | 索引与 FTS5 虚拟表在迁移末尾统一 `CREATE INDEX IF NOT EXISTS` / `CREATE VIRTUAL TABLE IF NOT EXISTS` |
| 8 | 备份：迁移前 `VACUUM INTO 'backup_<ts>.db'` |

## 11. 数据删除语义

- `DELETE engagement` → 级联删除 targets / findings / evidence / history / reports / coverage / traffic_entries / verify_runs / replay_runs / audit_runs / task_runs / projects(及其 facts/intents/hints)
- 证据文件：删除 engagement 时由应用层清理 `evidence_root/{engagement_id}/` 与 `traffic/{engagement_id}/`（含归档）——DB 引用先行，避免悬挂
- 探索图与漏洞/覆盖解耦：删除 project 不影响 findings / coverage_items（二者按 engagement 存续）
- **C4 归档不随删随走**：`archived=1` 的 traffic 文件在归档层独立保留；仅 `DELETE engagement`（显式销毁）才清除归档
