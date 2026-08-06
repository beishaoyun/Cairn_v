# 覆盖度引擎实现规格（Coverage Engine Implementation Spec）

> 配套文档：`architecture-research-report-pentest-v2.md` §4.13 / §5.2 / §8.13
> 用途：新平台「应测尽测」覆盖度引擎的落地实现细节 —— 可直接照着建表、写校验器、画热力图

---

## 1. 数据库 DDL（可直接执行）

> 前提：`engagements`、`targets` 表已存在；所有时间戳为 ISO8601 UTC 字符串；与探索图（facts/intents/hints）解耦，仅 `coverage_records` 弱关联 `source_fact_id` / `intent_id`。

```sql
-- ── 测试项目录：ROE 授权的测试类型，人工按规则配置 ──────────────────
CREATE TABLE IF NOT EXISTS test_types (
    id             TEXT PRIMARY KEY,           -- 'tt_web_sqli'（slug）
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,              -- 展示名，如 'SQL 注入'
    category       TEXT NOT NULL CHECK (category IN
                    ('recon','scan','webapp','network','config','osint','auth','other')),  -- 与 DDL §3 对齐
    risk           REAL NOT NULL DEFAULT 0.5 CHECK (risk BETWEEN 0 AND 1),
    default_depth  TEXT NOT NULL DEFAULT 'standard'
                   CHECK (default_depth IN ('baseline','standard','deep')),
    enabled        INTEGER NOT NULL DEFAULT 1,
    UNIQUE (engagement_id, name)
);

-- ── 覆盖项 = 测试矩阵格子（目标 × 测试项 × 深度）────────────────────
CREATE TABLE IF NOT EXISTS coverage_items (
    id              TEXT PRIMARY KEY,          -- 'c-001'（项目内自增）
    engagement_id   TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    target_id       TEXT NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    test_type_id    TEXT NOT NULL REFERENCES test_types(id) ON DELETE CASCADE,
    depth_required  TEXT NOT NULL DEFAULT 'standard'
                    CHECK (depth_required IN ('baseline','standard','deep')),
    priority_score  REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'untested'
                    CHECK (status IN ('untested','in_progress',
                                      'tested_no_issue','tested_with_finding',
                                      'not_applicable','waived')),
    seed_source     TEXT NOT NULL DEFAULT 'auto' CHECK (seed_source IN ('auto','human')),
    last_result     TEXT,                      -- 最近一次 outcome（冗余，供热力图）
    tested_at       TEXT,
    tested_by       TEXT,
    retest_round    INTEGER NOT NULL DEFAULT 0,   -- A5：复测轮次（finding fixed 触发 rebuild 时 +1，原行重置，不新建行）
    current_intent_id TEXT,                      -- B1：格子互斥——正在测该格的 intent（认领时置，写回时清空）
    created_at      TEXT NOT NULL,
    UNIQUE (engagement_id, target_id, test_type_id)  -- A5：UNIQUE 保持；复测重建复用原行（retest_round+1 + 状态重置），不违反约束
);
CREATE INDEX IF NOT EXISTS idx_cov_items_eng_status ON coverage_items(engagement_id, status);
CREATE INDEX IF NOT EXISTS idx_cov_items_eng_target ON coverage_items(engagement_id, target_id);
CREATE INDEX IF NOT EXISTS idx_cov_items_eng_prio   ON coverage_items(engagement_id, priority_score);

-- ── 覆盖结论：每次测试留痕，支持复测历史 ──────────────────────────
CREATE TABLE IF NOT EXISTS coverage_records (
    id             TEXT PRIMARY KEY,           -- 'cr-001'（项目内自增）
    item_id        TEXT NOT NULL REFERENCES coverage_items(id) ON DELETE CASCADE,
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    depth_achieved TEXT NOT NULL CHECK (depth_achieved IN ('baseline','standard','deep')),
    outcome        TEXT NOT NULL CHECK (outcome IN ('no_issue','finding_created','not_applicable')),
    source_fact_id TEXT,                       -- 弱关联探索图（可为 NULL）
    intent_id      TEXT,                       -- 弱关联探索图（可为 NULL）
    evidence_refs  TEXT,                       -- JSON 数组，相对路径 ["e-001/screenshot.png"]（服务端拼 evidence_root/{engagement_id}/）
    tested_scope   TEXT,                       -- C9：声明实际覆盖的具体范围（JSON：端点/参数/深度边界）；空=覆盖不明确
    partial        INTEGER NOT NULL DEFAULT 0, -- C9：1=仅部分覆盖（未充分覆盖，热力图半色而非全绿）
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cov_rec_item ON coverage_records(item_id);

-- ── 豁免：任何"不测"必须有理由 ──────────────────────────────────
CREATE TABLE IF NOT EXISTS waivers (
    id            TEXT PRIMARY KEY,            -- 'w-001'（项目内自增）
    item_id       TEXT NOT NULL REFERENCES coverage_items(id) ON DELETE CASCADE,
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('not_applicable','out_of_scope','risk_accepted')),
    reason        TEXT NOT NULL,               -- 必填，否则不允许豁免
    created_by    TEXT NOT NULL,               -- 仅人工
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waiver_item ON waivers(item_id);

> **B4 硬规则**：`coverage_items.status='not_applicable'` **必须伴随一条 `waivers(kind='not_applicable')` 记录**，否则仍视为未覆盖缺口；杜绝"不写理由直接标 N/A"。

-- ── FTS5 检索（可选，覆盖项可按 target/test_type 名检索）───────────
-- 注意：SQLite 虚拟表无法 FK；由应用层在写表时同步维护索引。
-- 与 DDL §8 / server/db.py 口径一致：非 contentless（无 content=''），常规 FTS5 表，
-- 建表即可 INSERT/UPDATE/DELETE；FTS 同步触发器未实现（占位表）。
-- target_value/test_type_name 不在 coverage_items 表内（只有 id），
-- 应用层须 JOIN targets/test_types 组装后手工写索引（服务端统一在 coverage writer 内维护）。
CREATE VIRTUAL TABLE IF NOT EXISTS fts_coverage USING fts5(
    item_id UNINDEXED,
    target_value,
    test_type_name
);
```

