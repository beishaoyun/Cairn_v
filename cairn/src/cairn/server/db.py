"""连接管理 + 全量 DDL + v1→v2 迁移 + 统一 ID 计数器（Agent 10 所有，唯一落库者）。

权威依据：``docs/database-ddl-draft.md`` 全文。本文件逐表逐条转写 DDL §1-§9，
含所有 CHECK/UNIQUE/FK/CASCADE 与 FTS5 虚拟表。**表创建顺序不可改动**——
级联删除依赖子表创建先后（DDL §5/§9.1 注释：findings 先于 traffic_entries 创建，
DELETE engagement 时 finding 系经 findings 级联先行删除，再删 traffic_entries）。

索引与 FTS5 单独放在末尾（DDL §10 step7：老库补列之后再建），否则 v1 老库在
``ALTER TABLE projects ADD COLUMN engagement_id`` 前建 ``idx_projects_eng`` 会报
「no such column: engagement_id」。
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator

from fastapi import Request

# ---------------------------------------------------------------------------
# 连接与 PRAGMA（DDL §0）
# ---------------------------------------------------------------------------


def connect(path: str) -> sqlite3.Connection:
    """打开连接并施加 DDL §0 PRAGMA：WAL / foreign_keys / busy_timeout / synchronous。"""
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：请求级短连接（开即用、用完关）。"""
    conn = connect(request.app.state.config.db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 全量 DDL（§1-§9 建表部分；索引与 FTS5 在 SCHEMA_INDEXES_DDL，末尾执行）
# ---------------------------------------------------------------------------

SCHEMA_TABLES_DDL = """
-- §1 全局配置（单例 rowid=1）
CREATE TABLE IF NOT EXISTS settings (
    rowid            INTEGER PRIMARY KEY CHECK (rowid = 1),
    intent_timeout   INTEGER NOT NULL DEFAULT 15,
    reason_timeout   INTEGER NOT NULL DEFAULT 15,
    global_kill_switch INTEGER NOT NULL DEFAULT 0,
    coverage_policy  TEXT NOT NULL DEFAULT '{}'
);

-- §2 授权范围子域
CREATE TABLE IF NOT EXISTS engagements (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'planning'
                        CHECK (status IN ('planning','active','paused','completed','archived')),
    authorized_start_at TEXT,
    authorized_end_at   TEXT,
    scope_policy        TEXT NOT NULL DEFAULT '{}',
    kill_switch         INTEGER NOT NULL DEFAULT 0,
    created_by          TEXT NOT NULL DEFAULT 'human',
    created_at          TEXT NOT NULL,
    completed_at        TEXT
);

CREATE TABLE IF NOT EXISTS targets (
    id             TEXT PRIMARY KEY,
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    value          TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('domain','ip','cidr','url','hostname')),
    scope_status   TEXT NOT NULL CHECK (scope_status IN ('authorized','prohibited')),
    criticality    REAL NOT NULL DEFAULT 0.5 CHECK (criticality BETWEEN 0 AND 1),
    auto_created   INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    added_by       TEXT NOT NULL DEFAULT 'human',
    added_at       TEXT NOT NULL,
    UNIQUE (engagement_id, value)
);

-- §3 覆盖度子域
CREATE TABLE IF NOT EXISTS test_types (
    id             TEXT PRIMARY KEY,
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
    id             TEXT PRIMARY KEY,
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
    retest_round   INTEGER NOT NULL DEFAULT 0,
    current_intent_id TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (engagement_id, target_id, test_type_id)
);

CREATE TABLE IF NOT EXISTS coverage_records (
    id             TEXT PRIMARY KEY,
    item_id        TEXT NOT NULL REFERENCES coverage_items(id) ON DELETE CASCADE,
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    depth_achieved TEXT NOT NULL CHECK (depth_achieved IN ('baseline','standard','deep')),
    outcome        TEXT NOT NULL CHECK (outcome IN ('no_issue','finding_created','not_applicable')),
    source_fact_id TEXT,
    intent_id      TEXT,
    evidence_refs  TEXT,
    tested_scope   TEXT,
    partial        INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS waivers (
    id            TEXT PRIMARY KEY,
    item_id       TEXT NOT NULL REFERENCES coverage_items(id) ON DELETE CASCADE,
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('not_applicable','out_of_scope','risk_accepted')),
    reason        TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- §4 探索图子域
CREATE TABLE IF NOT EXISTS projects (
    id                  TEXT PRIMARY KEY,
    engagement_id       TEXT REFERENCES engagements(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','stopped')),
    bootstrap_enabled   INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    reason_worker       TEXT,
    reason_trigger      TEXT,
    reason_started_at   TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id          TEXT NOT NULL,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    value      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

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

-- §5 漏洞子域
CREATE TABLE IF NOT EXISTS findings (
    id               TEXT PRIMARY KEY,
    engagement_id    TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    target_id        TEXT NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('critical','high','medium','low','info')),
    agent_severity   TEXT NOT NULL CHECK (agent_severity IN ('critical','high','medium','low','info')),
    verified_severity TEXT CHECK (verified_severity IN ('critical','high','medium','low','info')),
    verify_status    TEXT NOT NULL DEFAULT 'none'
                     CHECK (verify_status IN ('none','pending','confirmed','rejected')),
    retest_pass      INTEGER NOT NULL DEFAULT 0,
    retest_round     INTEGER NOT NULL DEFAULT 0,
    reverify_count   INTEGER NOT NULL DEFAULT 0,
    cvss_score       REAL CHECK (cvss_score BETWEEN 0 AND 10),
    cvss_vector      TEXT,
    cwe_id           TEXT,
    category         TEXT,
    status           TEXT NOT NULL DEFAULT 'open'
                     CHECK (status IN ('open','pending_verify','pending_false_positive','verified','needs_review','fixed','false_positive','accepted','closed')),
    description      TEXT NOT NULL,
    remediation      TEXT,
    references_      TEXT,
    detected_by      TEXT NOT NULL,
    source_fact_id   TEXT,
    coverage_item_id TEXT,
    evidence_summary TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    fixed_at         TEXT,
    closed_at        TEXT
);

CREATE TABLE IF NOT EXISTS finding_evidence (
    id          TEXT PRIMARY KEY,
    finding_id  TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('screenshot','file','command_log','raw')),
    path        TEXT NOT NULL,
    mime        TEXT,
    size        INTEGER,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding_http_evidence (
    id               TEXT PRIMARY KEY,
    finding_id       TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL DEFAULT 1,
    traffic_id       TEXT REFERENCES traffic_entries(id),
    source           TEXT NOT NULL DEFAULT 'captured'
                     CHECK (source IN ('captured','agent_typed')),
    method           TEXT NOT NULL,
    url              TEXT NOT NULL,
    request_headers  TEXT,
    request_body     TEXT,
    response_status  INTEGER,
    response_headers TEXT,
    response_body    TEXT,
    note             TEXT,
    captured_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding_history (
    id           TEXT PRIMARY KEY,
    finding_id   TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    note         TEXT,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding_retest_confirmations (
    id           TEXT PRIMARY KEY,
    finding_id   TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    retest_round INTEGER NOT NULL DEFAULT 0,
    kind         TEXT NOT NULL CHECK (kind IN ('replay','verify','human')),
    note         TEXT,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (finding_id, retest_round, kind)
);

-- §6 交付子域
CREATE TABLE IF NOT EXISTS reports (
    id            TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    format        TEXT NOT NULL CHECK (format IN ('markdown','html','pdf')),
    path          TEXT NOT NULL,
    generated_by  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- §7 调度状态子域
CREATE TABLE IF NOT EXISTS scheduler_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- §9.1 捕获流量
CREATE TABLE IF NOT EXISTS traffic_entries (
    id             TEXT PRIMARY KEY,
    engagement_id  TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    captured_at    TEXT NOT NULL,
    method         TEXT NOT NULL,
    url            TEXT NOT NULL,
    host           TEXT,
    client         TEXT,
    client_ip      TEXT,
    status         INTEGER,
    req_path       TEXT NOT NULL,
    resp_path      TEXT,
    req_bytes      INTEGER NOT NULL,
    resp_bytes     INTEGER,
    content_type   TEXT,
    sha256         TEXT,
    chunk_count    INTEGER NOT NULL DEFAULT 1,
    archived       INTEGER NOT NULL DEFAULT 0,
    archived_path  TEXT,
    finding_linked INTEGER NOT NULL DEFAULT 0
);

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

CREATE TABLE IF NOT EXISTS finding_command_evidence (
    id          TEXT PRIMARY KEY,
    finding_id  TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    command     TEXT NOT NULL,
    cwd         TEXT,
    exit_code   INTEGER,
    stdout      TEXT,
    stderr      TEXT,
    started_at  TEXT,
    duration_ms INTEGER
);

-- §9.2 独立复核
CREATE TABLE IF NOT EXISTS verify_runs (
    id                    TEXT PRIMARY KEY,
    finding_id            TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    task_run_id           TEXT,
    stage                 TEXT NOT NULL DEFAULT 'blind'
                          CHECK (stage IN ('blind','comparison','escalated')),
    independence          TEXT NOT NULL DEFAULT 'none'
                          CHECK (independence IN ('cross_worker','cross_model','cross_run','human','none')),
    input_traffic_digest  TEXT,
    observations          TEXT,
    verdict               TEXT CHECK (verdict IN ('confirmed','rejected','needs_more_evidence')),
    verified_severity     TEXT CHECK (verified_severity IN ('critical','high','medium','low','info')),
    reason                TEXT,
    verified_traffic_ids  TEXT,
    suggested_action      TEXT,
    created_at            TEXT NOT NULL,
    finished_at           TEXT
);

-- §9.3 确定性重放
CREATE TABLE IF NOT EXISTS replay_runs (
    id                  TEXT PRIMARY KEY,
    engagement_id       TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    finding_id          TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    trigger_traffic_id  TEXT NOT NULL REFERENCES traffic_entries(id),
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','success','failed','blocked')),
    payload_variants    INTEGER NOT NULL DEFAULT 0,
    matched_original    INTEGER NOT NULL DEFAULT 0,
    result              TEXT CHECK (result IN ('unchanged','remediated','ambiguous','error')),
    evidence_traffic_id TEXT,
    started_at          TEXT,
    finished_at         TEXT
);

-- §9.4 覆盖抽样复核
CREATE TABLE IF NOT EXISTS audit_runs (
    id               TEXT PRIMARY KEY,
    engagement_id    TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    coverage_item_id TEXT NOT NULL REFERENCES coverage_items(id) ON DELETE CASCADE,
    reason           TEXT NOT NULL CHECK (reason IN ('sampling','discrepancy','manual')),
    auditor          TEXT,
    verdict          TEXT CHECK (verdict IN ('covered_matches','coverage_discrepancy')),
    depth_reached    TEXT CHECK (depth_reached IN ('baseline','standard','deep')),
    note             TEXT,
    created_at       TEXT NOT NULL,
    finished_at      TEXT
);

-- §9.5 进度监控
CREATE TABLE IF NOT EXISTS task_runs (
    id           TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    project_id   TEXT,
    task_type    TEXT NOT NULL,
    worker       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running'
                 CHECK (status IN ('queued','running','success','failed','cancelled','unhealthy','rejected')),
    started_at   TEXT,
    finished_at  TEXT,
    outcome_note TEXT
);

CREATE TABLE IF NOT EXISTS task_events (
    id          TEXT PRIMARY KEY,
    task_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('step','tool','command','output','status','error')),
    level       TEXT NOT NULL DEFAULT 'info',
    message     TEXT,
    raw_path    TEXT,
    raw_offset  INTEGER
);
"""

#: 索引 + FTS5 虚拟表（DDL §8/§10 step7：迁移补列之后再建）
SCHEMA_INDEXES_DDL = """
CREATE INDEX IF NOT EXISTS idx_eng_status ON engagements(status);
CREATE INDEX IF NOT EXISTS idx_targets_eng_scope ON targets(engagement_id, scope_status);
CREATE INDEX IF NOT EXISTS idx_cov_eng_status ON coverage_items(engagement_id, status);
CREATE INDEX IF NOT EXISTS idx_cov_eng_prio   ON coverage_items(engagement_id, priority_score);
CREATE INDEX IF NOT EXISTS idx_cov_eng_intent ON coverage_items(current_intent_id);
CREATE INDEX IF NOT EXISTS idx_cov_rec_item ON coverage_records(item_id);
CREATE INDEX IF NOT EXISTS idx_waiver_item ON waivers(item_id);
CREATE INDEX IF NOT EXISTS idx_projects_eng ON projects(engagement_id);
CREATE INDEX IF NOT EXISTS idx_findings_eng_status ON findings(engagement_id, status);
CREATE INDEX IF NOT EXISTS idx_findings_eng_target ON findings(engagement_id, target_id);
CREATE INDEX IF NOT EXISTS idx_findings_title_hash  ON findings(engagement_id, target_id, title);
CREATE INDEX IF NOT EXISTS idx_http_ev_finding ON finding_http_evidence(finding_id);
CREATE INDEX IF NOT EXISTS idx_find_hist_finding ON finding_history(finding_id);
CREATE INDEX IF NOT EXISTS idx_frc_finding ON finding_retest_confirmations(finding_id);
CREATE INDEX IF NOT EXISTS idx_reports_eng ON reports(engagement_id);
CREATE INDEX IF NOT EXISTS idx_traffic_eng_url ON traffic_entries(engagement_id, url);
CREATE INDEX IF NOT EXISTS idx_traffic_eng_time ON traffic_entries(engagement_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_traffic_eng_client ON traffic_entries(engagement_id, client);
CREATE INDEX IF NOT EXISTS idx_ftl_finding ON finding_traffic_links(finding_id);
CREATE INDEX IF NOT EXISTS idx_fce_finding ON finding_command_evidence(finding_id);
CREATE INDEX IF NOT EXISTS idx_vr_finding ON verify_runs(finding_id);
CREATE INDEX IF NOT EXISTS idx_rp_finding ON replay_runs(finding_id);
CREATE INDEX IF NOT EXISTS idx_ar_item ON audit_runs(coverage_item_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_eng ON task_runs(engagement_id, task_type, status);
CREATE INDEX IF NOT EXISTS idx_task_events_run ON task_events(task_run_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_facts USING fts5(
    fact_id UNINDEXED, project_id UNINDEXED, description
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_findings USING fts5(
    finding_id UNINDEXED, engagement_id UNINDEXED, title, description, remediation
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts_coverage USING fts5(
    item_id UNINDEXED, target_value, test_type_name
);
"""


# ---------------------------------------------------------------------------
# 迁移（DDL §10：v1→v2）
# ---------------------------------------------------------------------------


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()
    return bool(row and row[0] > 0)


def _backup(conn: sqlite3.Connection, db_path: str) -> str:
    """迁移前备份：``VACUUM INTO 'backup_<ts>.db'``（DDL §10 step8）。"""
    ts = time.strftime("%Y%m%dT%H%M%S")
    backup_path = f"{db_path}.backup_{ts}.db"
    n = 0
    while os.path.exists(backup_path):
        n += 1
        backup_path = f"{db_path}.backup_{ts}_{n}.db"
    quoted = backup_path.replace("'", "''")
    conn.execute(f"VACUUM INTO '{quoted}'")
    return backup_path


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_legacy_columns(conn: sqlite3.Connection) -> None:
    """DDL §10 step2-4：老库补列（幂等；全新库为 no-op）。"""
    # step2：projects.engagement_id
    cols = _column_names(conn, "projects")
    if "engagement_id" not in cols:
        conn.execute(
            "ALTER TABLE projects ADD COLUMN engagement_id TEXT REFERENCES engagements(id) ON DELETE CASCADE"
        )
    # step3：bootstrap_mode → bootstrap_enabled（沿用 v1 _ensure_project_columns 思路）
    cols = _column_names(conn, "projects")
    if "bootstrap_enabled" not in cols:
        if "bootstrap_mode" in cols:
            conn.execute("ALTER TABLE projects RENAME COLUMN bootstrap_mode TO bootstrap_enabled")
        else:
            conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
    # step4：settings 补列（历史 settings 可能只有部分列）
    cols = _column_names(conn, "settings")
    for name, ddl in (
        ("intent_timeout", "INTEGER NOT NULL DEFAULT 15"),
        ("reason_timeout", "INTEGER NOT NULL DEFAULT 15"),
        ("global_kill_switch", "INTEGER NOT NULL DEFAULT 0"),
        ("coverage_policy", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE settings ADD COLUMN {name} {ddl}")


def _backfill(conn: sqlite3.Connection) -> None:
    """DDL §10 step6：空库回填 settings 单例 + counters.engagement 初始行。"""
    conn.execute(
        "INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout, global_kill_switch, coverage_policy) "
        "VALUES (1, 15, 15, 0, '{}')"
    )
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('engagement', 0)")


def init_db(path: str, *, migrate: bool = True) -> sqlite3.Connection:
    """建库 + 全量 DDL + 迁移（幂等）。返回已施加 PRAGMA 的连接，调用方负责关闭。

    老库存在用户表时，先 ``VACUUM INTO`` 备份再迁移；全新空库跳过备份。
    执行顺序：备份 → 建表（SCHEMA_TABLES_DDL）→ 补列（_migrate_legacy_columns）
    → 索引+FTS5（SCHEMA_INDEXES_DDL）→ 空库回填。
    """
    conn = connect(path)
    try:
        if migrate and _has_user_tables(conn):
            _backup(conn, path)
        conn.executescript(SCHEMA_TABLES_DDL)
        if migrate:
            _migrate_legacy_columns(conn)
        conn.executescript(SCHEMA_INDEXES_DDL)
        _backfill(conn)
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise


# ---------------------------------------------------------------------------
# 统一 ID 计数器（DDL §4.1；A1/A4）
# ---------------------------------------------------------------------------

#: 业务 kind → ID 前缀（DDL §4.1 映射表；全部走全局 ``counters`` 表 name=kind 自增）
ENGAGEMENT_KIND_PREFIX = {
    "finding": "fd",
    "evidence": "fe",
    "http_evidence": "he",
    "command_evidence": "ce",
    "finding_traffic_link": "ftl",
    "coverage_item": "c",
    "coverage_record": "cr",
    "waiver": "w",
    "report": "rpt",
    "traffic": "tr",
    "verify_run": "vr",
    "replay_run": "rp",
    "audit_run": "ar",
    "retest_confirmation": "rc",
    "target": "t",
    "finding_history": "fh",
}


def next_id(conn: sqlite3.Connection, kind: str, engagement_id: str | None = None) -> str:
    """统一 ID 生成（DDL §4.1）。

    - ``kind == 'engagement'``：全局 ``counters`` 表自增，返回 ``'eng_###'``；
    - 其余 16 个业务 kind：同样走全局 ``counters`` 表（name=kind）自增，返回
      ``'<prefix>-###'``，**跨 engagement 全局唯一**。

    ``engagement_id`` 参数对业务 kind **忽略**（签名保留兼容 20-24 旧调用
    ``next_id(conn, kind, engagement_id=eid)``；自增首次使用自动 ``INSERT OR
    IGNORE`` 播种 counters 行）。``engagement_counters`` 表保留仅兼容历史迁移，
    新代码不再写入。

    幂等键：``test_types`` 走 ``tt_<slug>``（见 :func:`test_type_id`）。
    """
    if kind == "engagement":
        conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('engagement', 0)")
        row = conn.execute(
            "UPDATE counters SET value = value + 1 WHERE name = 'engagement' RETURNING value"
        ).fetchone()
        if row is None:  # pragma: no cover —— INSERT OR IGNORE 后必然存在
            raise RuntimeError("counters.engagement 初始化失败")
        return f"eng_{row['value']:03d}"

    prefix = ENGAGEMENT_KIND_PREFIX.get(kind)
    if prefix is None:
        raise ValueError(f"未知计数器 kind: {kind!r}（非 engagement 全局，也不是业务 kind）")
    # 业务 kind 一律全局自增（counters 表 name=kind，跨 engagement 全局唯一）。
    # engagement_id 参数忽略（签名保留兼容旧调用 20-24）。
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES (?, 0)", (kind,))
    row = conn.execute(
        "UPDATE counters SET value = value + 1 WHERE name = ? RETURNING value",
        (kind,),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError(f"counters.{kind} 初始化失败")
    return f"{prefix}-{row['value']:03d}"


def test_type_id(slug: str) -> str:
    """test_types 幂等键：``tt_<slug>``（DDL §1）。"""
    return f"tt_{slug}"
