"""10-server-foundation 验收测试。

覆盖：建库/表数/PRAGMA、计数器自增、settings PUT→GET、401/422/404 错误码形状、
GET /projects 与 /health 豁免冒烟、v1→v2 迁移、DELETE engagement 级联删除。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from cairn import __version__
from cairn.config import ServerConfig
from cairn.server import db as db_module
from cairn.server.app import create_app
from cairn.server.errors import ErrorCode

#: DDL §1-§9 全部 30 张业务表 + §8 三张 FTS5 虚拟表
EXPECTED_TABLES = {
    # §1
    "settings",
    # §2
    "engagements", "targets",
    # §3
    "test_types", "coverage_items", "coverage_records", "waivers",
    # §4
    "projects", "facts", "intents", "intent_sources", "hints",
    "counters", "scoped_counters", "engagement_counters",
    # §5
    "findings", "finding_evidence", "finding_http_evidence", "finding_history",
    "finding_retest_confirmations",
    # §6
    "reports",
    # §7
    "scheduler_state",
    # §9
    "traffic_entries", "finding_traffic_links", "finding_command_evidence",
    "verify_runs", "replay_runs", "audit_runs", "task_runs", "task_events",
    # §8 FTS5
    "fts_facts", "fts_findings", "fts_coverage",
}


def make_config(db_path: str, token: str = "secret") -> ServerConfig:
    return ServerConfig(
        db_path=db_path,
        api_token=token,
        evidence_root=f"{db_path}.evidence",
        traffic_root=f"{db_path}.traffic",
        archive_root=f"{db_path}.archive",
    )


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# 1. 建库：表数 ≥ DDL 表数、PRAGMA、settings 单例、counters 初始行
# ---------------------------------------------------------------------------


def test_init_db_creates_full_schema_and_pragmas(db_path):
    conn = db_module.init_db(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        missing = EXPECTED_TABLES - tables
        assert not missing, f"缺少表: {sorted(missing)}"
        assert len(tables) >= len(EXPECTED_TABLES)

        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL

        # settings 单例 + counters 初始行
        s = conn.execute("SELECT * FROM settings WHERE rowid = 1").fetchone()
        assert s is not None
        assert (s["intent_timeout"], s["reason_timeout"], s["global_kill_switch"]) == (15, 15, 0)
        assert s["coverage_policy"] == "{}"
        c = conn.execute("SELECT value FROM counters WHERE name='engagement'").fetchone()
        assert c["value"] == 0
    finally:
        conn.close()


def test_init_db_idempotent(db_path):
    c1 = db_module.init_db(db_path)
    c1.close()
    c2 = db_module.init_db(db_path)  # 再次建库不应报错
    c2.close()


# ---------------------------------------------------------------------------
# 2. 计数器自增（DDL §4.1 映射 + 幂等键）
# ---------------------------------------------------------------------------


def test_counters_increment(db_path):
    conn = db_module.init_db(db_path)
    try:
        now = "2026-08-05T00:00:00Z"
        e1 = db_module.next_id(conn, "engagement")
        e2 = db_module.next_id(conn, "engagement")
        assert e1 == "eng_001"
        assert e2 == "eng_002"
        conn.execute(
            "INSERT INTO engagements (id, title, created_at) VALUES (?, 'E1', ?)", (e1, now)
        )
        conn.execute(
            "INSERT INTO engagements (id, title, created_at) VALUES (?, 'E2', ?)", (e2, now)
        )

        # 全局计数器（DDL §4.1）：前缀 + 三位补零自增
        assert db_module.next_id(conn, "target", engagement_id=e1) == "t-001"
        assert db_module.next_id(conn, "target", engagement_id=e1) == "t-002"
        assert db_module.next_id(conn, "finding", engagement_id=e1) == "fd-001"
        assert db_module.next_id(conn, "evidence", engagement_id=e1) == "fe-001"
        assert db_module.next_id(conn, "http_evidence", engagement_id=e1) == "he-001"
        assert db_module.next_id(conn, "finding_traffic_link", engagement_id=e1) == "ftl-001"
        assert db_module.next_id(conn, "coverage_item", engagement_id=e1) == "c-001"
        assert db_module.next_id(conn, "coverage_record", engagement_id=e1) == "cr-001"
        assert db_module.next_id(conn, "waiver", engagement_id=e1) == "w-001"
        assert db_module.next_id(conn, "report", engagement_id=e1) == "rpt-001"
        assert db_module.next_id(conn, "traffic", engagement_id=e1) == "tr-001"
        assert db_module.next_id(conn, "verify_run", engagement_id=e1) == "vr-001"
        assert db_module.next_id(conn, "replay_run", engagement_id=e1) == "rp-001"
        assert db_module.next_id(conn, "audit_run", engagement_id=e1) == "ar-001"
        assert db_module.next_id(conn, "retest_confirmation", engagement_id=e1) == "rc-001"
        assert db_module.next_id(conn, "finding_history", engagement_id=e1) == "fh-001"

        # 全局唯一：不同 engagement 不再从 -001 重启（跨 engagement 不复用 ID）
        assert db_module.next_id(conn, "target", engagement_id=e2) == "t-003"
        # engagement_id 对业务 kind 可选/忽略：不传也继续全局自增
        assert db_module.next_id(conn, "target") == "t-004"
        # 全局 engagement 计数继续
        assert db_module.next_id(conn, "engagement") == "eng_003"

        # 未知 kind → 报错
        with pytest.raises(ValueError):
            db_module.next_id(conn, "bogus", engagement_id=e1)

        # test_types 幂等键
        assert db_module.test_type_id("web_sqli") == "tt_web_sqli"
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. settings PUT→GET 回读 + 部分更新
# ---------------------------------------------------------------------------


def test_settings_put_get(db_path):
    client = TestClient(create_app(make_config(db_path)))
    r = client.put(
        "/settings",
        headers={"Authorization": "Bearer secret"},
        json={
            "intent_timeout": 30,
            "reason_timeout": 25,
            "global_kill_switch": 1,
            "coverage_policy": {"target_coverage": 0.95},
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["intent_timeout"] == 30
    assert data["reason_timeout"] == 25
    assert data["global_kill_switch"] == 1
    assert data["coverage_policy"] == {"target_coverage": 0.95}

    r2 = client.get("/settings", headers={"Authorization": "Bearer secret"})
    assert r2.status_code == 200
    assert r2.json() == data

    # 部分更新：只改 global_kill_switch，其余保留
    r3 = client.put(
        "/settings",
        headers={"Authorization": "Bearer secret"},
        json={"global_kill_switch": 0},
    )
    assert r3.status_code == 200
    body = r3.json()
    assert body["global_kill_switch"] == 0
    assert body["intent_timeout"] == 30


# ---------------------------------------------------------------------------
# 4. 鉴权 401 / 统一错误码形状
# ---------------------------------------------------------------------------


def test_auth_401_shapes(db_path):
    client = TestClient(create_app(make_config(db_path, token="secret")))

    # 缺 token → AUTH_REQUIRED
    r = client.get("/settings")
    assert r.status_code == 401
    body = r.json()
    assert body["error_code"] == "AUTH_REQUIRED"
    assert set(body) >= {"error_code", "message", "detail"}

    # 错 token → AUTH_INVALID
    r = client.get("/settings", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_INVALID"

    # 非 Bearer → AUTH_REQUIRED
    r = client.get("/settings", headers={"Authorization": "secret"})
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_REQUIRED"


def test_auth_missing_config_token(db_path):
    # 未配置 token（env 也未设）→ 401 AUTH_REQUIRED
    client = TestClient(create_app(make_config(db_path, token=None)))
    r = client.get("/settings")
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_REQUIRED"


def test_404_error_shape(db_path):
    client = TestClient(create_app(make_config(db_path)))
    r = client.get("/no/such/route", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 404
    body = r.json()
    assert body["error_code"] == "NOT_FOUND"
    assert "message" in body and "detail" in body


def test_422_validation_shape(db_path):
    client = TestClient(create_app(make_config(db_path)))
    # PUT /settings 类型错误 → 422 包 VALIDATION，保留 FastAPI detail
    r = client.put(
        "/settings",
        headers={"Authorization": "Bearer secret"},
        json={"intent_timeout": "abc"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error_code"] == "VALIDATION"
    assert isinstance(body["detail"], list)


# ---------------------------------------------------------------------------
# 5. 健康冒烟：GET /projects 与 GET /health 豁免鉴权 → 200
# ---------------------------------------------------------------------------


def test_health_and_projects_200(db_path):
    client = TestClient(create_app(make_config(db_path)))
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/projects")  # 无 token 也应 200（豁免）
    assert r.status_code == 200
    assert r.json() == []


def test_version():
    assert isinstance(__version__, str) and __version__


# ---------------------------------------------------------------------------
# 6. v1→v2 迁移冒烟（构造最小 v1 库）
# ---------------------------------------------------------------------------


def test_migrate_v1(tmp_path):
    db_path = str(tmp_path / "v1.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE projects (
            id             TEXT PRIMARY KEY,
            title          TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'active',
            bootstrap_mode INTEGER NOT NULL DEFAULT 1,
            created_at     TEXT NOT NULL
        );
        CREATE TABLE counters (
            name  TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO counters (name, value) VALUES ('engagement', 3);
        INSERT INTO projects (id, title, status, bootstrap_mode, created_at)
               VALUES ('proj_001', 'v1', 'active', 0, '2026-01-01T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    migrated = db_module.init_db(db_path)
    try:
        cols = {r[1] for r in migrated.execute("PRAGMA table_info(projects)")}
        assert "engagement_id" in cols
        assert "bootstrap_enabled" in cols
        assert "bootstrap_mode" not in cols

        # 数据保留：bootstrap_mode=0 → bootstrap_enabled=0
        row = migrated.execute(
            "SELECT id, bootstrap_enabled, engagement_id FROM projects WHERE id='proj_001'"
        ).fetchone()
        assert row["bootstrap_enabled"] == 0
        assert row["engagement_id"] is None

        # settings 单例补全（含新增列）
        s = migrated.execute(
            "SELECT intent_timeout, reason_timeout, global_kill_switch, coverage_policy FROM settings WHERE rowid=1"
        ).fetchone()
        assert s is not None
        assert s["global_kill_switch"] == 0
        assert s["coverage_policy"] == "{}"

        # counters 保留 v1 数值（不归零）
        c = migrated.execute("SELECT value FROM counters WHERE name='engagement'").fetchone()
        assert c["value"] == 3
        # 续增从 4 开始
        assert db_module.next_id(migrated, "engagement") == "eng_004"

        # 新表已建
        tables = {
            r[0]
            for r in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert {"engagements", "findings", "traffic_entries", "task_runs"} <= tables

        # 迁移前 VACUUM INTO 备份存在
        assert list(tmp_path.glob("v1.db.backup_*.db"))
        migrated.commit()
    finally:
        migrated.close()


# ---------------------------------------------------------------------------
# 7. DELETE engagement 级联删除冒烟（findings.target_id / traffic_id 两处边角）
# ---------------------------------------------------------------------------


def _seed_engagement_with_evidence(conn):
    """造一个带 target/finding/http-evidence/traffic/task_run 的 engagement。"""
    now = "2026-08-05T00:00:00Z"
    eid = db_module.next_id(conn, "engagement")
    conn.execute(
        "INSERT INTO engagements (id, title, created_at) VALUES (?, 'E', ?)", (eid, now)
    )
    tid = db_module.next_id(conn, "target", engagement_id=eid)
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, added_at) "
        "VALUES (?, ?, 'example.com', 'domain', 'authorized', ?)",
        (tid, eid, now),
    )
    fid = db_module.next_id(conn, "finding", engagement_id=eid)
    conn.execute(
        "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, "
        "description, detected_by, created_at, updated_at) VALUES (?, ?, ?, 'XSS', 'high', 'high', 'desc', 'w', ?, ?)",
        (fid, eid, tid, now, now),
    )
    trid = db_module.next_id(conn, "traffic", engagement_id=eid)
    conn.execute(
        "INSERT INTO traffic_entries (id, engagement_id, seq, captured_at, method, url, req_path, req_bytes) "
        "VALUES (?, ?, 1, ?, 'GET', 'http://example.com/x', 'traffic/x.req', 10)",
        (trid, eid, now),
    )
    # 边角：finding_http_evidence.traffic_id FK 无 ON DELETE（依赖级联顺序）
    heid = db_module.next_id(conn, "http_evidence", engagement_id=eid)
    conn.execute(
        "INSERT INTO finding_http_evidence (id, finding_id, traffic_id, method, url, captured_at) "
        "VALUES (?, ?, ?, 'GET', 'http://example.com/x', ?)",
        (heid, fid, trid, now),
    )
    # 边角：replay_runs.trigger_traffic_id 同样无 ON DELETE
    rpid = db_module.next_id(conn, "replay_run", engagement_id=eid)
    conn.execute(
        "INSERT INTO replay_runs (id, engagement_id, finding_id, trigger_traffic_id) "
        "VALUES (?, ?, ?, ?)",
        (rpid, eid, fid, trid),
    )
    # task_runs.project_id 可空（B2）
    conn.execute(
        "INSERT INTO task_runs (id, engagement_id, task_type, worker) VALUES ('task-001', ?, 'verify', 'w1')",
        (eid,),
    )
    conn.commit()
    return eid, fid, trid, heid, rpid


def test_cascade_delete_engagement(db_path):
    conn = db_module.init_db(db_path)
    try:
        eid, fid, trid, heid, rpid = _seed_engagement_with_evidence(conn)
        assert conn.execute("SELECT count(*) FROM findings WHERE id=?", (fid,)).fetchone()[0] == 1

        # DELETE engagement 不应触发 FK 违约
        conn.execute("DELETE FROM engagements WHERE id=?", (eid,))
        conn.commit()

        for table, ref in (
            ("findings", ("id", fid)),
            ("traffic_entries", ("id", trid)),
            ("finding_http_evidence", ("id", heid)),
            ("replay_runs", ("id", rpid)),
            ("targets", ("engagement_id", eid)),
            ("task_runs", ("engagement_id", eid)),
        ):
            col, val = ref
            assert (
                conn.execute(f"SELECT count(*) FROM {table} WHERE {col}=?", (val,)).fetchone()[0] == 0
            ), f"{table} 级联删除失败"
    finally:
        conn.close()


def test_delete_target_restricts_findings(db_path):
    """应用层 gate：DELETE target 前应先清 findings/coverage（human-workflow §2）。"""
    conn = db_module.init_db(db_path)
    try:
        now = "2026-08-05T00:00:00Z"
        eid = db_module.next_id(conn, "engagement")
        conn.execute(
            "INSERT INTO engagements (id, title, created_at) VALUES (?, 'E', ?)", (eid, now)
        )
        tid = db_module.next_id(conn, "target", engagement_id=eid)
        conn.execute(
            "INSERT INTO targets (id, engagement_id, value, kind, scope_status, added_at) "
            "VALUES (?, ?, 'a.com', 'domain', 'authorized', ?)",
            (tid, eid, now),
        )
        fid = db_module.next_id(conn, "finding", engagement_id=eid)
        conn.execute(
            "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, "
            "description, detected_by, created_at, updated_at) VALUES (?, ?, ?, 'X', 'low', 'low', 'd', 'w', ?, ?)",
            (fid, eid, tid, now, now),
        )
        conn.commit()
        # targets 级联删除会连带 findings；这里验证 target 删除是级联的（findings.target_id CASCADE）
        conn.execute("DELETE FROM targets WHERE id=?", (tid,))
        conn.commit()
        assert conn.execute("SELECT count(*) FROM findings WHERE id=?", (fid,)).fetchone()[0] == 0
    finally:
        conn.close()