**ID 生成**：覆盖/漏洞等 **engagement 作用域** ID 使用新增 `engagement_counters` 表（kind：`coverage_item`→`c-###`、`coverage_record`→`cr-###`、`waiver`→`w-###`、`finding`→`fd-###`、`http_evidence`→`he-###`）；`test_types` 用 `tt_<slug>` 幂等键。全部 engagement 内自增、三位补零。project 作用域（fact/intent/hint）仍用 `scoped_counters`。

**迁移兼容**：`coverage_items` 对老库无侵入（新表）；若 `targets` 未建立，需先补 targets 表。

### 1.1 默认测试项目录模板（创建 engagement 时预置）

> 补 `v2 §8.9（D3）` 引用的"内置默认测试项目录模板"。**创建 engagement 时把下表写入 `test_types`**（`enabled=1`，人工可增删改），bootstrap 播种只作用于已启用的 test_types。
> 本表是起点模板：risk / default_depth 均为**默认值**，可在 engagement 内按资产价值人工覆盖（见 `human-workflow-guide.md` §3「调整深度」）。`category` 取值与 DDL §3 CHECK 一致。

| id（`tt_<slug>` 幂等键） | name（展示名） | category | risk（默认） | default_depth（默认） |
|---|---|---|---|---|
| `tt_asset_discovery` | 资产发现（子域/端口/服务） | recon | 0.7 | standard |
| `tt_service_identification` | 服务识别（版本/banner） | recon | 0.6 | standard |
| `tt_tech_fingerprint` | 技术栈指纹 | recon | 0.5 | baseline |
| `tt_osint_gathering` | OSINT 情报搜集 | osint | 0.4 | baseline |
| `tt_port_scan` | 端口扫描 | scan | 0.6 | baseline |
| `tt_vuln_scanning` | 漏洞扫描（nuclei 模板集等） | scan | 0.7 | standard |
| `tt_ssl_tls_scan` | TLS/证书配置检查 | scan | 0.4 | baseline |
| `tt_directory_bruteforce` | 目录/接口爆破 | scan | 0.6 | standard |
| `tt_web_sqli` | SQL 注入 | webapp | 0.9 | deep |
| `tt_web_xss` | 跨站脚本（XSS） | webapp | 0.8 | standard |
| `tt_web_csrf` | CSRF | webapp | 0.5 | standard |
| `tt_web_auth_bypass` | 认证绕过 | webapp | 0.8 | deep |
| `tt_web_weak_credentials` | 弱口令/默认凭据 | auth | 0.9 | standard |
| `tt_web_session` | 会话管理 | webapp | 0.7 | standard |
| `tt_web_file_upload` | 文件上传 | webapp | 0.8 | standard |
| `tt_web_ssti` | 服务端模板注入（SSTI） | webapp | 0.8 | deep |
| `tt_web_command_injection` | 命令注入 | webapp | 0.9 | deep |
| `tt_web_path_traversal` | 路径穿越 | webapp | 0.8 | standard |
| `tt_web_ssrf` | SSRF | webapp | 0.8 | standard |
| `tt_web_cors` | CORS 配置 | webapp | 0.4 | baseline |
| `tt_web_open_redirect` | 开放重定向 | webapp | 0.4 | baseline |
| `tt_web_info_disclosure` | 信息泄露/敏感文件 | config | 0.6 | standard |
| `tt_net_service_hardening` | 服务弱配置/加固 | network | 0.5 | standard |
| `tt_net_ssh_brute` | SSH 弱口令 | auth | 0.8 | standard |
| `tt_net_snmp` | SNMP 枚举 | network | 0.6 | standard |
| `tt_net_default_creds` | 默认凭据（设备/中间件） | config | 0.8 | baseline |
| `tt_cfg_insecure_config` | 不安全配置 | config | 0.5 | baseline |
| `tt_cfg_encryption` | 传输/存储加密缺失 | config | 0.6 | standard |

> **与 bootstrap 播种的关系**：bootstrap 的 `discoveries[]`（target/port/service）命中目录项后生成覆盖项（`seed_source='auto'`）；目录项 `enabled=0` 的跳过播种。`network_cap=false` 的 engagement 应禁用网络型测试项（如 `tt_net_*`）或标注 N/A。

---

## 2. 缺口排序（priority）与收敛判定（report-ready）伪代码

