"""25-graph-subdomain 验收测试。

对照 ``dev-agents/25-graph-subdomain.md`` §3 验收标准 + ``docs/exploration-graph-spec.md`` §7：
1. 播种 origin/goal（f001/f002）；ID 走 scoped_counters（f/i/h 各自独立 %03d，禁裸自增）；
2. Fact 只增不改；重复 description 幂等跳过；
3. create_intent 校验：from 含 goal / to=goal / worker≠creator → VALIDATION；
4. 租约仲裁：A 认领→B heartbeat/release 409；A release 后 B 可认领；conclude 后不可再 heartbeat；
5. conclude 三子域编排：facts 写图 + coverage_result/findings 转发 21/22（同事务，stub 验证）；
6. freeze_project_leases 清 open intent worker + reason 租约（B5）；
7. 超时清理：伪造超时心跳 → 读后 worker=NULL 重新可认领；已 conclude 不参与；
8. 403：stopped 项目上 claim → PROJECT_INACTIVE；
9. export YAML 合法含全部节点；timeline 事实增量 JSON。
"""

from __future__ import annotations

import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient

from cairn.config import ServerConfig
from cairn.server import db as db_module
from cairn.server.app import create_app
from cairn.server.errors import ErrorCode
from cairn.server.services import coverage as coverage_module
from cairn.server.services import findings as findings_module
from cairn.server.services import graph as svc

NOW = "2026-08-06T00:00:00.000000Z"
OLD_TS = "2020-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# 测试基建
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(tmp_path):
    conn = db_module.init_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


def _make_engagement(conn: sqlite3.Connection, *, title: str = "Engagement", status: str = "active") -> str:
    eid = db_module.next_id(conn, "engagement")
    conn.execute(
        "INSERT INTO engagements (id, title, status, created_at) VALUES (?,?,?,?)",
        (eid, title, status, NOW),
    )
    conn.commit()
    return eid


def _make_project(conn: sqlite3.Connection, eid: str, *, title: str = "Project") -> dict:
    p = svc.create_project(conn, engagement_id=eid, title=title)
    conn.commit()
    return p


def _app_client(db_path: str, token: str = "secret") -> TestClient:
    config = ServerConfig(
        db_path=db_path, api_token=token,
        evidence_root=f"{db_path}.evidence", traffic_root=f"{db_path}.traffic",
        archive_root=f"{db_path}.archive",
    )
    return TestClient(create_app(config))


@pytest.fixture()
def http_env(tmp_path):
    """已建 engagement 的 HTTP 环境（客户端 + db_path + 认证头）。"""
    db_path = str(tmp_path / "http.db")
    conn = db_module.init_db(db_path)
    eid = _make_engagement(conn)
    conn.close()
    return {"db_path": db_path, "eid": eid, "token": "secret"}


def _headers(token: str = "secret") -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. 播种 / ID 独立计数 / 只增不改幂等
# ---------------------------------------------------------------------------


def test_create_project_seeds_origin_goal(db_conn):
    eid = _make_engagement(db_conn)
    pid = svc.create_project(db_conn, engagement_id=eid, title="P")["id"]
    db_conn.commit()
    facts = svc.list_facts(db_conn, pid)
    by_desc = {f["description"]: f["id"] for f in facts}
    assert set(by_desc) >= {"origin", "goal"}
    # origin=f001, goal=f002（scoped_counters %03d）
    assert by_desc["origin"] == "f001"
    assert by_desc["goal"] == "f002"
    assert pid.startswith("proj_")
    # project 走全局 counters（与 engagement 独立）
    assert pid == "proj_001"


