"""24-progress-timeline 验收测试。

覆盖（dev-agents/24-progress-timeline.md §3 六项）：
1. open→append→events_after 增量序列正确；seq 单调；
2. SSE：TestClient 下 events 端点输出 event:/data: 帧 + 心跳注释；after_seq 断点续传无丢；
3. ticket 一次性 + 5s 过期；过期后 SSE 拒绝；
4. timeline 六源归并有序、limit 截断、after_ts 增量正确；
5. task_runs.project_id=NULL（verify 任务）可插入成功；
6. 对照 capture §7.2（流式采集/摘要≤512B）与 frontend §9（SSE/ticket/长轮询）自查。
"""

from __future__ import annotations

import os
import sqlite3
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from cairn.config import ServerConfig
from cairn.server import db as db_module
from cairn.server.app import create_app
from cairn.server.services import progress as progress_svc

import cairn.server.routers.progress as progress_router  # noqa: E402


def _utc() -> str:
    return "2026-08-06T00:00:00Z"


def make_config(db_path, tmp_path, token="secret") -> ServerConfig:
    return ServerConfig(
        db_path=db_path,
        api_token=token,
        evidence_root=str(tmp_path / "evidence"),
        traffic_root=str(tmp_path / "traffic"),
        archive_root=str(tmp_path / "archive"),
        logs_root=str(tmp_path / "logs"),
    )


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    config = make_config(db_path, tmp_path)
    app = create_app(config)
    c = TestClient(app)
    with c:
        conn = db_module.connect(db_path)
        eid = db_module.next_id(conn, "engagement")
        conn.execute(
            "INSERT INTO engagements (id, title, status, created_by, created_at) VALUES (?, 'E', 'active', 'human', ?)",
            (eid, _utc()),
        )
        conn.commit()
        conn.close()
        yield c, db_path, eid


def _auth(token: str = "secret") -> dict:
    return {"Authorization": f"Bearer {token}"}


def _open_run(db_path, eid, **kw) -> dict:
    conn = db_module.connect(db_path)
    run = progress_svc.open_task_run(
        conn, engagement_id=eid, task_type=kw.get("task_type", "explore"), worker=kw.get("worker", "worker-1"),
        project_id=kw.get("project_id"),
    )
    conn.close()
    return run


# ---------------------------------------------------------------------------
# 1. open→append→events_after 增量序列；seq 单调；B2 project_id 可空
# ---------------------------------------------------------------------------


def test_open_task_run_project_id_null(client):
    """B2：project_id=NULL（verify/audit/replay 不挂 project）可插入成功。"""
    c, db_path, eid = client
    # 直接经服务调用（独立连接）
    conn = db_module.connect(db_path)
    run = progress_svc.open_task_run(
        conn, engagement_id=eid, project_id=None, task_type="verify", worker="worker-2"
    )
    conn.close()
    assert run["project_id"] is None
    assert run["status"] == "queued"
    assert run["id"].startswith("task-")

    conn = db_module.connect(db_path)
    run2 = progress_svc.open_task_run(
        conn, engagement_id=eid, project_id="proj_001", task_type="explore", worker="worker-1"
    )
    conn.close()
    assert run2["project_id"] == "proj_001"
    assert run2["id"] != run["id"]


def test_open_task_run_validates(client):
    c, db_path, eid = client
    conn = db_module.connect(db_path)
    with pytest.raises(Exception) as exc:
        progress_svc.open_task_run(conn, engagement_id="eng_bogus", task_type="explore", worker="w")
    assert "不存在" in str(exc.value)
    with pytest.raises(Exception):
        progress_svc.open_task_run(conn, engagement_id=eid, task_type="explore", worker="")
    with pytest.raises(Exception):
        progress_svc.open_task_run(conn, engagement_id=eid, task_type="badtype", worker="w")
    conn.close()