```python
# coverage/accounting.py

DEPTH_BONUS = 0.2  # deep/standard 相对 baseline 的加成，收敛进 runtime.tuning

def priority_score(asset_criticality: float, test_type_risk: float, depth: str) -> float:
    """优先级 = 资产重要性 × 测试项风险 × 深度加成。全部可配置。"""
    bonus = DEPTH_BONUS if depth != "baseline" else 0.0
    return asset_criticality * test_type_risk * (1.0 + bonus)


# D5：asset_criticality 默认来源（防止全员默认 0.5 导致排序退化为纯 risk 排序）。
# 默认值按资产类型/暴露面推断（可配置表），登记 target 时自动填入，人工可覆盖（PUT /targets/{tid}）。
DEFAULT_CRITICALITY = {
    "public_domain": 0.7,   # 公网域名/URL
    "public_ip":     0.8,   # 公网 IP
    "private_cidr":  0.6,   # 内网网段
    "private_host":  0.5,   # 内网主机
    "core_service":  0.9,   # 探测出 DB/认证/核心服务后自动上调
}

def infer_criticality(kind: str, service_kind: str | None = None) -> float:
    """D5：bootstrap/recon 播种与 targets 登记时调用；探测出核心服务（db/auth/ldap/...）上调。"""
    base = DEFAULT_CRITICALITY.get(kind, 0.5)
    return max(base, 0.9) if service_kind in ("mysql", "postgres", "ldap", "redis", "auth", "ssh") else base


def compute_gaps(conn, engagement_id, *, threshold: float = 0.0, exclude_in_progress: bool = False, limit: int | None = 50) -> list[dict]:
    """确定性缺口清单：untested（+可选的 in_progress），排除已豁免/不适用。

    B1 格子互斥：reason 消费缺口时必须 exclude_in_progress=True ——
    in_progress 格已被某 explore intent 认领（current_intent_id 非空），
    不得再为它派第二个 explore，否则并发下必然 COVERAGE_ALREADY_COVERED。
    limit：reason 消费按 priority 降序取前 N（默认 50），防缺口列表撑爆 prompt。
    """
    status_filter = "ci.status IN ('untested','in_progress')" if not exclude_in_progress \
                    else "ci.status = 'untested' AND ci.current_intent_id IS NULL"
    rows = conn.execute(
        f"""
        SELECT ci.id, ci.target_id, ci.test_type_id, ci.depth_required, ci.priority_score,
               t.value AS target_value, t.criticality, tt.name AS test_type_name, tt.risk
        FROM coverage_items ci
        JOIN targets t    ON ci.target_id = t.id
        JOIN test_types tt ON ci.test_type_id = tt.id
        WHERE ci.engagement_id = ?
          AND {status_filter}
        """,
        (engagement_id,),
    ).fetchall()
    gaps = []
    for r in rows:
        # A3：priority 始终实时计算（criticality/risk 变更即生效）；
        # coverage_items.priority_score 仅作展示缓存，不作为排序依据。
        prio = priority_score(r["criticality"], r["risk"], r["depth_required"])
        if prio >= threshold:
            gaps.append({
                "item_id": r["id"], "target_id": r["target_id"],
                "target_value": r["target_value"], "test_type_id": r["test_type_id"],
                "test_type_name": r["test_type_name"], "depth": r["depth_required"],
                "priority": round(prio, 3),
            })
    gaps.sort(key=lambda g: (-g["priority"], g["target_id"], g["item_id"]))
    return gaps[:limit] if limit is not None else gaps


def coverage_summary(conn, engagement_id, *, exclude_item_ids: set[str] | None = None) -> dict:
    """覆盖率汇总（热力图顶栏数据）。exclude_item_ids：F11 收敛口径排除集。
    C9：partial 单独计数——部分覆盖格不算充分覆盖（热力图半色），也不阻塞收敛但明示。"""
    exclude_item_ids = exclude_item_ids or set()
    total = covered = untested = in_progress = na = waived = with_finding = partial = 0
    for (item_id, status,) in conn.execute(
        "SELECT id, status FROM coverage_items WHERE engagement_id = ?", (engagement_id,)
    ).fetchall():
        if item_id in exclude_item_ids:
            continue
        total += 1
        if status == "tested_no_issue":
            covered += 1
            partial += conn.execute(
                "SELECT COUNT(*) FROM coverage_records WHERE item_id=? AND partial=1", (item_id,)
            ).fetchone()[0]
        elif status == "tested_with_finding": covered += 1; with_finding += 1
        elif status == "not_applicable": covered += 1; na += 1
        elif status == "waived": covered += 1; waived += 1
        elif status == "in_progress": in_progress += 1
        else: untested += 1
    return {
        "total": total, "covered": covered,
        "coverage_ratio": round(covered / total, 4) if total else 1.0,
        "untested": untested, "in_progress": in_progress,
        "not_applicable": na, "waived": waived, "with_finding": with_finding,
        "partial": partial,
    }


# ── 收敛策略（存 engagement.scope_policy.coverage，可配置）──────────────
DEFAULT_COVERAGE_POLICY = {
    "min_priority_threshold": 0.30,   # 低于该优先级的缺口视为低价值
    "target_coverage": 0.95,          # 整体覆盖率目标
    "require_all_findings_triaged": True,  # finalize 前 findings 无未分诊
    "require_depth": "standard",      # 高优先级项必须达到的最小深度
    # F11：auto_created 目标（findings 写回自动建）的覆盖项不阻塞收敛
    "auto_created_closure": {
        "max_extra_depth": "baseline",
        "excluded_from_report_ready": True,
    },
    # F3：覆盖质量抽样复核（自报的独立抽查）
    "audit_sampling": {
        "enabled": True,
        "high_priority_sample_rate": 0.10,   # 高优先格子 10% 抽样
        "discrepancy_trigger": True,         # 声称 finding_created 却无 finding → 强制审计
    },
    # C8：reason 收敛空转保护（类比 F6 的 needs_more 上限）——连续失败升级人工
    "reason_escalation": {
        "max_consecutive_failures": 3,       # 连续 N 次校验失败（无 intent 也无 finalize）→ 升级人工
        "max_finalize_rejected": 3,          # 连续 N 次建议 finalize 被人工拒绝 → 升级人工
        "escalate_to": "needs_review",       # 升级后 reason 停止自动重试，仅人工可恢复
    },
}


def _auto_created_item_ids(conn, engagement_id) -> set[str]:
    """F11：auto_created 目标的覆盖项 id（收敛口径排除用）。"""
    return {
        r["id"] for r in conn.execute(
            """
            SELECT ci.id FROM coverage_items ci
            JOIN targets t ON ci.target_id = t.id
            WHERE ci.engagement_id = ? AND t.auto_created = 1
            """,
            (engagement_id,),
        ).fetchall()
    }


def report_ready(conn, engagement_id, policy: dict | None = None) -> tuple[bool, dict]:
    """判定是否达到 report-ready（finalize 的 gate）。

    F11 闭环：auto_created 目标的覆盖项**不参与** report-ready 的深度校验与覆盖率分母，
    避免「发现新资产 → 新增未覆盖项 → 覆盖率下降 → 永远无法收敛」的无限回退。
    它们仍显示在热力图并照常测试（补全度），只是不阻塞 finalize。
    """
    policy = policy or DEFAULT_COVERAGE_POLICY
    closure = policy.get("auto_created_closure", {})
    excluded = _auto_created_item_ids(conn, engagement_id) if closure.get("excluded_from_report_ready") else set()

    uncovered_high = [
        g for g in compute_gaps(conn, engagement_id, threshold=policy["min_priority_threshold"])
        if g["item_id"] not in excluded
    ]
    DEPTH_RANK = {"baseline": 0, "standard": 1, "deep": 2}
    min_depth = DEPTH_RANK[policy["require_depth"]]
    depth_shortfall = 0
    for (item_id, last_depth,) in conn.execute(
        """
        SELECT ci.id,
               (SELECT cr.depth_achieved FROM coverage_records cr
                WHERE cr.item_id = ci.id ORDER BY cr.created_at DESC LIMIT 1)
        FROM coverage_items ci
        WHERE ci.engagement_id = ?
          AND ci.status IN ('tested_no_issue','tested_with_finding')
        """,
        (engagement_id,),
    ).fetchall():
        if item_id in excluded:
            continue  # F11：auto_created 目标项不参与深度达标校验
        # C1：按深度等级比较（baseline=0/standard=1/deep=2），禁止字符串字典序比较
        if last_depth and DEPTH_RANK.get(last_depth, 0) < min_depth:
            depth_shortfall += 1
    summary = coverage_summary(conn, engagement_id, exclude_item_ids=excluded)
    untriaged = conn.execute(
        # 未分诊 = 尚无结论的态；verified 已复核确认，属于已分诊，不阻塞 finalize
        "SELECT COUNT(*) FROM findings WHERE engagement_id = ? AND status IN ('open','pending_verify','pending_false_positive','needs_review')",
        (engagement_id,),
    ).fetchone()[0]

    ok = (
        not uncovered_high
        and depth_shortfall == 0
        and summary["coverage_ratio"] >= policy["target_coverage"]
        and (not policy["require_all_findings_triaged"] or untriaged == 0)
    )
    return ok, {
        "uncovered_high": uncovered_high, "depth_shortfall": depth_shortfall,
        "summary": summary, "untriaged_findings": untriaged,
        "policy": policy,
    }
```