def test_scoped_id_independent_counting(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    svc.create_fact(db_conn, pid, description="candidate")          # f003
    svc.create_intent(db_conn, pid, description="int", creator="w",
                      from_fact_ids=[_fact_id(db_conn, pid, "origin")])  # i001
    svc.create_hint(db_conn, pid, content="h", creator="human")     # h001
    db_conn.commit()
    # f/i/h 各自独立计数，互不串扰
    assert svc.create_fact(db_conn, pid, description="vuln")["id"] == "f004"
    assert svc.create_intent(db_conn, pid, description="int2", creator="w",
                             from_fact_ids=[_fact_id(db_conn, pid, "origin")])["id"] == "i002"
    assert svc.create_hint(db_conn, pid, content="h2", creator="human")["id"] == "h002"


def test_fact_append_only_idempotent(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    f1 = svc.create_fact(db_conn, pid, description="same")
    f2 = svc.create_fact(db_conn, pid, description="same")
    db_conn.commit()
    assert f1["id"] == f2["id"]
    rows = db_conn.execute(
        "SELECT COUNT(*) FROM facts WHERE project_id=? AND description='same'", (pid,)
    ).fetchone()[0]
    assert rows == 1


def _fact_id(conn, pid, description) -> str:
    row = conn.execute(
        "SELECT id FROM facts WHERE project_id=? AND description=? LIMIT 1", (pid, description)
    ).fetchone()
    assert row is not None, f"fact {description!r} 未播种"
    return row["id"]


# ---------------------------------------------------------------------------
# 2. create_intent 校验（VALIDATION / NOT_FOUND）
# ---------------------------------------------------------------------------


def test_create_intent_validation_from_goal(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    goal_id = _fact_id(db_conn, pid, "goal")
    with pytest.raises(Exception) as ei:
        svc.create_intent(db_conn, pid, description="bad", creator="w", from_fact_ids=[goal_id])
    assert ei.value.error_code == ErrorCode.VALIDATION


def test_create_intent_validation_to_goal(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    goal_id = _fact_id(db_conn, pid, "goal")
    origin_id = _fact_id(db_conn, pid, "origin")
    with pytest.raises(Exception) as ei:
        svc.create_intent(db_conn, pid, description="bad", creator="w",
                          from_fact_ids=[origin_id], to_fact_id=goal_id)
    assert ei.value.error_code == ErrorCode.VALIDATION


def test_create_intent_validation_worker_ne_creator(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    origin_id = _fact_id(db_conn, pid, "origin")
    with pytest.raises(Exception) as ei:
        svc.create_intent(db_conn, pid, description="bad", creator="creatorA",
                          from_fact_ids=[origin_id], worker="workerB")
    assert ei.value.error_code == ErrorCode.VALIDATION


def test_create_intent_not_found(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    with pytest.raises(Exception) as ei:
        svc.create_intent(db_conn, pid, description="bad", creator="w", from_fact_ids=["f999"])
    assert ei.value.error_code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# 3. 租约仲裁（intent 级）
# ---------------------------------------------------------------------------


def _new_intent(conn, pid, *, creator="workerA") -> dict:
    origin_id = _fact_id(conn, pid, "origin")
    return svc.create_intent(conn, pid, description="explore", creator=creator,
                             from_fact_ids=[origin_id])


def test_intent_lease_arbitration(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    intent = _new_intent(db_conn, pid)
    iid = intent["id"]
    # A 认领
    svc.claim_intent(db_conn, pid, iid, worker="workerA")
    db_conn.commit()
    # B heartbeat/release → 409
    with pytest.raises(Exception) as ei:
        svc.heartbeat_intent(db_conn, pid, iid, worker="workerB")
    assert ei.value.error_code == ErrorCode.LEASE_CONFLICT
    with pytest.raises(Exception) as ei:
        svc.release_intent(db_conn, pid, iid, worker="workerB")
    assert ei.value.error_code == ErrorCode.LEASE_CONFLICT
    # A release 后 B 可认领
    svc.release_intent(db_conn, pid, iid, worker="workerA")
    db_conn.commit()
    svc.claim_intent(db_conn, pid, iid, worker="workerB")
    db_conn.commit()
    assert svc.get_project(db_conn, pid) is not None  # 冒烟
    # conclude 后不可再 heartbeat
    svc.conclude_intent(db_conn, pid, iid, worker="workerB", facts=["done"])
    db_conn.commit()
    with pytest.raises(Exception) as ei:
        svc.heartbeat_intent(db_conn, pid, iid, worker="workerB")
    assert ei.value.error_code == ErrorCode.LEASE_CONFLICT


def test_conclude_writes_facts_and_releases(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    intent = _new_intent(db_conn, pid, creator="workerA")
    iid = intent["id"]
    svc.claim_intent(db_conn, pid, iid, worker="workerA")
    db_conn.commit()
    result = svc.conclude_intent(db_conn, pid, iid, worker="workerA", facts=["f1", {"description": "f2"}])
    db_conn.commit()
    assert result["concluded_at"] is not None
    assert result["worker"] is None
    assert result["fact_ids"] == ["f003", "f004"]  # origin=f001 goal=f002 之后
    # 重复 fact 幂等：再次 conclude 用同 description 不重复建节点（已被拒绝——concluded 不可操作）
    facts = svc.list_facts(db_conn, pid)
    assert any(f["description"] == "f1" for f in facts)
    assert any(f["description"] == "f2" for f in facts)


# ---------------------------------------------------------------------------
# 4. reason 租约仲裁
# ---------------------------------------------------------------------------


def test_reason_lease_arbitration(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    svc.claim_reason(db_conn, pid, worker="workerA", trigger="gaps")
    db_conn.commit()
    # B heartbeat/release → 409
    with pytest.raises(Exception) as ei:
        svc.heartbeat_reason(db_conn, pid, worker="workerB")
    assert ei.value.error_code == ErrorCode.LEASE_CONFLICT
    with pytest.raises(Exception) as ei:
        svc.release_reason(db_conn, pid, worker="workerB")
    assert ei.value.error_code == ErrorCode.LEASE_CONFLICT
    # A 释放后 B 可认领
    svc.release_reason(db_conn, pid, worker="workerA")
    db_conn.commit()
    svc.claim_reason(db_conn, pid, worker="workerB")
    db_conn.commit()
    proj = svc.get_project(db_conn, pid)
    assert proj["reason_worker"] == "workerB"


# ---------------------------------------------------------------------------
# 5. freeze_project_leases（B5）
# ---------------------------------------------------------------------------


def test_freeze_project_leases(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    i1 = _new_intent(db_conn, pid, creator="workerA")
    i2 = _new_intent(db_conn, pid, creator="workerB")
    svc.claim_intent(db_conn, pid, i1["id"], worker="workerA")
    svc.claim_intent(db_conn, pid, i2["id"], worker="workerB")
    svc.claim_reason(db_conn, pid, worker="workerA")
    db_conn.commit()
    svc.freeze_project_leases(db_conn, pid)
    db_conn.commit()
    for intent in svc.list_intents(db_conn, pid):
        assert intent["worker"] is None
    proj = svc.get_project(db_conn, pid)
    assert proj["reason_worker"] is None


# ---------------------------------------------------------------------------
# 6. 超时清理
# ---------------------------------------------------------------------------


def test_timeout_cleanup(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    stale = _new_intent(db_conn, pid, creator="workerA")
    kept = _new_intent(db_conn, pid, creator="workerB")
    svc.claim_intent(db_conn, pid, stale["id"], worker="workerA")
    svc.claim_intent(db_conn, pid, kept["id"], worker="workerB")
    db_conn.commit()
    # 伪造超时心跳
    db_conn.execute("UPDATE intents SET last_heartbeat_at=? WHERE id=? AND project_id=?",
                    (OLD_TS, stale["id"], pid))
    db_conn.commit()
    cleaned = svc.intent_timeout_cleanup(db_conn, pid=pid)
    db_conn.commit()
    assert stale["id"] in cleaned
    # stale 重新可认领
    row = db_conn.execute("SELECT worker FROM intents WHERE id=?", (stale["id"],)).fetchone()
    assert row["worker"] is None
    # kept 未清理
    row = db_conn.execute("SELECT worker FROM intents WHERE id=?", (kept["id"],)).fetchone()
    assert row["worker"] == "workerB"


def test_timeout_cleanup_skips_concluded(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    intent = _new_intent(db_conn, pid, creator="workerA")
    svc.claim_intent(db_conn, pid, intent["id"], worker="workerA")
    svc.conclude_intent(db_conn, pid, intent["id"], worker="workerA")
    db_conn.commit()
    # 模拟异常态：concluded 却残留 worker（查询必须排除 concluded）
    db_conn.execute("UPDATE intents SET worker='workerA', last_heartbeat_at=? WHERE id=?",
                    (OLD_TS, intent["id"]))
    db_conn.commit()
    cleaned = svc.intent_timeout_cleanup(db_conn, pid=pid)
    db_conn.commit()
    assert intent["id"] not in cleaned
    row = db_conn.execute("SELECT worker, concluded_at FROM intents WHERE id=?", (intent["id"],)).fetchone()
    assert row["concluded_at"] is not None  # concluded 不参与清理


def test_reason_timeout_cleanup(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    svc.claim_reason(db_conn, pid, worker="workerA")
    db_conn.commit()
    db_conn.execute("UPDATE projects SET reason_last_heartbeat_at=? WHERE id=?", (OLD_TS, pid))
    db_conn.commit()
    svc.reason_timeout_cleanup(db_conn, pid=pid)
    db_conn.commit()
    proj = svc.get_project(db_conn, pid)
    assert proj["reason_worker"] is None


# ---------------------------------------------------------------------------
# 7. export YAML / 服务级
# ---------------------------------------------------------------------------


def test_export_graph_yaml_contains_all(db_conn):
    eid = _make_engagement(db_conn)
    pid = _make_project(db_conn, eid)["id"]
    svc.create_fact(db_conn, pid, description="found: 443 open")
    intent = _new_intent(db_conn, pid, creator="workerA")
    svc.claim_intent(db_conn, pid, intent["id"], worker="workerA")
    svc.conclude_intent(db_conn, pid, intent["id"], worker="workerA", facts=["concluded fact"])
    svc.create_hint(db_conn, pid, content="check /admin", creator="human")
    db_conn.commit()
    text = svc.export_graph_yaml(db_conn, pid)
    doc = yaml.safe_load(text)
    assert doc["project"]["id"] == pid
    descs = {f["description"] for f in doc["facts"]}
    assert {"origin", "goal", "found: 443 open", "concluded fact"} <= descs
    assert any(i["description"] == "explore" for i in doc["intents"])
    assert doc["hints"][0]["content"] == "check /admin"
    # from_fact_ids 引用
    intent0 = doc["intents"][0]
    assert intent0["from_fact_ids"] == [_fact_id(db_conn, pid, "origin")]


# ---------------------------------------------------------------------------
# 8. 路由（HTTP）
# ---------------------------------------------------------------------------


def _create_project_via_http(c: TestClient, eid: str, headers: dict) -> dict:
    r = c.post("/projects", json={"engagement_id": eid, "title": "HTTP Project"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _origin_id_via_http(c: TestClient, pid: str, headers: dict) -> str:
    r = c.get(f"/projects/{pid}", headers=headers)
    assert r.status_code == 200
    for f in r.json()["facts"]:
        if f["description"] == "origin":
            return f["id"]
    raise AssertionError("origin fact 未播种")


def test_router_create_project_and_seed(http_env):
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    project = _create_project_via_http(c, http_env["eid"], H)
    pid = project["id"]
    detail = c.get(f"/projects/{pid}", headers=H).json()
    descs = {f["description"] for f in detail["facts"]}
    assert {"origin", "goal"} <= descs
    # 未授权 → 401（GET /projects/{pid} 非豁免）
    assert c.get(f"/projects/{pid}").status_code == 401
    # GET /projects 列表被本路由遮蔽 10 的占位（返回真实项目而非 []）
    lst = c.get("/projects", headers=H).json()
    assert any(p["id"] == pid for p in lst)
    # 按 engagement 过滤
    lst2 = c.get(f"/projects?engagement_id={http_env['eid']}", headers=H).json()
    assert len(lst2) == 1 and lst2[0]["id"] == pid
    # 状态过滤
    lst3 = c.get("/projects?status=active", headers=H).json()
    assert any(p["id"] == pid for p in lst3)


def test_router_intent_validation(http_env):
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    pid = _create_project_via_http(c, http_env["eid"], H)["id"]
    origin = _origin_id_via_http(c, pid, H)
    goal = "f002"
    # from 含 goal → VALIDATION(422)
    r = c.post(f"/projects/{pid}/intents", json={
        "description": "bad", "creator": "w", "from_fact_ids": [goal],
    }, headers=H)
    assert r.status_code == 422 and r.json()["error_code"] == "VALIDATION"
    # to=goal → VALIDATION
    r = c.post(f"/projects/{pid}/intents", json={
        "description": "bad", "creator": "w", "from_fact_ids": [origin], "to_fact_id": goal,
    }, headers=H)
    assert r.status_code == 422 and r.json()["error_code"] == "VALIDATION"
    # worker≠creator → VALIDATION
    r = c.post(f"/projects/{pid}/intents", json={
        "description": "bad", "creator": "creatorA", "from_fact_ids": [origin], "worker": "workerB",
    }, headers=H)
    assert r.status_code == 422 and r.json()["error_code"] == "VALIDATION"


def test_router_lease_arbitration(http_env):
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    pid = _create_project_via_http(c, http_env["eid"], H)["id"]
    origin = _origin_id_via_http(c, pid, H)
    r = c.post(f"/projects/{pid}/intents", json={
        "description": "explore", "creator": "workerA", "from_fact_ids": [origin],
    }, headers=H)
    assert r.status_code == 201
    iid = r.json()["id"]
    # A 认领（claim 路由，12 客户端路径假设）
    assert c.post(f"/projects/{pid}/intents/{iid}/claim", json={"worker": "workerA"}, headers=H).status_code == 204
    # B heartbeat / release → 409
    r = c.post(f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": "workerB"}, headers=H)
    assert r.status_code == 409 and r.json()["error_code"] == "LEASE_CONFLICT"
    r = c.post(f"/projects/{pid}/intents/{iid}/release", json={"worker": "workerB"}, headers=H)
    assert r.status_code == 409
    # A release 后 B 可认领（首次心跳即认领）
    assert c.post(f"/projects/{pid}/intents/{iid}/release", json={"worker": "workerA"}, headers=H).status_code == 204
    assert c.post(f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": "workerB"}, headers=H).status_code == 204
    # conclude 后不可再 heartbeat
    assert c.post(f"/projects/{pid}/intents/{iid}/conclude", json={
        "worker": "workerB", "facts": ["final"],
    }, headers=H).status_code == 204
    r = c.post(f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": "workerB"}, headers=H)
    assert r.status_code == 409 and r.json()["error_code"] == "LEASE_CONFLICT"


def test_router_reason_lease(http_env):
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    pid = _create_project_via_http(c, http_env["eid"], H)["id"]
    assert c.post(f"/projects/{pid}/reason/claim", json={"worker": "workerA"}, headers=H).status_code == 204
    assert c.post(f"/projects/{pid}/reason/heartbeat", json={"worker": "workerB"}, headers=H).status_code == 409
    assert c.post(f"/projects/{pid}/reason/release", json={"worker": "workerB"}, headers=H).status_code == 409
    assert c.post(f"/projects/{pid}/reason/release", json={"worker": "workerA"}, headers=H).status_code == 204
    assert c.post(f"/projects/{pid}/reason/claim", json={"worker": "workerB"}, headers=H).status_code == 204


def test_router_stopped_project_403(http_env):
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    pid = _create_project_via_http(c, http_env["eid"], H)["id"]
    origin = _origin_id_via_http(c, pid, H)
    r = c.post(f"/projects/{pid}/intents", json={
        "description": "explore", "creator": "workerA", "from_fact_ids": [origin],
    }, headers=H)
    iid = r.json()["id"]
    assert c.put(f"/projects/{pid}/status", json={"status": "stopped"}, headers=H).status_code == 200
    r = c.post(f"/projects/{pid}/intents/{iid}/claim", json={"worker": "workerA"}, headers=H)
    assert r.status_code == 403 and r.json()["error_code"] == "PROJECT_INACTIVE"
    # stopped 立即冻结租约（B5）：先认领再停 → worker 清空
    pid2 = _create_project_via_http(c, http_env["eid"], H)["id"]
    origin2 = _origin_id_via_http(c, pid2, H)
    iid2 = c.post(f"/projects/{pid2}/intents", json={
        "description": "explore", "creator": "workerA", "from_fact_ids": [origin2],
    }, headers=H).json()["id"]
    c.post(f"/projects/{pid2}/intents/{iid2}/claim", json={"worker": "workerA"}, headers=H)
    c.put(f"/projects/{pid2}/status", json={"status": "stopped"}, headers=H)
    detail = c.get(f"/projects/{pid2}", headers=H).json()
    intent = next(i for i in detail["intents"] if i["id"] == iid2)
    assert intent["worker"] is None
    # 恢复 active 可继续
    assert c.put(f"/projects/{pid2}/status", json={"status": "active"}, headers=H).status_code == 200


def test_router_export_yaml(http_env):
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    pid = _create_project_via_http(c, http_env["eid"], H)["id"]
    origin = _origin_id_via_http(c, pid, H)
    c.post(f"/projects/{pid}/intents", json={
        "description": "explore", "creator": "workerA", "from_fact_ids": [origin],
    }, headers=H)
    r = c.get(f"/projects/{pid}/export?format=yaml", headers=H)
    assert r.status_code == 200
    assert "yaml" in r.headers.get("content-type", "")
    doc = yaml.safe_load(r.text)
    assert doc["project"]["id"] == pid
    assert {f["description"] for f in doc["facts"]} >= {"origin", "goal"}
    assert any(i["description"] == "explore" for i in doc["intents"])


def test_router_export_timeline(http_env):
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    pid = _create_project_via_http(c, http_env["eid"], H)["id"]
    r = c.get(f"/projects/{pid}/export?format=timeline", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert data["project_id"] == pid
    assert {f["description"] for f in data["facts"]} >= {"origin", "goal"}
    # after_ts 过滤
    r2 = c.get(f"/projects/{pid}/export?format=timeline&after_ts=2099-01-01T00:00:00Z", headers=H)
    assert r2.json()["facts"] == []


# ---------------------------------------------------------------------------
# 9. conclude 三子域编排（stub 21/22 验证同事务）
# ---------------------------------------------------------------------------


def test_router_conclude_forwards_21_22(http_env, monkeypatch):
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    pid = _create_project_via_http(c, http_env["eid"], H)["id"]
    origin = _origin_id_via_http(c, pid, H)
    iid = c.post(f"/projects/{pid}/intents", json={
        "description": "explore", "creator": "workerA", "from_fact_ids": [origin],
    }, headers=H).json()["id"]
    assert c.post(f"/projects/{pid}/intents/{iid}/claim", json={"worker": "workerA"}, headers=H).status_code == 204

    calls: list[tuple] = []

    def fake_coverage(conn, eid, **kwargs):
        calls.append(("coverage", eid, kwargs))

    def fake_finding(conn, eid, **kwargs):
        calls.append(("finding", eid, kwargs))

    monkeypatch.setattr(coverage_module, "write_coverage_result", fake_coverage)
    monkeypatch.setattr(findings_module, "create_finding", fake_finding)

    r = c.post(f"/projects/{pid}/intents/{iid}/conclude", json={
        "worker": "workerA",
        "facts": ["new fact A", {"description": "new fact B"}],
        "coverage_result": {
            "item_ids": ["c-001"], "depth_achieved": "standard", "outcome": "no_issue",
            "tested_scope": {"endpoints": ["/"]},
        },
        "findings": [{"title": "XSS", "severity": "medium", "target_id": "t-001"}],
    }, headers=H)
    assert r.status_code == 204, r.text

    # 21/22 均被调用（同请求同事务）
    kinds = [k for k, _, _ in calls]
    assert "coverage" in kinds and "finding" in kinds
    cov_call = next(x for x in calls if x[0] == "coverage")
    assert cov_call[1] == http_env["eid"]  # 用 project.engagement_id
    assert cov_call[2]["intent_id"] == iid
    assert cov_call[2]["item_ids"] == ["c-001"]
    fnd_call = next(x for x in calls if x[0] == "finding")
    assert fnd_call[1] == http_env["eid"]
    assert fnd_call[2]["actor"] == "agent"
    assert fnd_call[2]["payload"]["title"] == "XSS"
    # source_fact_id 溯源本次产出关键 fact（首次写出的 fact 是 f003）
    assert fnd_call[2]["payload"]["source_fact_id"] == "f003"

    # 图侧已提交：facts 落库 + concluded
    fresh = db_module.connect(http_env["db_path"])
    try:
        row = fresh.execute("SELECT concluded_at FROM intents WHERE id=? AND project_id=?", (iid, pid)).fetchone()
        assert row["concluded_at"] is not None
        count = fresh.execute(
            "SELECT COUNT(*) FROM facts WHERE project_id=? AND description IN ('new fact A','new fact B')",
            (pid,),
        ).fetchone()[0]
        assert count == 2
    finally:
        fresh.close()


def test_router_conclude_rollback_same_transaction(http_env, monkeypatch):
    """同事务原子性：coverage 写回失败 → 整请求失败，图写（facts/concluded）回滚。"""
    c = _app_client(http_env["db_path"])
    H = _headers(http_env["token"])
    pid = _create_project_via_http(c, http_env["eid"], H)["id"]
    origin = _origin_id_via_http(c, pid, H)
    iid = c.post(f"/projects/{pid}/intents", json={
        "description": "explore", "creator": "workerA", "from_fact_ids": [origin],
    }, headers=H).json()["id"]
    assert c.post(f"/projects/{pid}/intents/{iid}/claim", json={"worker": "workerA"}, headers=H).status_code == 204

    from cairn.server.errors import CairnError

    def boom(conn, eid, **kwargs):
        raise CairnError(ErrorCode.VALIDATION, message="stub coverage 失败")

    monkeypatch.setattr(coverage_module, "write_coverage_result", boom)

    r = c.post(f"/projects/{pid}/intents/{iid}/conclude", json={
        "worker": "workerA",
        "facts": ["rolled back fact"],
        "coverage_result": {"item_ids": ["c-001"], "depth_achieved": "standard", "outcome": "no_issue"},
    }, headers=H)
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION"

    fresh = db_module.connect(http_env["db_path"])
    try:
        row = fresh.execute("SELECT concluded_at FROM intents WHERE id=? AND project_id=?", (iid, pid)).fetchone()
        assert row["concluded_at"] is None  # 回滚：未 conclude
        count = fresh.execute(
            "SELECT COUNT(*) FROM facts WHERE project_id=? AND description='rolled back fact'", (pid,)
        ).fetchone()[0]
        assert count == 0  # 回滚：fact 未落库
    finally:
        fresh.close()