def test_open_append_events_after_incremental(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    ev1 = progress_svc.append_event(conn, run["id"], kind="step", level="info", message="开始")
    ev2 = progress_svc.append_event(conn, run["id"], kind="command", level="info", message="$ ls")
    ev3 = progress_svc.append_event(conn, run["id"], kind="error", level="error", message="boom")
    conn.close()

    assert ev1["seq"] == 1
    assert ev2["seq"] == 2
    assert ev3["seq"] == 3
    assert ev1["id"].startswith("ev-")

    # 增量读取：after_seq=1 → 只回 seq 2,3
    conn = db_module.connect(db_path)
    items = progress_svc.events_after(conn, run["id"], after_seq=1, limit=10)
    conn.close()
    assert [it["seq"] for it in items] == [2, 3]
    assert [it["kind"] for it in items] == ["command", "error"]

    # seq 单调（全量读）
    conn = db_module.connect(db_path)
    all_items = progress_svc.events_after(conn, run["id"], after_seq=0, limit=10)
    conn.close()
    seqs = [it["seq"] for it in all_items]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))


def test_append_event_validates_kind_level(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    with pytest.raises(Exception):
        progress_svc.append_event(conn, run["id"], kind="bogus", level="info")
    with pytest.raises(Exception):
        progress_svc.append_event(conn, run["id"], kind="step", level="bogus")
    with pytest.raises(Exception):
        progress_svc.append_event(conn, "task-bogus", kind="step", level="info")
    conn.close()


def test_append_event_message_truncation(client):
    """capture §7.2：message ≤ tuning.event_summary_max_bytes（512B），超限截断。"""
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    ev = progress_svc.append_event(conn, run["id"], kind="output", level="info", message="x" * 600)
    conn.close()
    assert len(ev["message"]) <= 512
    assert len(ev["message"]) >= 480


def test_finish_task_run(client):
    """12 客户端 path 假设：/tasks/{id}/finish 收尾（仅终态）。"""
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    h = _auth()
    # queued → success
    r = c.post(f"/tasks/{run['id']}/finish", headers=h, json={"status": "success", "outcome_note": "done"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "success"
    assert body["outcome_note"] == "done"
    assert body["finished_at"] is not None

    # 中间态拒收
    r = c.post(f"/tasks/{run['id']}/finish", headers=h, json={"status": "queued"})
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION"

    # 未知状态 → 422
    r = c.post(f"/tasks/{run['id']}/finish", headers=h, json={"status": "bogus"})
    assert r.status_code == 422

    # 不存在 run → 404
    r = c.post("/tasks/task-bogus/finish", headers=h, json={"status": "failed"})
    assert r.status_code == 404


def test_events_after_kind_level_filters(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="output", level="debug", message="a")
    progress_svc.append_event(conn, run["id"], kind="error", level="error", message="b")
    progress_svc.append_event(conn, run["id"], kind="step", level="info", message="c")
    conn.close()
    conn = db_module.connect(db_path)
    assert [it["seq"] for it in progress_svc.events_after(conn, run["id"], 0, kind="error")] == [2]
    assert [it["seq"] for it in progress_svc.events_after(conn, run["id"], 0, level="debug")] == [1]
    assert [it["seq"] for it in progress_svc.events_after(conn, run["id"], 0, kind="step", level="info")] == [3]
    conn.close()


def test_task_list_and_detail(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid, task_type="explore", worker="worker-1")
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="step", level="info", message="doing")
    conn.close()

    h = _auth()
    r = c.get(f"/tasks/{run['id']}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_count"] == 1
    assert body["latest_event"]["message"] == "doing"
    assert body["task_type"] == "explore"
    assert "duration_seconds" in body

    r = c.get(f"/engagements/{eid}/tasks", headers=h)
    assert r.status_code == 200
    assert len(r.json()) == 1

    # active=true 只含 queued/running
    r = c.get(f"/engagements/{eid}/tasks?active=true", headers=h)
    assert r.status_code == 200
    assert len(r.json()) == 1
    # 把 run 置为终态后 active 不再含它
    conn = db_module.connect(db_path)
    conn.execute("UPDATE task_runs SET status='success' WHERE id=?", (run["id"],))
    conn.commit()
    conn.close()
    r = c.get(f"/engagements/{eid}/tasks?active=true", headers=h)
    assert r.json() == []


# ---------------------------------------------------------------------------
# 2. SSE：event:/data: 帧 + 心跳；after_seq 断点续传
#
# 说明：TestClient 流式响应无法增量读无限流（transport 缓冲），故测试用
# ``_SSE_MAX_HEARTBEATS`` 让流在 N 次心跳后自然结束，再 ``resp.read()`` 全量断言。
# 帧格式/心跳/断点续传另在 :func:`test_sse_generator_direct` 用生成器直测。
# ---------------------------------------------------------------------------


def test_sse_frames_and_heartbeat(client, monkeypatch):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="step", level="info", message="开始任务")
    conn.close()

    monkeypatch.setattr(progress_router, "_SSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(progress_router, "_SSE_MAX_HEARTBEATS", 2)
    monkeypatch.setattr(progress_svc, "tuning_values", lambda: (0.05, 20, 512))  # 心跳 0.05s

    h = _auth()
    tok = c.post(f"/tasks/{run['id']}/events/ticket", headers=h).json()["ticket"]
    with c.stream(
        "GET", f"/tasks/{run['id']}/events?ticket={tok}&after_seq=0&mode=sse", headers=h
    ) as resp:
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")
        text = resp.read().decode("utf-8")
    assert "event: step" in text
    assert '"seq": 1' in text
    assert '"message": "开始任务"' in text
    assert ": heartbeat" in text


def test_sse_after_seq_resume_no_loss(client, monkeypatch):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="output", level="info", message="first")
    progress_svc.append_event(conn, run["id"], kind="output", level="info", message="second")
    conn.close()

    monkeypatch.setattr(progress_router, "_SSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(progress_router, "_SSE_MAX_HEARTBEATS", 1)
    monkeypatch.setattr(progress_svc, "tuning_values", lambda: (0.05, 20, 512))

    h = _auth()
    tok = c.post(f"/tasks/{run['id']}/events/ticket", headers=h).json()["ticket"]
    with c.stream(
        "GET", f"/tasks/{run['id']}/events?ticket={tok}&after_seq=1&mode=sse", headers=h
    ) as resp:
        text = resp.read().decode("utf-8")
    # 断点续传：只推 seq=2，不丢
    assert '"seq": 2' in text
    assert '"seq": 1' not in text
    assert '"message": "second"' in text


def test_sse_works_without_bearer(client, monkeypatch):
    """模拟 EventSource：不带 Authorization 头，仅 ticket（auth 中间件豁免路径）。"""
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="step", level="info", message="x")
    conn.close()

    monkeypatch.setattr(progress_router, "_SSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(progress_router, "_SSE_MAX_HEARTBEATS", 1)
    monkeypatch.setattr(progress_svc, "tuning_values", lambda: (0.05, 20, 512))

    tok = c.post(f"/tasks/{run['id']}/events/ticket", headers=_auth()).json()["ticket"]
    with c.stream("GET", f"/tasks/{run['id']}/events?ticket={tok}&after_seq=0&mode=sse") as resp:
        assert resp.status_code == 200
        text = resp.read().decode("utf-8")
    assert '"seq": 1' in text


def test_sse_generator_direct(tmp_path):
    """直接迭代 SSE 生成器：帧格式 + 心跳 + after_seq 断点续传（不依赖 HTTP 传输）。"""
    db_path = str(tmp_path / "test.db")
    config = make_config(db_path, tmp_path)
    create_app(config)
    conn = db_module.connect(db_path)
    eid = db_module.next_id(conn, "engagement")
    conn.execute(
        "INSERT INTO engagements (id,title,created_at) VALUES (?,'E','2026-08-06T00:00:00Z')", (eid,)
    )
    run = progress_svc.open_task_run(conn, engagement_id=eid, task_type="explore", worker="w1")
    progress_svc.append_event(conn, run["id"], kind="step", level="info", message="s1")
    progress_svc.append_event(conn, run["id"], kind="error", level="error", message="s2")
    conn.close()

    # after_seq=0 → 两条回填 + 心跳
    gen = progress_router._sse_events(db_path, run["id"], 0, None, None, heartbeat=0.05, poll=0.01)
    frames = []
    for _ in range(100):
        f = next(gen)
        frames.append(f)
        if ": heartbeat" in f:
            break
    gen.close()
    joined = "".join(frames)
    assert "event: step" in joined and "event: error" in joined
    assert '"seq": 1' in joined and '"seq": 2' in joined
    assert ": heartbeat" in joined

    # after_seq=1 → 只回 seq=2（断点续传）
    gen = progress_router._sse_events(db_path, run["id"], 1, None, None, heartbeat=0.05, poll=0.01)
    frames = []
    for _ in range(100):
        f = next(gen)
        frames.append(f)
        if ": heartbeat" in f:
            break
    gen.close()
    joined = "".join(frames)
    assert '"seq": 2' in joined
    assert '"seq": 1' not in joined


def test_sse_requires_ticket(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    # 无 ticket 的 SSE → 422 VALIDATION
    r = c.get(f"/tasks/{run['id']}/events?mode=sse", headers=_auth())
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION"


# ---------------------------------------------------------------------------
# 3. ticket 一次性 + 过期
# ---------------------------------------------------------------------------


def test_ticket_onetime(client, monkeypatch):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="step", level="info", message="x")
    conn.close()

    h = _auth()
    tok = c.post(f"/tasks/{run['id']}/events/ticket", headers=h).json()["ticket"]

    # 首次消费（handler 同步消费 ticket；用 _SSE_MAX_HEARTBEATS 让流可自然结束，避免悬挂）
    import cairn.server.routers.progress as rmod

    monkeypatch.setattr(rmod, "_SSE_MAX_HEARTBEATS", 1)
    monkeypatch.setattr(rmod, "_SSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(progress_svc, "tuning_values", lambda: (0.05, 20, 512))
    with c.stream("GET", f"/tasks/{run['id']}/events?ticket={tok}&after_seq=0&mode=sse", headers=h) as resp:
        assert resp.status_code == 200
        resp.read()

    # 复用同一 ticket → 422 已使用
    r = c.get(f"/tasks/{run['id']}/events?ticket={tok}&mode=sse", headers=h)
    assert r.status_code == 422
    assert "已使用" in r.json()["message"]


def test_ticket_expiry(client, monkeypatch):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    h = _auth()

    monkeypatch.setattr(progress_router, "_TICKET_TTL_SECONDS", 0.05)
    tok = c.post(f"/tasks/{run['id']}/events/ticket", headers=h).json()["ticket"]
    time.sleep(0.1)
    # 过期后 SSE 拒绝
    r = c.get(f"/tasks/{run['id']}/events?ticket={tok}&mode=sse", headers=h)
    assert r.status_code == 422
    assert "过期" in r.json()["message"]


def test_ticket_requires_auth(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    # 无 Bearer → 401（ticket 签发端点走主 token）
    r = c.post(f"/tasks/{run['id']}/events/ticket")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 3b. 事件端点鉴权 / 长轮询 / 即时 JSON
# ---------------------------------------------------------------------------


def test_events_json_requires_auth(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    r = c.get(f"/tasks/{run['id']}/events")
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_REQUIRED"


def test_events_json_immediate(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="output", level="info", message="hi")
    conn.close()
    r = c.get(f"/tasks/{run['id']}/events?after_seq=0", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["last_seq"] == 1
    assert body["items"][0]["kind"] == "output"


def test_longpoll_returns_events(client):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="status", level="info", message="ok")
    conn.close()
    r = c.get(f"/tasks/{run['id']}/events?mode=longpoll&after_seq=0", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1
    assert body["last_seq"] == 1


def test_longpoll_empty_after_hold(client, monkeypatch):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    monkeypatch.setattr(progress_router, "_LONGPOLL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(progress_svc, "tuning_values", lambda: (15, 0.05, 512))  # hold 0.05s
    t0 = time.monotonic()
    r = c.get(f"/tasks/{run['id']}/events?mode=longpoll&after_seq=0", headers=_auth())
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    assert r.json() == {"items": [], "last_seq": 0}
    assert elapsed < 1.0  # hold 生效且不悬挂


def test_event_raw_lazy(client, tmp_path):
    c, db_path, eid = client
    run = _open_run(db_path, eid)
    logs_root = str(tmp_path / "logs")
    os.makedirs(logs_root, exist_ok=True)
    # 写分片文件 + 摘要
    rel = f"logs/{run['id']}/1.chunk"
    full = os.path.join(logs_root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write("raw line 1\nraw line 2\n")
    conn = db_module.connect(db_path)
    progress_svc.append_event(conn, run["id"], kind="output", level="info", message="摘要", raw_path=rel)
    conn.close()

    r = c.get(f"/tasks/{run['id']}/events/1/raw", headers=_auth())
    assert r.status_code == 200
    assert r.text == "raw line 1\nraw line 2\n"
    assert r.headers["content-type"].startswith("text/plain")

    # 无原始文件的事件 → 404
    r = c.get(f"/tasks/{run['id']}/events/999/raw", headers=_auth())
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4. timeline 六源归并 / limit / after_ts
# ---------------------------------------------------------------------------


def _seed_timeline_sources(conn, eid):
    """六源各造一条，ts 递增；返回各源 ts 映射。"""
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, added_by, added_at) "
        "VALUES ('t-001', ?, 'a.com', 'domain', 'authorized', 'human', '2026-08-06T00:00:00Z')",
        (eid,),
    )
    conn.execute(
        "INSERT INTO test_types (id, engagement_id, name, category) VALUES ('tt_x', ?, 'Web', 'webapp')",
        (eid,),
    )
    # graph
    conn.execute(
        "INSERT INTO projects (id, engagement_id, title, created_at) VALUES ('proj_001', ?, 'P', '2026-08-06T00:00:00Z')",
        (eid,),
    )
    conn.execute(
        "INSERT INTO facts (id, project_id, description, created_at) VALUES ('f001', 'proj_001', 'fact A', '2026-08-06T00:00:01Z')",
    )
    conn.execute(
        "INSERT INTO intents (id, project_id, description, creator, created_at, concluded_at) "
        "VALUES ('i001', 'proj_001', 'intent A', 'worker-1', '2026-08-06T00:00:00Z', '2026-08-06T00:00:01Z')",
    )
    conn.execute(
        "INSERT INTO hints (id, project_id, content, creator, created_at) "
        "VALUES ('h001', 'proj_001', 'hint A', 'human', '2026-08-06T00:00:01Z')",
    )
    # task
    conn.execute(
        "INSERT INTO task_runs (id, engagement_id, task_type, worker, status) VALUES ('task-001', ?, 'explore', 'worker-1', 'running')",
        (eid,),
    )
    conn.execute(
        "INSERT INTO task_events (id, task_run_id, seq, ts, kind, level, message) "
        "VALUES ('ev-001', 'task-001', 1, '2026-08-06T00:00:02Z', 'step', 'info', 'step msg')",
    )
    # finding
    conn.execute(
        "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, description, "
        "detected_by, created_at, updated_at) VALUES ('fd-001', ?, 't-001', 'XSS', 'high', 'high', 'd', 'worker-1', '2026-08-06T00:00:00Z', '2026-08-06T00:00:00Z')",
        (eid,),
    )
    conn.execute(
        "INSERT INTO finding_history (id, finding_id, from_status, to_status, actor, created_at) "
        "VALUES ('fh-001', 'fd-001', NULL, 'open', 'worker-1', '2026-08-06T00:00:03Z')",
    )
    # traffic（用 +00:00 格式验证 epoch 归并兼容 Z/+00:00 混合）
    conn.execute(
        "INSERT INTO traffic_entries (id, engagement_id, seq, captured_at, method, url, client, req_path, req_bytes) "
        "VALUES ('tr-001', ?, 1, '2026-08-06T00:00:04+00:00', 'GET', 'http://a.com/x', 'worker-1', 't/1.req', 10)",
        (eid,),
    )
    # coverage
    conn.execute(
        "INSERT INTO coverage_items (id, engagement_id, target_id, test_type_id, created_at) "
        "VALUES ('c-001', ?, 't-001', 'tt_x', '2026-08-06T00:00:00Z')",
        (eid,),
    )
    conn.execute(
        "INSERT INTO coverage_records (id, item_id, engagement_id, depth_achieved, outcome, created_at) "
        "VALUES ('cr-001', 'c-001', ?, 'standard', 'no_issue', '2026-08-06T00:00:05Z')",
        (eid,),
    )
    conn.execute(
        "INSERT INTO waivers (id, item_id, engagement_id, kind, reason, created_by, created_at) "
        "VALUES ('w-001', 'c-001', ?, 'out_of_scope', 'na', 'human', '2026-08-06T00:00:05Z')",
        (eid,),
    )
    conn.execute(
        "INSERT INTO audit_runs (id, engagement_id, coverage_item_id, reason, auditor, created_at) "
        "VALUES ('ar-001', ?, 'c-001', 'sampling', 'worker-2', '2026-08-06T00:00:05Z')",
        (eid,),
    )
    # report
    conn.execute(
        "INSERT INTO reports (id, engagement_id, format, path, generated_by, created_at) "
        "VALUES ('rpt-001', ?, 'markdown', 'r.md', 'human', '2026-08-06T00:00:06Z')",
        (eid,),
    )


def test_timeline_six_sources_sorted(client):
    c, db_path, eid = client
    conn = db_module.connect(db_path)
    _seed_timeline_sources(conn, eid)
    conn.commit()
    conn.close()

    r = c.get(f"/engagements/{eid}/timeline", headers=_auth())
    assert r.status_code == 200, r.text
    items = r.json()
    sources = {it["source"] for it in items}
    assert sources == {"graph", "task", "finding", "traffic", "coverage", "report"}
    # 统一结构键
    for it in items:
        assert {"ts", "source", "kind", "actor", "summary", "ref"} <= set(it)
    # 按 ts 升序（epoch 归并，兼容 Z / +00:00）
    from cairn.server.services.timeline import _iso_to_epoch

    epochs = [_iso_to_epoch(it["ts"]) for it in items]
    assert epochs == sorted(epochs)
    # 第一个是 graph fact_created（00:00:01Z）；traffic（00:00:04+00:00）排在 finding(03Z) 之后
    assert items[0]["source"] == "graph"
    sources_in_order = [it["source"] for it in items]
    assert sources_in_order.index("traffic") > sources_in_order.index("finding")


def test_timeline_after_ts_and_limit(client):
    c, db_path, eid = client
    conn = db_module.connect(db_path)
    _seed_timeline_sources(conn, eid)
    conn.commit()
    conn.close()
    h = _auth()

    # limit=2 → 只回前 2 条（00:00:01Z 两条 graph）
    r = c.get(f"/engagements/{eid}/timeline?limit=2", headers=h)
    items = r.json()
    assert len(items) == 2

    # after_ts=00:00:03Z → 只回 ts>03Z（traffic 04 / coverage 05 / report 06）
    r = c.get(f"/engagements/{eid}/timeline?after_ts=2026-08-06T00:00:03Z", headers=h)
    items = r.json()
    assert len(items) == 5
    sources = {it["source"] for it in items}
    assert "finding" not in sources  # finding 是 03Z，不含
    assert sources == {"traffic", "coverage", "report"}

    # 增量：再拉 after_ts=04+00:00（URL 编码 +）→ 只剩 coverage/report（traffic 恰等于 04Z，严格 > 排除）
    after = quote("2026-08-06T00:00:04+00:00", safe="")
    r = c.get(f"/engagements/{eid}/timeline?after_ts={after}", headers=h)
    items = r.json()
    assert {it["source"] for it in items} == {"coverage", "report"}


def test_timeline_unknown_engagement(client):
    c, db_path, eid = client
    r = c.get("/engagements/eng_bogus/timeline", headers=_auth())
    assert r.status_code == 404


def test_timeline_empty(client):
    c, db_path, eid = client
    r = c.get(f"/engagements/{eid}/timeline", headers=_auth())
    assert r.status_code == 200
    assert r.json() == []