**播种（bootstrap/recon）伪代码**：

```python
# coverage/seeding.py

SERVICE_TO_TEST_TYPES = {
    "http":   ["webapp_dir", "webapp_sqli", "webapp_xss", "webapp_auth", "webapp_ssrf", ...],
    "https":  [...],
    "ssh":    ["config_hardening", "auth_default_cred"],
    "mysql":  ["config_hardening", "auth_default_cred"],
    ...
}  # 服务 → 适用测试项 映射表（可配置，识别不到的只播种 baseline 扫描项）

def seed_from_discovery(conn, engagement_id, discoveries: list[DiscoveredService]) -> int:
    """bootstrap/recon 落图后调用：为每个发现资产播种覆盖项。"""
    created = 0
    for d in discoveries:
        target_id = ensure_target(conn, engagement_id, d)   # 不存在则建 target（scope 校验后）
        for tt in applicable_test_types(conn, engagement_id, d.service_kind):
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO coverage_items
                      (id, engagement_id, target_id, test_type_id, depth_required,
                       priority_score, status, seed_source, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'untested', 'auto', ?)
                    """,
                    (next_item_id(conn, engagement_id), engagement_id, target_id, tt["id"],
                     tt["default_depth"],
                     priority_score(infer_criticality("hostname", d.service_kind), tt["risk"], tt["default_depth"])), utcnow()),
                )
                # C2：用 cursor.rowcount 取本次 INSERT 影响行数（total_changes 是累计值）
                created += conn.execute("SELECT changes() AS c").fetchone()["c"]
            except sqlite3.IntegrityError:
                pass  # 已存在（重复发现）
    return created
```

**格子互斥协议（B1）**——一个覆盖格同一时刻最多被一个 explore intent 认领：

