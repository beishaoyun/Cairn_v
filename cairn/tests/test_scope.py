"""Agent 20 · 授权范围子域验收测试（dev-agents/20-engagement-scope.md §3）。

覆盖五项验收：
1. 状态机全路径（合法/非法转换、completed→active 需 retest=true、archived 单向）；
2. 授权窗口到期自动 pause（expire_engagements 调用后 status 变化）；
3. scope guard（prohibited → SCOPE_DENIED 且无 fallback；auto_created target 已创建）；
4. targets 删除应用层 gate（被引用 → 409 列引用；未引用 → 删除成功）；
5. 创建 engagement 后 test_types 播种（21 就绪则断言行数 ≥1，否则注明跳过）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cairn.config import ServerConfig
from cairn.server import db as db_module
from cairn.server.app import create_app
from cairn.server.errors import ErrorCode
from cairn.server.services import scope as scope_svc


def _seed_ready() -> bool:
    try:
        from cairn.server.services.coverage import seed_default_test_types  # noqa: F401
        return True
    except ImportError:
        return False


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


@pytest.fixture()
def conn(db_path):
    c = db_module.init_db(db_path)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def client(db_path):
    return TestClient(create_app(make_config(db_path)))


def make_engagement(
    conn,
    title="E",
    start="2026-01-01T00:00:00Z",
    end="2026-12-31T00:00:00Z",
    **kw,
):
    return scope_svc.create_engagement(
        conn, title=title, window_start=start, window_end=end, **kw
    )


def add_target(conn, eid, value, scope_status="authorized", **kw):
    return scope_svc.create_target(
        conn, eid, value=value, scope_status=scope_status, **kw
    )


# ---------------------------------------------------------------------------
# 1. Engagement 创建与 ID 计数
# ---------------------------------------------------------------------------


def test_create_engagement_basic(conn):
    eng = make_engagement(conn)
    assert eng["id"] == "eng_001"
    assert eng["status"] == "planning"
    assert eng["title"] == "E"
    assert eng["kill_switch"] == 0
    assert eng["scope_policy"] == {}
    assert eng["completed_at"] is None
    assert eng["created_by"] == "human"

    eng2 = make_engagement(conn, title="E2")
    assert eng2["id"] == "eng_002"


def test_create_engagement_invalid_window(conn):
    with pytest.raises(Exception) as ei:
        make_engagement(conn, start="2026-12-31T00:00:00Z", end="2026-01-01T00:00:00Z")
    assert getattr(ei.value, "error_code", None) == ErrorCode.VALIDATION
    # 单端窗口同样非法
    with pytest.raises(Exception) as ei2:
        make_engagement(conn, start="2026-01-01T00:00:00Z", end=None)
    assert getattr(ei2.value, "error_code", None) == ErrorCode.VALIDATION


def test_create_engagement_empty_title(conn):
    with pytest.raises(Exception) as ei:
        make_engagement(conn, title="   ")
    assert getattr(ei.value, "error_code", None) == ErrorCode.VALIDATION


def test_create_engagement_seeds_test_types(conn):
    """验收 5：创建 engagement 后 test_types 已播种（21 就绪则断言 ≥1）。"""
    make_engagement(conn)
    n = conn.execute("SELECT count(*) FROM test_types WHERE engagement_id='eng_001'").fetchone()[0]
    if _seed_ready():
        assert n >= 1, "21 已就绪：create_engagement 应播种默认测试项目录"
    else:
        pytest.skip(f"21 未就绪（services.coverage 不存在），播种为 0：{n}")


# ---------------------------------------------------------------------------
# 2. 状态机全路径（验收 1）
# ---------------------------------------------------------------------------


def test_state_machine_planning_to_active_preconditions(conn):
    eid = make_engagement(conn)["id"]
    # scope 空 → 409
    with pytest.raises(Exception) as ei:
        scope_svc.transition_status(conn, eid, "active")
    assert getattr(ei.value, "error_code", None) == ErrorCode.ENGAGEMENT_INVALID_STATE
    # 登记 authorized target 后激活成功
    add_target(conn, eid, "example.com")
    eng = scope_svc.transition_status(conn, eid, "active")
    assert eng["status"] == "active"


def test_state_machine_illegal_transitions(conn):
    eid = make_engagement(conn)["id"]
    # planning → completed 非法
    with pytest.raises(Exception) as ei:
        scope_svc.transition_status(conn, eid, "completed")
    assert getattr(ei.value, "error_code", None) == ErrorCode.ENGAGEMENT_INVALID_STATE


def test_state_machine_full_path(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "example.com")
    # planning→active
    assert scope_svc.transition_status(conn, eid, "active")["status"] == "active"
    # active→paused
    assert scope_svc.transition_status(conn, eid, "paused")["status"] == "paused"
    # paused→active（恢复）
    assert scope_svc.transition_status(conn, eid, "active")["status"] == "active"
    # active→completed（置 completed_at）
    eng = scope_svc.transition_status(conn, eid, "completed")
    assert eng["status"] == "completed"
    assert eng["completed_at"] is not None


def test_state_machine_completed_to_active_requires_retest(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "example.com")
    scope_svc.transition_status(conn, eid, "active")
    scope_svc.transition_status(conn, eid, "completed")
    # 复测必须显式 retest=true
    with pytest.raises(Exception) as ei:
        scope_svc.transition_status(conn, eid, "active")
    assert getattr(ei.value, "error_code", None) == ErrorCode.ENGAGEMENT_INVALID_STATE
    eng = scope_svc.transition_status(conn, eid, "active", retest=True)
    assert eng["status"] == "active"
    assert eng["completed_at"] is None  # 复测清 completed_at


def test_state_machine_archived_irreversible(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "example.com")
    scope_svc.transition_status(conn, eid, "active")
    scope_svc.transition_status(conn, eid, "completed")
    eng = scope_svc.transition_status(conn, eid, "archived")
    assert eng["status"] == "archived"
    # archived 单向不可逆
    for target in ("active", "paused", "completed", "planning"):
        with pytest.raises(Exception) as ei:
            scope_svc.transition_status(conn, eid, target)
        assert getattr(ei.value, "error_code", None) == ErrorCode.ENGAGEMENT_INVALID_STATE


def test_state_machine_activate_kill_off(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "example.com")
    scope_svc.set_kill_switch(conn, eid, True)
    with pytest.raises(Exception) as ei:
        scope_svc.transition_status(conn, eid, "active")
    assert getattr(ei.value, "error_code", None) == ErrorCode.KILL_SWITCH_ON
    scope_svc.set_kill_switch(conn, eid, False)
    assert scope_svc.transition_status(conn, eid, "active")["status"] == "active"


# ---------------------------------------------------------------------------
# 3. 窗口到期自动 pause（验收 2 / B5）
# ---------------------------------------------------------------------------


def _raw_insert_target(conn, eid, tid, value, kind="domain"):
    """直插 target 行（测试辅助）。

    注意：``targets.id`` 为全局 PRIMARY KEY 而 ID 走 engagement 作用域计数器
    （next_id 对每个 engagement 都从 t-001 起），跨 engagement 复用 t-### 会触发
    PK 冲突（DDL 缺陷，已在交接物登记）。本辅助显式指定不冲突的 id 以聚焦
    到期语义，绕过该已知缺陷。
    """
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, auto_created, added_by, added_at) "
        "VALUES (?, ?, ?, ?, 'authorized', 0, 'human', '2026-08-06T00:00:00Z')",
        (tid, eid, value, kind),
    )
    conn.commit()


def test_expire_engagements_window_end(conn):
    # 窗口已结束（start<end 均在过去）
    eid = make_engagement(
        conn, start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z"
    )["id"]
    add_target(conn, eid, "example.com")
    scope_svc.transition_status(conn, eid, "active")
    assert scope_svc.get_engagement(conn, eid)["status"] == "active"
    # 未到期 engagement（第二 target 显式指定 id，绕开 targets.id 全局 PK 缺陷）
    eid2 = make_engagement(conn)["id"]
    _raw_insert_target(conn, eid2, "t-901", "example.org")
    scope_svc.transition_status(conn, eid2, "active")

    scope_svc.expire_engagements(conn)
    assert scope_svc.get_engagement(conn, eid)["status"] == "paused"
    assert scope_svc.get_engagement(conn, eid2)["status"] == "active"


def test_expire_engagements_no_window_noop(conn):
    eid = make_engagement(conn, start=None, end=None)["id"]
    add_target(conn, eid, "example.com")
    scope_svc.transition_status(conn, eid, "active")
    scope_svc.expire_engagements(conn)
    assert scope_svc.get_engagement(conn, eid)["status"] == "active"


# ---------------------------------------------------------------------------
# 4. check_engagement_writable（v2 §6.3）
# ---------------------------------------------------------------------------


def test_writable_requires_active(conn):
    eid = make_engagement(conn)["id"]
    with pytest.raises(Exception) as ei:
        scope_svc.check_engagement_writable(conn, eid)
    assert getattr(ei.value, "error_code", None) == ErrorCode.ENGAGEMENT_INVALID_STATE


def test_writable_out_of_window(conn):
    eid = make_engagement(
        conn, start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z"
    )["id"]
    add_target(conn, eid, "example.com")
    scope_svc.transition_status(conn, eid, "active")
    with pytest.raises(Exception) as ei:
        scope_svc.check_engagement_writable(conn, eid)
    assert getattr(ei.value, "error_code", None) == ErrorCode.OUT_OF_AUTHORIZATION_WINDOW


def test_writable_not_started_yet(conn):
    eid = make_engagement(
        conn, start="2099-01-01T00:00:00Z", end="2099-12-31T00:00:00Z"
    )["id"]
    add_target(conn, eid, "example.com")
    scope_svc.transition_status(conn, eid, "active")
    with pytest.raises(Exception) as ei:
        scope_svc.check_engagement_writable(conn, eid)
    assert getattr(ei.value, "error_code", None) == ErrorCode.OUT_OF_AUTHORIZATION_WINDOW


def test_writable_within_window(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "example.com")
    scope_svc.transition_status(conn, eid, "active")
    scope_svc.check_engagement_writable(conn, eid)  # 不抛


# ---------------------------------------------------------------------------
# 5. scope guard（验收 3：prohibited → SCOPE_DENIED 且无 fallback；auto_created）
# ---------------------------------------------------------------------------


def test_scope_guard_prohibited_denied(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "evil.com", scope_status="prohibited")
    with pytest.raises(Exception) as ei:
        scope_svc.check_scope_allowed(conn, eid, "evil.com")
    assert getattr(ei.value, "error_code", None) == ErrorCode.SCOPE_DENIED


def test_scope_guard_prohibited_no_fallback(conn):
    """prohibited 命中即使落在 authorized 大网段内也必须拒绝（无 fallback）。"""
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "10.0.0.0/8")          # authorized 大段
    add_target(conn, eid, "10.5.5.5", scope_status="prohibited")  # 但该 IP 被禁
    with pytest.raises(Exception) as ei:
        scope_svc.check_scope_allowed(conn, eid, "10.5.5.5")
    assert getattr(ei.value, "error_code", None) == ErrorCode.SCOPE_DENIED


def test_scope_guard_authorized_exact(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "example.com")
    t = scope_svc.check_scope_allowed(conn, eid, "example.com")
    assert t is not None and t["value"] == "example.com" and t["auto_created"] == 0
    # 精确命中不产生新行
    n = conn.execute(
        "SELECT count(*) FROM targets WHERE engagement_id=?", (eid,)
    ).fetchone()[0]
    assert n == 1


def test_scope_guard_auto_created_target(conn):
    """验收 3：check_scope_allowed 支持值→新 target（子域命中 auto_created=1）。"""
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "example.com")
    t = scope_svc.check_scope_allowed(conn, eid, "www.example.com")
    assert t is not None
    assert t["value"] == "www.example.com"
    assert t["auto_created"] == 1
    assert t["scope_status"] == "authorized"
    # 行确实落库
    row = conn.execute(
        "SELECT auto_created, scope_status FROM targets WHERE id=?", (t["id"],)
    ).fetchone()
    assert row["auto_created"] == 1 and row["scope_status"] == "authorized"
    # 重复查询幂等：不再新建
    t2 = scope_svc.check_scope_allowed(conn, eid, "www.example.com")
    assert t2["id"] == t["id"]


def test_scope_guard_cidr_containment(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "10.0.0.0/8")
    t = scope_svc.check_scope_allowed(conn, eid, "10.1.2.3")
    assert t is not None
    assert t["kind"] == "ip"
    assert t["auto_created"] == 1
    # CIDR 网段外的 IP → None（不在范围）
    assert scope_svc.check_scope_allowed(conn, eid, "192.168.1.1") is None


def test_scope_guard_out_of_scope_returns_none(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "example.com")
    assert scope_svc.check_scope_allowed(conn, eid, "unknown.other.org") is None


# ---------------------------------------------------------------------------
# 6. kill switch（v2 §4.12 / §6.3）
# ---------------------------------------------------------------------------


def test_kill_switch_project(conn):
    eid = make_engagement(conn)["id"]
    scope_svc.check_kill_switch(conn, eid)  # 未开 → 不抛
    scope_svc.set_kill_switch(conn, eid, True)
    with pytest.raises(Exception) as ei:
        scope_svc.check_kill_switch(conn, eid)
    assert getattr(ei.value, "error_code", None) == ErrorCode.KILL_SWITCH_ON


def test_kill_switch_global(conn):
    conn.execute("UPDATE settings SET global_kill_switch=1 WHERE rowid=1")
    conn.commit()
    eid = make_engagement(conn)["id"]
    with pytest.raises(Exception) as ei:
        scope_svc.check_kill_switch(conn, eid)
    assert getattr(ei.value, "error_code", None) == ErrorCode.KILL_SWITCH_ON


# ---------------------------------------------------------------------------
# 7. targets CRUD + 删除 gate（验收 4）
# ---------------------------------------------------------------------------


def test_target_create_kind_inference_and_dup(conn):
    eid = make_engagement(conn)["id"]
    t = add_target(conn, eid, "10.0.0.0/16")
    assert t["kind"] == "cidr"
    assert t["scope_status"] == "authorized"
    # 重复 value → 409 + 明细
    with pytest.raises(Exception) as ei:
        add_target(conn, eid, "10.0.0.0/16")
    err = ei.value
    assert getattr(err, "error_code", None) == ErrorCode.ENGAGEMENT_INVALID_STATE
    assert err.detail and err.detail["value"] == "10.0.0.0/16"


def test_target_create_bad_format(conn):
    eid = make_engagement(conn)["id"]
    with pytest.raises(Exception) as ei:
        add_target(conn, eid, "  ")  # 空 value
    assert getattr(ei.value, "error_code", None) == ErrorCode.VALIDATION


def test_target_update(conn):
    eid = make_engagement(conn)["id"]
    t = add_target(conn, eid, "example.com")
    t2 = scope_svc.update_target(
        conn, eid, t["id"], scope_status="prohibited", criticality=0.9, note="x"
    )
    assert t2["scope_status"] == "prohibited"
    assert t2["criticality"] == 0.9
    assert t2["note"] == "x"


def test_target_list_filter(conn):
    eid = make_engagement(conn)["id"]
    add_target(conn, eid, "a.com")
    add_target(conn, eid, "b.net")
    add_target(conn, eid, "c.org", scope_status="prohibited")
    auth = scope_svc.list_targets(conn, eid, scope_status="authorized")
    assert [t["value"] for t in auth] == ["a.com", "b.net"]
    assert len(scope_svc.list_targets(conn, eid)) == 3


def test_target_delete_unreferenced(conn):
    eid = make_engagement(conn)["id"]
    t = add_target(conn, eid, "example.com")
    scope_svc.delete_target(conn, eid, t["id"])
    assert scope_svc.get_target(conn, eid, t["id"]) is None


def test_target_delete_referenced_by_finding_409(conn):
    """验收 4：被未结算 finding 引用 → 409 并列出引用。"""
    eid = make_engagement(conn)["id"]
    t = add_target(conn, eid, "example.com")
    now = "2026-08-06T00:00:00Z"
    fid = db_module.next_id(conn, "finding", engagement_id=eid)
    conn.execute(
        "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, "
        "description, detected_by, created_at, updated_at) "
        "VALUES (?, ?, ?, 'XSS', 'high', 'high', 'd', 'w', ?, ?)",
        (fid, eid, t["id"], now, now),
    )
    conn.commit()
    with pytest.raises(Exception) as ei:
        scope_svc.delete_target(conn, eid, t["id"])
    err = ei.value
    assert getattr(err, "error_code", None) == ErrorCode.ENGAGEMENT_INVALID_STATE
    assert err.detail["findings"][0]["id"] == fid
    # target 仍在
    assert scope_svc.get_target(conn, eid, t["id"]) is not None


def test_target_delete_referenced_by_coverage_409(conn):
    eid = make_engagement(conn)["id"]
    t = add_target(conn, eid, "example.com")
    now = "2026-08-06T00:00:00Z"
    # 注（21 集成）：create_engagement 现已预置默认测试项目录（含 tt_web_xss），
    # 此处用手工 test_type 避免与 21 播种的默认目录撞主键。
    ttid = db_module.test_type_id("web_custom")
    conn.execute(
        "INSERT INTO test_types (id, engagement_id, name, category) VALUES (?, ?, 'Custom XSS', 'webapp')",
        (ttid, eid),
    )
    cid = db_module.next_id(conn, "coverage_item", engagement_id=eid)
    conn.execute(
        "INSERT INTO coverage_items (id, engagement_id, target_id, test_type_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (cid, eid, t["id"], ttid, now),
    )
    conn.commit()
    with pytest.raises(Exception) as ei:
        scope_svc.delete_target(conn, eid, t["id"])
    err = ei.value
    assert getattr(err, "error_code", None) == ErrorCode.ENGAGEMENT_INVALID_STATE
    assert err.detail["coverage_items"][0]["id"] == cid


def test_target_delete_after_settle(conn):
    """finding 已结算（closed）后不再阻塞删除。"""
    eid = make_engagement(conn)["id"]
    t = add_target(conn, eid, "example.com")
    now = "2026-08-06T00:00:00Z"
    fid = db_module.next_id(conn, "finding", engagement_id=eid)
    conn.execute(
        "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, "
        "status, description, detected_by, created_at, updated_at) "
        "VALUES (?, ?, ?, 'X', 'low', 'low', 'closed', 'd', 'w', ?, ?)",
        (fid, eid, t["id"], now, now),
    )
    conn.commit()
    scope_svc.delete_target(conn, eid, t["id"])
    assert scope_svc.get_target(conn, eid, t["id"]) is None


# ---------------------------------------------------------------------------
# 8. 路由集成（engagements + targets + scope/check + finalize 占位）
# ---------------------------------------------------------------------------


def test_router_engagement_crud_and_status(client):
    H = {"Authorization": "Bearer secret"}
    r = client.post(
        "/engagements",
        headers=H,
        json={
            "title": "R1",
            "authorized_start_at": "2026-01-01T00:00:00Z",
            "authorized_end_at": "2026-12-31T00:00:00Z",
            "scope_policy": {"tools": ["nuclei"]},
        },
    )
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    assert r.json()["scope_policy"] == {"tools": ["nuclei"]}

    # 列表（12 客户端 list_active：status=active 过滤）
    r = client.get("/engagements?status=planning", headers=H)
    assert r.status_code == 200
    assert isinstance(r.json(), list) and r.json()[0]["id"] == eid

    # 详情 / 更新
    r = client.put(
        f"/engagements/{eid}", headers=H, json={"title": "R1-renamed"}
    )
    assert r.status_code == 200 and r.json()["title"] == "R1-renamed"

    # targets + 激活 + scope check
    client.post(f"/engagements/{eid}/targets", headers=H, json={"value": "example.com", "scope": "authorized"})
    r = client.put(f"/engagements/{eid}/status", headers=H, json={"status": "active"})
    assert r.status_code == 200 and r.json()["status"] == "active"

    r = client.get(f"/engagements/{eid}/scope/check", headers=H, params={"value": "www.example.com"})
    assert r.status_code == 200 and r.json()["auto_created"] == 1

    # 不在 authorized 范围 → fail-closed 403（mallory.net 非 example.com 子域）
    r = client.get(f"/engagements/{eid}/scope/check", headers=H, params={"value": "mallory.net"})
    assert r.status_code == 403 and r.json()["error_code"] == "SCOPE_DENIED"

    # 熔断
    r = client.post(f"/engagements/{eid}/kill", headers=H)
    assert r.status_code == 200 and r.json()["kill_switch"] == 1

    # 删除
    r = client.delete(f"/engagements/{eid}", headers=H)
    assert r.status_code == 204


def test_router_status_illegal_409(client):
    H = {"Authorization": "Bearer secret"}
    r = client.post(
        "/engagements",
        headers=H,
        json={
            "title": "R2",
            "authorized_start_at": "2026-01-01T00:00:00Z",
            "authorized_end_at": "2026-12-31T00:00:00Z",
        },
    )
    eid = r.json()["id"]
    r = client.put(f"/engagements/{eid}/status", headers=H, json={"status": "completed"})
    assert r.status_code == 409
    assert r.json()["error_code"] == "ENGAGEMENT_INVALID_STATE"


def test_router_finalize_501(client):
    """finalize 占位已被 41 替换为真实端点。

    全新 planning engagement 未激活 → 409 ``ENGAGEMENT_INVALID_STATE``（状态 gate），
    而非旧 501 占位。达标流程（覆盖收敛 → completed）验收见 ``tests/test_report.py``。
    """
    H = {"Authorization": "Bearer secret"}
    r = client.post("/engagements", headers=H, json={"title": "F"})
    eid = r.json()["id"]
    r = client.post(f"/engagements/{eid}/finalize", headers=H)
    assert r.status_code == 409
    assert r.json()["error_code"] == "ENGAGEMENT_INVALID_STATE"


def test_router_target_delete_gate_409(client, conn):
    H = {"Authorization": "Bearer secret"}
    r = client.post("/engagements", headers=H, json={"title": "T"})
    eid = r.json()["id"]
    r = client.post(
        f"/engagements/{eid}/targets", headers=H, json={"value": "gate.com"}
    )
    tid = r.json()["id"]
    now = "2026-08-06T00:00:00Z"
    fid = db_module.next_id(conn, "finding", engagement_id=eid)
    conn.execute(
        "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, "
        "description, detected_by, created_at, updated_at) "
        "VALUES (?, ?, ?, 'X', 'high', 'high', 'd', 'w', ?, ?)",
        (fid, eid, tid, now, now),
    )
    conn.commit()
    r = client.delete(f"/engagements/{eid}/targets/{tid}", headers=H)
    assert r.status_code == 409
    body = r.json()
    assert body["error_code"] == "ENGAGEMENT_INVALID_STATE"
    assert body["detail"]["findings"][0]["id"] == fid


def test_router_duplicate_target_409(client):
    H = {"Authorization": "Bearer secret"}
    eid = client.post("/engagements", headers=H, json={"title": "D"}).json()["id"]
    client.post(f"/engagements/{eid}/targets", headers=H, json={"value": "dup.com"})
    r = client.post(f"/engagements/{eid}/targets", headers=H, json={"value": "dup.com"})
    assert r.status_code == 409
    assert r.json()["detail"]["value"] == "dup.com"


def test_router_auth_required(client):
    r = client.get("/engagements")
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_REQUIRED"