```python
# coverage/accounting.py
def claim_item_for_intent(conn, item_id, intent_id) -> bool:
    """explore 派发前调用：item 未覆盖（untested 且 current_intent_id IS NULL）→
    置 in_progress + current_intent_id=intent_id，返回 True；否则返回 False（本格已被认领）。"""
    cur = conn.execute(
        "UPDATE coverage_items SET status='in_progress', current_intent_id=? "
        "WHERE id=? AND status='untested' AND current_intent_id IS NULL",
        (intent_id, item_id),
    )
    return cur.rowcount > 0

def release_item_for_intent(conn, item_id, intent_id) -> None:
    """任务失败/取消/超时：仅当 current_intent_id == intent_id 才回退 untested，防误清他人认领。"""
    conn.execute(
        "UPDATE coverage_items SET status='untested', current_intent_id=NULL "
        "WHERE id=? AND current_intent_id=?",
        (item_id, intent_id),
    )

def covered_items_for_writeback(conn, eid, *, item_ids, intent_id, fact_id,
                                depth_achieved, outcome, evidence_refs, tested_scope) -> None:
    """explore 写回（write_coverage_result 前置）：校验 covered_items 属于本 engagement
    且 current_intent_id == 本次 intent（或显式 retest 放行）→ 写 coverage_records +
    更新 item 状态 + 清空 current_intent_id。同事务。"""
    for iid in item_ids:
        item = conn.execute("SELECT * FROM coverage_items WHERE id=?", (iid,)).fetchone()
        if item is None or item["engagement_id"] != eid:
            raise CoverageValidation(f"COVERAGE_NOT_APPLICABLE: {iid}")
        if item["current_intent_id"] != intent_id:
            # 仅本次 intent 认领者可写回；NULL 不放行——未认领格必须由调度器先 claim；
            # 复测格经 rebuild 已清空 current_intent_id，由 retest explore 重新 claim 后再写回
            raise CoverageValidation(f"COVERAGE_ALREADY_COVERED: {iid} 被 {item['current_intent_id']} 认领")
        # ...写 coverage_records（含 tested_scope/partial）→ 更新 status/current_intent_id=NULL
```

> 派发链路：reason 产出 intent（引用格子）→ Dispatcher 派发 explore 前逐一 `claim_item_for_intent`；
> 任一格子认领失败 → 该 intent 不派发（记录"格子忙"），下轮 reason 换格。
> `COVERAGE_ALREADY_COVERED` 在并发下成为**预期内分支**而非校验事故：explore 写回时格子若已被他人认领 → 该次写回作废 + release，交由下轮 reason 重排。

**复测重建（A5）**——finding `fixed` 触发，**复用原行**而非新建（UNIQUE 约束下不可建新行）：

```python
# coverage/accounting.py
def rebuild_for_retest(conn, engagement_id, target_id, test_type_id, *, depth="retest") -> CoverageItem:
    """A5：找到 (target, test_type) 原覆盖项 → retest_round+1 + 状态重置为 untested；
    coverage_records 历史保留（复测前后对比用）。格子不存在时（异常）才新建。"""
    item = conn.execute(
        "SELECT * FROM coverage_items WHERE engagement_id=? AND target_id=? AND test_type_id=?",
        (engagement_id, target_id, test_type_id),
    ).fetchone()
    if item is None:
        return upsert_coverage_item(conn, engagement_id, target_id, test_type_id, "standard", seed_source="human")
    conn.execute(
        "UPDATE coverage_items SET status='untested', last_result=NULL, tested_at=NULL, "
        "tested_by=NULL, current_intent_id=NULL, retest_round=retest_round+1, "
        "depth_required=?, created_at=? WHERE id=?",
        (depth if depth != "retest" else item["depth_required"], utcnow(), item["id"]),
    )
    return item
```

> 前端热力图复测联动改语义：fixed → 该格**重置为 untested 并高亮**（`retest_round` 徽标），而非"新建 item"。

**抽样复核（coverage audit）伪代码（F3）**——覆盖自报的独立抽查：

```python
# coverage/audit.py

def pick_audit_targets(conn, engagement_id, policy: dict | None = None) -> list[dict]:
    """F3：选出需要独立复核的覆盖项。

    两类触发：
    - sampling：高优先已测格子按 high_priority_sample_rate 抽样（抽查"自报已测"是否真测）；
    - discrepancy：声称 finding_created 但该 target 无任何 finding → 强制复核（结果可疑）。
    """
    policy = policy or DEFAULT_COVERAGE_POLICY
    audit = policy.get("audit_sampling", {})
    targets = []
    if audit.get("enabled", True):
        rows = conn.execute(
            """
            SELECT ci.id, t.criticality, tt.risk, ci.depth_required,
                   (SELECT cr.outcome FROM coverage_records cr
                    WHERE cr.item_id = ci.id ORDER BY cr.created_at DESC LIMIT 1) AS outcome
            FROM coverage_items ci
            JOIN targets t    ON ci.target_id = t.id
            JOIN test_types tt ON ci.test_type_id = tt.id
            WHERE ci.engagement_id = ?
              AND ci.status IN ('tested_no_issue','tested_with_finding')
            """,
            (engagement_id,),
        ).fetchall()
        for r in rows:
            # A3：与 compute_gaps 同口径 —— 实时计算 priority，不用缓存列 priority_score
            #（验收点 1"同一查询路径，杜绝两套口径"；缓存列仅作热力图展示）
            prio = priority_score(r["criticality"], r["risk"], r["depth_required"])
            if prio >= policy.get("min_priority_threshold", 0.3):
                if random.random() < audit.get("high_priority_sample_rate", 0.10):
                    targets.append({"item_id": r["id"], "reason": "sampling"})
            # discrepancy：声称有 finding 但 target 下无 finding → 强制审计
            if audit.get("discrepancy_trigger") and r["outcome"] == "finding_created":
                cnt = conn.execute(
                    "SELECT COUNT(*) FROM findings f WHERE f.engagement_id=? AND f.coverage_item_id=?",
                    (engagement_id, r["id"]),
                ).fetchone()[0]
                if cnt == 0:
                    targets.append({"item_id": r["id"], "reason": "discrepancy"})
    return targets


def apply_audit_verdict(conn, engagement_id, *, item_id, verdict, auditor, reason: str = "sampling") -> None:
    """F3：审计落定。audit_runs 留痕；coverage_discrepancy → 覆盖项回退 untested + 缺口重排。

    （理由：自报不可全信——热力图绿 ≠ 真测过。抽查打回后，reason 会重新把该格排进缺口。）
    reason 由调用方传入（sampling/discrepancy/manual），不得硬编码——discrepancy/manual 触发路径会失真。
    """
    conn.execute(
        "INSERT INTO audit_runs (id, engagement_id, coverage_item_id, reason, auditor, verdict, created_at) VALUES (?,?,?,?,?,?,?)",
        (next_id("audit"), engagement_id, item_id, reason, auditor, verdict, utcnow()),
    )
    if verdict == "coverage_discrepancy":
        conn.execute(
            "UPDATE coverage_items SET status='untested', last_result='audit_discrepancy', tested_at=NULL WHERE id=?",
            (item_id,),
        )
```

---

## 3. reason / explore 输出契约（JSON Schema + 校验器规则）

### 3.1 reason 输出契约（覆盖度记账）

```json
{
  "accepted": true,
  "data": {
    "intents": [
      {
        "from": ["f003"],
        "description": "对 10.0.0.5 的 8080 服务做 SQL 注入测试",
        "coverage_item_ids": ["c-013", "c-014"]
      }
    ],
    "coverage": {
      "recommend_finalize": false,
      "reason": "剩余缺口为低价值项",
      "waivers": [
        { "item_id": "c-099", "kind": "not_applicable", "reason": "该服务无认证功能" }
      ]
    }
  }
}
```

JSON Schema（关键约束）：

```json
{
  "type": "object",
  "required": ["accepted", "data"],
  "properties": {
    "accepted": { "const": true },
    "data": {
      "type": "object",
      "required": [],
      "properties": {
        "intents": {
          "type": "array",
          "maxItems": 8,
          "items": {
            "type": "object",
            "required": ["from", "description", "coverage_item_ids"],
            "properties": {
              "from": { "type": "array", "items": { "type": "string" }, "minItems": 1 },
              "description": { "type": "string", "minLength": 1 },
              "coverage_item_ids": { "type": "array", "items": { "type": "string" }, "minItems": 1 }
            }
          }
        },
        "coverage": {
          "type": "object",
          "properties": {
            "recommend_finalize": { "type": "boolean" },
            "reason": { "type": "string" },
            "waivers": {
              "type": "array",
              "items": {
                "type": "object",
                "required": ["item_id", "kind", "reason"],
                "properties": {
                  "item_id": { "type": "string" },
                  "kind": { "enum": ["not_applicable", "out_of_scope", "risk_accepted"] },
                  "reason": { "type": "string", "minLength": 1 }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

**reason 校验器规则（校验顺序）**：
1. `accepted` 结构 / JSON 提取 / 字段存在性（沿用原契约基础校验）。
2. `data.intents` 每条：`from` ⊆ 当前合法 fact id 且不含 `goal`；`description` 非空；**`coverage_item_ids` 非空且 ⊆ 当前 engagement 未覆盖覆盖项**（`COVERAGE_ALREADY_COVERED` 失败）。
3. **收敛硬约束**：若存在 `priority ≥ policy.min_priority_threshold` 的缺口，则 `intents` 非空 **或** `coverage.recommend_finalize=true`；两者都缺 → 任务失败（等价原"open_intents 空必须出 intent"）。
4. `coverage.waivers` 只作为**建议**提交人工；Agent 不能直接豁免（写库需人工接口）。
5. `data.complete` **不再接受**（渗透场景废除 goal 达成判定）——出现即 `VALIDATION` 失败。
6. **C8 空转保护**：连续 `reason_escalation.max_consecutive_failures` 次校验失败（无 intent 也无 finalize）、或连续 `max_finalize_rejected` 次 `recommend_finalize` 被人工拒绝 → reason 升级 `needs_review`，**停止自动重试**，仅人工介入后可恢复。计数落 `scheduler_state`（重启不丢）。

**格子互斥（B1，explore 校验前置）**：派发前 `claim_item_for_intent` 认领格子；写回校验 `current_intent_id ∈ {本次 intent, NULL}`；并发下他格被认领是**预期分支**（写回作废 + release，下轮重排），不是事故。

### 3.2 explore 输出契约（覆盖项驱动 + 漏洞）

```json
{
  "accepted": true,
  "data": {
    "description": "对 /admin 登录框测试弱口令，确认默认口令 admin:admin 可直接登录",
    "findings": [
      {
        "title": "后台默认口令",
        "severity": "high",
        "cvss_score": 8.1,
        "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
        "cwe_id": "CWE-521",
        "category": "auth",
        "asset": "http://10.0.0.5:8080/admin",
        "description": "使用 admin:admin 可直接登录后台",
        "remediation": "强制修改默认口令并启用强口令策略",
        "evidence_refs": ["e-001/screenshot.png", "e-001/command.log"],
        "http": [                                    // 可选：Web 类漏洞必备的触发请求/响应包
          {
            "method": "POST",
            "url": "http://10.0.0.5:8080/admin/login",
            "request_headers": "Host: 10.0.0.5:8080\nContent-Type: application/x-www-form-urlencoded\nCookie: JSESSIONID=...",
            "request_body": "user=admin&pass=admin",
            "response_status": 302,
            "response_headers": "Location: /admin/dashboard\nContent-Type: text/html",
            "response_body": "<!DOCTYPE html>... (截断)",
            "note": "触发弱口令登录"
          }
        ]
      }
    ],
    "coverage": {
      "covered_items": ["c-013"],
      "depth_achieved": "standard",
      "outcome": "finding_created",
      "tested_scope": {                     // C9：声明实际覆盖的具体范围（防"部分覆盖当全绿"）
        "endpoints": ["/admin/login", "/search?q="],
        "params": ["user", "q"],
        "partial": false                     // true=仅覆盖部分端点/参数（热力图半色，不算充分覆盖）
      }
    }
  }
}
```

**explore 校验器规则**：
1. `description` 非空（落图事实，沿用）。
2. **`coverage` 必填**：`covered_items` 非空且 ⊆ 当前 engagement 覆盖项；引用的覆盖项**必须由本次 intent 认领**（B1：`current_intent_id == 本次 intent`；未认领格写回被拒——复测格经 rebuild 清空后由 retest explore 重新 claim）；`depth_achieved`、`outcome` ∈ 枚举；若 `findings` 非空则 `outcome` 应为 `finding_created`（交叉校验）。
   **C9 充分性**：`tested_scope` 建议必填——`outcome=no_issue` 但仅覆盖部分端点/参数（或 `tested_scope.partial=true`）时，覆盖项写为**部分覆盖**（`coverage_records.partial=1`），不算充分覆盖、不进"全绿"，reason 仍可将其列低优先级补测。
   **not_applicable 语义（B4 + §6.2）**：`outcome=not_applicable` 只写 `coverage_records`，**不置** `coverage_items.status='not_applicable'`（item 保持 untested，reason 仍可见为低优先缺口）；item 的 `status='not_applicable'` 仅由人工建 `waivers(kind='not_applicable')` 后置，杜绝「Agent 建议 N/A 绕过应测尽测」。
3. `findings[]`（可选）：`severity` ∈ {critical,high,medium,low,info}；`cvss_score` ∈ [0,10]；`cwe_id` 格式 `CWE-\d+`；`asset` 非空；**`evidence_refs` 用相对路径**（容器内写入 `/home/worker/evidence/<rel>`，服务端解析为 `evidence_root/{engagement_id}/<rel>` 且文件必须存在，防越权/穿越）。
4. `findings[].http[]`（可选，Web 类漏洞建议携带）：`method` ∈ HTTP 方法枚举；`url` 必须为绝对 URL；`response_status` ∈ [100,599]；`request_headers/response_headers/body` 长度上限（默认 64KB，超出截断并在 `note` 标注）；**写库到 `finding_http_evidence`**（与 finding 同事务）。
5. 校验通过后：写 fact（conclude）+ 写 `coverage_records` + 更新 `coverage_items.status` + findings 去重落库 + http 证据 —— **同事务**。
   6. **写回幂等**：`coverage_records` 以 `(item_id, intent_id)` + 请求幂等键去重（配 `writeback_retries=1` 重试），防止「服务端已成功、Dispatcher 超时重发」产生重复结论记录。
6. conclude 阶段（双阶段收尾）契约与 execute 相同，可携带 findings + coverage + http。

### 3.3 bootstrap 输出契约（攻击面发现 + 播种）

```json
{
  "accepted": true,
  "data": {
    "fact": { "description": "扫描发现 10.0.0.5 开放 22/80/8080，80 为 nginx，8080 为 Tomcat" },
    "complete": { "description": "目标集初探完成，攻击面已落图" },
    "discoveries": [
      { "target": "10.0.0.5", "port": 80,  "service": "http" },
      { "target": "10.0.0.5", "port": 8080, "service": "tomcat" }
    ],
    "coverage": { "outcome": "no_issue" }
  }
}
```

- `discoveries` 触发 `seed_from_discovery` 播种覆盖项；
- `complete` 语义为"初探完成"（不是"项目完成"），Engagement 仍按覆盖收敛推进。

---

## 4. 前端覆盖热力图交互设计（Coverage Heatmap）

### 4.1 数据契约

`GET /engagements/{id}/coverage` 返回：

```json
{
  "targets": [
    { "id": "t1", "value": "10.0.0.5", "criticality": 0.8 }
  ],
  "test_types": [
    { "id": "tt_web_sqli", "name": "SQL注入", "category": "webapp", "risk": 0.9 }
  ],
  "cells": [
    {
      "item_id": "c-013", "target_id": "t1", "test_type_id": "tt_web_sqli",
      "status": "untested", "priority": 0.72, "depth_required": "standard",
      "last_result": null, "tested_at": null,
      "partial": false, "retest_round": 0        // C9/A5：部分覆盖标记 / 复测轮次徽标
    }
  ],
  "summary": { "total": 40, "covered": 22, "coverage_ratio": 0.55,
               "untested": 15, "in_progress": 1, "not_applicable": 1,
               "waived": 1, "with_finding": 4, "partial": 2 }
}
```

### 4.2 渲染（CSS Grid / 表格，500ms 无交互时自适应列宽）

- 行 = targets（含 criticality 徽标）；列 = test_types（按 category 分组表头）
- 单元格状态色：

| status | 颜色 | 视觉 |
|---|---|---|
| untested 且 priority ≥ 阈值 | 红 `#fee2e2` | 实心，右上角优先级数字 |
| untested 低优先 | 橙 `#ffedd5` | 淡 |
| in_progress | 琥珀 `#fde68a` | 呼吸闪烁动画 |
| tested_no_issue | 绿 `#dcfce7` | ✓ |
| tested_no_issue 但 partial | 黄绿 `#fef9c3` | ✓ + ⚠ 角标（C9：部分覆盖，不算充分覆盖） |
| tested_with_finding | 玫红 `#fecdd3` | 圆点 ●（点击直达 findings） |
| not_applicable / waived | 灰 `#e2e8f0` | ⓘ（tooltip 显示理由） |

- 顶栏：覆盖率进度条（covered/total）+ 缺口计数 + 「finalize」按钮（`report_ready=false` 时禁用，tooltip 列出未达标项）

### 4.3 交互

1. **点单元格** → 右侧抽屉：
   - 覆盖项详情（target/test_type/depth/priority）
   - `coverage_records` 历史（时间线：每次测试的 depth/outcome/fact/intent）
   - 证据链接（evidence_refs，点击在新标签预览）
   - 操作按钮：**豁免**（选 kind + 必填 reason）、**标记不适用**、**调整深度**、**强制校准**（人工改状态）
   - 若 `tested_with_finding` → 直达关联 finding
2. **过滤**：状态 / category / 优先级阈值滑块（拖动重算 `compute_gaps`）
3. **分组折叠**：按 target 或 category 折叠；全灰列（该测试项全部豁免）自动折叠提示
4. **复测联动（A5）**：finding 置 `fixed` → 对应覆盖项**原格重置为 `untested`**（`retest_round+1` 徽标 + 高亮；UNIQUE 约束下不复建新 item），`coverage_records` 保留复测前后历史，热力图实时反映
5. **轮询**：沿用前端 5s 轮询，增量更新（服务端返回 `updated_at`，仅重拉变化）

### 4.4 前端状态机（cell 本地乐观更新）

```
untested ──dispatch intent──▶ in_progress（本地立即置，后台核对）
in_progress ──coverage 写回──▶ tested_no_issue / tested_with_finding / not_applicable
任意 ──人工豁免──▶ waived（写 waivers）
任意 ──复测重建──▶ 原格重置 untested + retest_round+1（finding fixed 触发；A5 不复建新 item）
```

---

## 5. 验收要点（新平台覆盖度引擎合入条件）

1. `compute_gaps` 与热力图数据一致（同一查询路径，杜绝两套口径）。
2. reason 校验器强制"覆盖未满不得重测"，`COVERAGE_ALREADY_COVERED` 可被单测覆盖。
3. explore 无 `coverage` 块 → 校验失败；conclude 阶段同样校验。
4. finalize 在覆盖未达标时返回 `COVERAGE_POLICY_UNMET`，且豁免可绕过但必须留 reason。
5. `coverage_items` 写入与 `coverage_records` 同事务，崩溃不产生半状态。
6. Mock 驱动扩展：mock 的 explore 输出模板加入 `coverage_result` 与 `findings`，保证全链路回归覆盖新契约。
7. **F11 闭环**：auto_created 目标新增后 report_ready 覆盖率分母/深度校验不受影响；该目标项仍在热力图可见。
8. **F3 抽样复核**：`audit_runs` 落库；`coverage_discrepancy` → 覆盖项回退 `untested` + 缺口重排；discrepancy 触发（声称 finding_created 但无 finding）能被单测覆盖。
9. 前端热力图标注被审计打回的格子（`last_result='audit_discrepancy'` 显示 ⚠）。
10. **A3 口径统一**：`pick_audit_targets` 与 `compute_gaps` 用同一 `priority_score()` 实时计算（不读缓存列），单测覆盖"缓存列变化不影响排序"。
11. **A5 复测重建**：`rebuild_for_retest` 复用原行（`retest_round+1` + 状态重置），不产生 UNIQUE 冲突；`coverage_records` 保留复测前后历史。
12. **B1 格子互斥**：两 intent 并发认领同一格时第二个 `claim_item_for_intent` 返回 False 不派发；explore 写回遇他人认领 → `COVERAGE_ALREADY_COVERED` 作废 + release，无幽灵任务。
13. **C8 reason 升级**：连续失败/连续 finalize 被拒超限 → 升级 `needs_review`，reason 不再自动重试（计数落 `scheduler_state`）。
14. **C9 部分覆盖**：`tested_scope.partial=true` 的格热力图半色 + 摘要 `partial` 计数正确；`outcome=no_issue` 但未声明 tested_scope 的写回被要求补注。
15. **D5 criticality**：`infer_criticality` 按资产类型/核心服务推断，人工覆盖生效。
