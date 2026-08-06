"""23-capture 验收测试。

覆盖（dev-agents/23-capture.md §3 六项 + capture-verify-progress-spec §10 1-6/11-13/16）：
1. index_traffic→resolve_traffic 往返：字节一致；sha256 校验失败标 traffic_corrupt（§10.1/F2）
2. digest：≤digest_budget、截断含 sha256 引用；for_model=false 返回全量引用（§10.5）
3. fail-closed：白名单外 host 请求不被索引；模拟代理回写时服务端拒绝（F5/§10.2）
4. C12：client_ip→worker 映射正确；host 网络共享 IP → client=NULL（§10.11）
5. C11：targets 增删后白名单 ≤1 interval 更新；kill 后置空（§10.12）
6. C2 对账 capture_gap（§10.13）+ C13 at-rest 0700（§10.16）
7. F8：POST /traffic 代理受限 token 鉴权（非 Bearer 主 token）
8. derive_http_from_capture 以捕获字节派生 finding_http_evidence（C2，经 22 服务函数）
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from cairn.config import ServerConfig
from cairn.server import db as db_module
from cairn.server.app import create_app
from cairn.server.models import HttpEvidenceSource, TrafficLinkRole
from cairn.server.services import capture as capture_svc
from cairn.server.capture_proxy import (
    CaptureProxyManager,
    FakeProxyEngine,
    generate_ca,
)

MAIN_TOKEN = "secret"
CAPTURE_TOKEN = "captok"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_CAPTURE_TOKEN", CAPTURE_TOKEN)
    db_path = str(tmp_path / "test.db")
    traffic_root = str(tmp_path / "traffic")
    os.makedirs(traffic_root, exist_ok=True)
    config = ServerConfig(
        db_path=db_path,
        api_token=MAIN_TOKEN,
        evidence_root=str(tmp_path / "evidence"),
        traffic_root=traffic_root,
        archive_root=str(tmp_path / "archive"),
    )
    client = TestClient(create_app(config))
    with client:
        yield {
            "db_path": db_path,
            "traffic_root": traffic_root,
            "client": client,
            "main": MAIN_TOKEN,
            "capture": CAPTURE_TOKEN,
        }


def _seed_engagement(db_path, *, scope_policy=None, targets=()):
    """直接播种 engagement + targets（20 未实现阶段用；scope_policy 可给 capture_proxy 段）。"""
    conn = db_module.connect(db_path)
    eid = db_module.next_id(conn, "engagement")
    conn.execute(
        "INSERT INTO engagements (id, title, status, scope_policy, created_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (eid, "E", "active", json.dumps(scope_policy or {}), "human", _utc()),
    )
    for value, kind, scope_status in targets:
        tid = db_module.next_id(conn, "target", eid)
        conn.execute(
            "INSERT INTO targets (id, engagement_id, value, kind, scope_status, criticality, added_by, added_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (tid, eid, value, kind, scope_status, 0.5, "human", _utc()),
        )
    conn.commit()
    conn.close()
    return eid


def _seed_finding(db_path, eid, target_id, title="SQLi"):
    conn = db_module.connect(db_path)
    fid = db_module.next_id(conn, "finding", eid)
    conn.execute(
        "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, "
        "status, description, detected_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (fid, eid, target_id, title, "high", "high", "open", "desc", "worker-1", _utc(), _utc()),
    )
    conn.commit()
    conn.close()
    return fid


def _write_traffic(traffic_root, req: bytes, resp: bytes, seq: int = 1, eid: str = "eng_001"):
    rel_req = f"{eid}/20260806T010203_{seq}.req"
    rel_resp = f"{eid}/20260806T010203_{seq}.resp"
    os.makedirs(os.path.join(traffic_root, eid), exist_ok=True)
    with open(os.path.join(traffic_root, rel_req), "wb") as f:
        f.write(req)
    with open(os.path.join(traffic_root, rel_resp), "wb") as f:
        f.write(resp)
    return rel_req, rel_resp


def _post_traffic(env, eid, entry):
    return env["client"].post(
        f"/engagements/{eid}/traffic",
        json=entry,
        headers={"Authorization": f"Bearer {env['capture']}"},
    )


REQ = b"GET /login?x=1 HTTP/1.1\r\nHost: allowed.example.com\r\nUser-Agent: curl/8.0\r\n\r\n"
RESP = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 3\r\n\r\nok!"


def _entry(rel_req, rel_resp, host="allowed.example.com", client=None, client_ip="172.17.0.3", sha=None):
    return {
        "method": "GET",
        "url": f"http://{host}/login?x=1",
        "host": host,
        "client": client,
        "client_ip": client_ip,
        "status": 200,
        "req_path": rel_req,
        "resp_path": rel_resp,
        "req_bytes": len(REQ),
        "resp_bytes": len(RESP),
        "content_type": "text/html",
        "sha256": sha,
        "chunk_count": 1,
    }


# ---------------------------------------------------------------------------
# 验收 1：index→resolve 往返字节一致 + sha256 校验失败标 corrupt（F2）
# ---------------------------------------------------------------------------


def test_index_resolve_roundtrip_byte_identical(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    sha = hashlib.sha256(REQ + RESP).hexdigest()

    r = _post_traffic(env, eid, _entry(rel_req, rel_resp, sha=sha))
    assert r.status_code == 200, r.text
    tid = r.json()["id"]

    full = env["client"].get(
        f"/engagements/{eid}/traffic/{tid}", headers={"Authorization": f"Bearer {env['main']}"}
    ).json()
    assert full["mode"] == "full"
    assert full["request"] == REQ.decode()
    assert full["response"] == RESP.decode()
    assert full["sha256"] == sha
    assert full["corrupt"] is False


def test_resolve_marks_corrupt_on_sha256_mismatch(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    sha = hashlib.sha256(REQ + RESP).hexdigest()
    tid = _post_traffic(env, eid, _entry(rel_req, rel_resp, sha=sha)).json()["id"]

    # 篡改请求文件 → 还原校验失败 → traffic_corrupt
    with open(os.path.join(env["traffic_root"], rel_req), "wb") as f:
        f.write(b"GET /evil HTTP/1.1\r\nHost: allowed.example.com\r\n\r\n")

    full = env["client"].get(
        f"/engagements/{eid}/traffic/{tid}", headers={"Authorization": f"Bearer {env['main']}"}
    ).json()
    assert full["corrupt"] is True
    assert full["traffic_corrupt"] is True


def test_resolve_marks_corrupt_on_missing_chunk(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    sha = hashlib.sha256(REQ + RESP).hexdigest()
    entry = _entry(rel_req, rel_resp, sha=sha)
    entry["chunk_count"] = 2  # 声明 2 分片，但只有 .0 无 .1 → 缺失
    tid = _post_traffic(env, eid, entry).json()["id"]

    full = env["client"].get(
        f"/engagements/{eid}/traffic/{tid}", headers={"Authorization": f"Bearer {env['main']}"}
    ).json()
    assert full["corrupt"] is True


# ---------------------------------------------------------------------------
# 验收 2：digest ≤digest_budget、截断含 sha256 引用；for_model=false 全量（F2）
# ---------------------------------------------------------------------------


def test_digest_format_and_budget(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    big_body = b"A" * 5000  # 超过 prefix 2KB + suffix 512B → 触发截断
    req = b"GET /big HTTP/1.1\r\nHost: allowed.example.com\r\n\r\n" + big_body
    resp = b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n" + b"B" * 3000
    rel_req, rel_resp = _write_traffic(env["traffic_root"], req, resp, seq=2)
    sha = hashlib.sha256(req + resp).hexdigest()

    tid = _post_traffic(env, eid, {
        "method": "GET",
        "url": "http://allowed.example.com/big",
        "host": "allowed.example.com",
        "status": 200,
        "req_path": rel_req,
        "resp_path": rel_resp,
        "req_bytes": len(req),
        "resp_bytes": len(resp),
        "sha256": sha,
        "chunk_count": 1,
    }).json()["id"]

    digest = env["client"].get(
        f"/engagements/{eid}/traffic/{tid}", params={"for_model": "true"},
        headers={"Authorization": f"Bearer {env['main']}"},
    ).json()
    assert digest["mode"] == "digest"
    assert digest["digest_budget"] <= 8192
    d = digest["digest"]
    assert "REQUEST" in d and "RESPONSE" in d
    assert "truncated" in d.lower()
    assert sha in d  # 截断处含全量 sha256 引用
    assert len(d.encode("utf-8")) <= digest["digest_budget"]

    # for_model=false → 全量（报告/审计/replay 走全量）
    full = env["client"].get(
        f"/engagements/{eid}/traffic/{tid}", params={"for_model": "false"},
        headers={"Authorization": f"Bearer {env['main']}"},
    ).json()
    assert full["mode"] == "full"
    assert full["request"].endswith("A" * 5000)
    assert full["response"].endswith("B" * 3000)


# ---------------------------------------------------------------------------
# 验收 3：fail-closed —— 白名单外 host 不被索引；no_capture_hosts 双保险（F5）
# ---------------------------------------------------------------------------


def test_fail_closed_rejects_host_outside_allowlist(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    entry = _entry(rel_req, rel_resp, host="evil.example.org")

    r = _post_traffic(env, eid, entry)
    assert r.status_code == 403, r.text
    assert r.json()["error_code"] == "SCOPE_DENIED"

    # 未被索引
    rows = env["client"].get(
        f"/engagements/{eid}/traffic", headers={"Authorization": f"Bearer {env['main']}"}
    ).json()
    assert rows == []


def test_fail_closed_empty_allowlist_rejects_everything(env):
    eid = _seed_engagement(env["db_path"])  # 无 target → allow 为空
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    r = _post_traffic(env, eid, _entry(rel_req, rel_resp))
    assert r.status_code == 403
    assert r.json()["error_code"] == "SCOPE_DENIED"


def test_no_capture_hosts_wins_even_if_authorized(env):
    scope_policy = {"capture_proxy": {"no_capture_hosts": ["api.anthropic.com", "cairn-server"]}}
    eid = _seed_engagement(env["db_path"], scope_policy=scope_policy,
                           targets=[("api.anthropic.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    entry = _entry(rel_req, rel_resp, host="api.anthropic.com")
    r = _post_traffic(env, eid, entry)
    assert r.status_code == 403
    assert r.json()["error_code"] == "SCOPE_DENIED"


# ---------------------------------------------------------------------------
# F8：代理受限 token 鉴权（写入口非 Bearer 主 token）
# ---------------------------------------------------------------------------


def test_proxy_write_endpoint_requires_capture_token(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    entry = _entry(rel_req, rel_resp)

    # 无 token → 401 AUTH_REQUIRED
    r = env["client"].post(f"/engagements/{eid}/traffic", json=entry)
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_REQUIRED"

    # Bearer 主 token → 401 AUTH_INVALID（代理必须用受限 capture token）
    r = env["client"].post(
        f"/engagements/{eid}/traffic", json=entry,
        headers={"Authorization": f"Bearer {env['main']}"},
    )
    assert r.status_code == 401
    assert r.json()["error_code"] == "AUTH_INVALID"

    # 受限 capture token → 200 落库
    r = env["client"].post(
        f"/engagements/{eid}/traffic", json=entry,
        headers={"Authorization": f"Bearer {env['capture']}"},
    )
    assert r.status_code == 200, r.text


def test_read_endpoints_require_main_token(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    # GET /traffic 非豁免 → 缺 token 401
    r = env["client"].get(f"/engagements/{eid}/traffic")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# 验收 4：C12 —— client_ip → worker 归属
# ---------------------------------------------------------------------------


def test_resolve_client_bridge_mapping():
    ip_map = {"172.17.0.3": "worker-1", "172.17.0.4": "worker-2"}
    assert capture_svc.resolve_client("172.17.0.3", ip_map) == "worker-1"
    assert capture_svc.resolve_client("172.17.0.4", ip_map) == "worker-2"


def test_resolve_client_host_network_returns_none():
    # host 网络共享 IP → 无法区分 → client=NULL
    assert capture_svc.resolve_client("127.0.0.1", {}) is None
    assert capture_svc.resolve_client(None, {"1.2.3.4": "w"}) is None


def test_index_stores_client_and_client_ip(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    r = _post_traffic(env, eid, _entry(rel_req, rel_resp, client="worker-1", client_ip="172.17.0.3"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client"] == "worker-1"
    assert body["client_ip"] == "172.17.0.3"

    # host 网络：client=None，仍记录 client_ip
    r2 = _post_traffic(env, eid, _entry(rel_req, rel_resp, client=None, client_ip="192.168.1.50"))
    assert r2.status_code == 200, r2.text
    assert r2.json()["client"] is None
    assert r2.json()["client_ip"] == "192.168.1.50"


# ---------------------------------------------------------------------------
# 验收 5：C11 —— 白名单热刷新（Dispatcher 侧 + Proxy 侧）
# ---------------------------------------------------------------------------


def test_dispatcher_whitelist_refresh_add_remove():
    from cairn.dispatcher.capture.client import CaptureWhitelist, derive_whitelist

    targets = [{"value": "example.com", "kind": "domain", "scope_status": "authorized"}]
    wl = derive_whitelist(targets, {"capture_proxy": {"no_capture_hosts": ["api.anthropic.com"]}})
    assert wl.allowed("example.com") is True
    assert wl.allowed("evil.com") is False
    assert wl.allowed("api.anthropic.com") is False

    # 增 target → refresh → ≤1 interval 内生效
    targets.append({"value": "new.example.net", "kind": "domain", "scope_status": "authorized"})
    wl.update_allow({t["value"] for t in targets if t["scope_status"] == "authorized"})
    assert wl.allowed("new.example.net") is True

    # 删 target → refresh → 不再允许
    targets = [t for t in targets if t["value"] != "example.com"]
    wl.update_allow({t["value"] for t in targets if t["scope_status"] == "authorized"})
    assert wl.allowed("example.com") is False

    # kill → 置空
    wl.clear()
    assert wl.allowed("new.example.net") is False


def test_capture_proxy_manager_start_refresh_stop(env):
    eid = _seed_engagement(env["db_path"], targets=[("example.com", "domain", "authorized")])
    conn = db_module.connect(env["db_path"])
    manager = CaptureProxyManager(engine_factory=lambda scope: FakeProxyEngine())
    state = manager.start_engagement(
        conn, eid, {"capture_proxy": {"port": 8080, "no_capture_hosts": ["api.anthropic.com"]}}
    )
    assert state.engine.is_running()
    assert state.port == 8080
    assert state.allowed("example.com") is True
    assert state.allowed("api.anthropic.com") is False

    # C11：targets 增删后 refresh（≤ interval 轮询语义）
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, criticality, added_by, added_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (db_module.next_id(conn, "target", eid), eid, "other.example.org", "domain", "authorized", 0.5, "human", _utc()),
    )
    conn.commit()
    manager.refresh(conn, eid)
    assert state.allowed("other.example.org") is True

    # C3/C11：kill → 停引擎 + 白名单置空
    assert manager.stop_engagement(eid) is True
    assert state.engine.is_running() is False
    assert state.allowed("example.com") is False
    conn.close()


# ---------------------------------------------------------------------------
# 验收 6（C13）：CA 生成 at-rest 0700 受限权限
# ---------------------------------------------------------------------------


def test_generate_ca_chmod_0700(tmp_path):
    ca_dir = str(tmp_path / "ca")
    r = generate_ca("eng_001", ca_dir)
    assert os.path.isfile(r["key_path"])
    assert os.path.isfile(r["cert_path"])
    # C13：密钥文件 0700
    assert oct(os.stat(r["key_path"]).st_mode & 0o777) == "0o700"
    assert oct(os.stat(r["cert_path"]).st_mode & 0o777) == "0o700"


# ---------------------------------------------------------------------------
# 关联 + derive（C2）：以捕获字节派生 finding_http_evidence（经 22 服务函数）
# ---------------------------------------------------------------------------


def test_link_finding_traffic(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    sha = hashlib.sha256(REQ + RESP).hexdigest()
    tid = _post_traffic(env, eid, _entry(rel_req, rel_resp, sha=sha)).json()["id"]

    target_id = db_module.connect(env["db_path"]).execute(
        "SELECT id FROM targets WHERE engagement_id=?", (eid,)
    ).fetchone()[0]
    fid = _seed_finding(env["db_path"], eid, target_id)

    # 规范实现：capture.link_finding_traffic（22 的 POST /findings/{fid}/traffic 委托本函数）
    conn = db_module.connect(env["db_path"])
    link_ids = capture_svc.link_finding_traffic(
        conn, fid, [tid], role=TrafficLinkRole.trigger.value, source=HttpEvidenceSource.captured.value
    )
    assert len(link_ids) == 1
    link = conn.execute("SELECT role, source FROM finding_traffic_links WHERE finding_id=?", (fid,)).fetchone()
    assert dict(link) == {"role": "trigger", "source": "captured"}
    te = conn.execute("SELECT finding_linked FROM traffic_entries WHERE id=?", (tid,)).fetchone()
    assert te["finding_linked"] == 1
    conn.close()

    # 幂等：重复关联同 (finding, traffic, role) 不新增
    conn = db_module.connect(env["db_path"])
    again = capture_svc.link_finding_traffic(
        conn, fid, [tid], role=TrafficLinkRole.trigger.value, source=HttpEvidenceSource.captured.value
    )
    assert again == []
    count = conn.execute("SELECT COUNT(*) FROM finding_traffic_links WHERE finding_id=?", (fid,)).fetchone()[0]
    assert count == 1
    conn.close()


def test_derive_http_from_capture_via_22_service(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    sha = hashlib.sha256(REQ + RESP).hexdigest()
    tid = _post_traffic(env, eid, _entry(rel_req, rel_resp, sha=sha)).json()["id"]
    target_id = db_module.connect(env["db_path"]).execute(
        "SELECT id FROM targets WHERE engagement_id=?", (eid,)
    ).fetchone()[0]
    fid = _seed_finding(env["db_path"], eid, target_id)

    # 依赖 22 的真实 services.findings.add_http_evidence（经 importlib 懒加载）
    conn = db_module.connect(env["db_path"])
    capture_svc.derive_http_from_capture(conn, fid, tid, traffic_root=env["traffic_root"])
    he = conn.execute(
        "SELECT * FROM finding_http_evidence WHERE finding_id=? AND traffic_id=?",
        (fid, tid),
    ).fetchone()
    assert he is not None
    assert he["source"] == "captured"
    assert he["method"] == "GET"
    assert he["url"].endswith("/login?x=1")
    assert "Host: allowed.example.com" in (he["request_headers"] or "")
    assert he["response_status"] == 200
    assert "Content-Type: text/html" in (he["response_headers"] or "")
    assert he["response_body"] == "ok!"

    # C2：trigger 关联同时建立
    link = conn.execute(
        "SELECT role, source FROM finding_traffic_links WHERE finding_id=? AND traffic_id=?",
        (fid, tid),
    ).fetchone()
    assert dict(link) == {"role": "trigger", "source": "captured"}

    # 幂等：同一 traffic_id 不重复登记（打断 22 add_http_evidence→_derive_http 互递归）
    capture_svc.derive_http_from_capture(conn, fid, tid, traffic_root=env["traffic_root"])
    count = conn.execute(
        "SELECT COUNT(*) FROM finding_http_evidence WHERE finding_id=? AND traffic_id=?",
        (fid, tid),
    ).fetchone()[0]
    assert count == 1
    conn.close()


# ---------------------------------------------------------------------------
# 验收 6（C2 对账）：capture_gap 标记（spec §2.5/§10.13）
# ---------------------------------------------------------------------------


def test_reconcile_capture_gap(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    target_id = db_module.connect(env["db_path"]).execute(
        "SELECT id FROM targets WHERE engagement_id=?", (eid,)
    ).fetchone()[0]
    fid = _seed_finding(env["db_path"], eid, target_id)

    # agent_typed http 证据（声明了请求但无捕获）→ captured=0 且 declared>0 → gap
    conn = db_module.connect(env["db_path"])
    conn.execute(
        "INSERT INTO finding_http_evidence (id, finding_id, seq, source, method, url, captured_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (db_module.next_id(conn, "http_evidence", eid), fid, 1, "agent_typed", "GET", "http://x/", _utc()),
    )
    conn.commit()
    conn.close()

    conn = db_module.connect(env["db_path"])
    result = capture_svc.reconcile(conn, eid, min_capture_ratio=2.0, min_capture_abs_diff=3)
    assert result["captured_count"] == 0
    assert result["declared_count"] == 1
    assert result["capture_gap"] is True

    # 落 scheduler_state 供 30/40 周期对账读取
    conn = db_module.connect(env["db_path"])
    gap = capture_svc.read_capture_gap(conn, eid)
    assert gap is not None and gap["capture_gap"] is True
    conn.close()


def test_reconcile_no_gap_when_captured_sufficient(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    _post_traffic(env, eid, _entry(rel_req, rel_resp))  # captured=1
    conn = db_module.connect(env["db_path"])
    result = capture_svc.reconcile(conn, eid)  # declared=0 → 无 gap
    assert result["capture_gap"] is False
    conn.close()


def test_capture_gap_findings_agent_typed_without_capture(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    target_id = db_module.connect(env["db_path"]).execute(
        "SELECT id FROM targets WHERE engagement_id=?", (eid,)
    ).fetchone()[0]
    # finding A：agent_typed http 证据、无 captured 关联 → gap 候选
    fid_a = _seed_finding(env["db_path"], eid, target_id, title="A-agent-typed")
    # finding B：无 http 证据（纯命令证据类）→ 不判为缺抓
    fid_b = _seed_finding(env["db_path"], eid, target_id, title="B-command-only")

    conn = db_module.connect(env["db_path"])
    conn.execute(
        "INSERT INTO finding_http_evidence (id, finding_id, seq, source, method, url, captured_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (db_module.next_id(conn, "http_evidence", eid), fid_a, 1, "agent_typed", "GET", "http://x/", _utc()),
    )
    conn.commit()
    gaps = capture_svc.capture_gap_findings(conn, eid)
    ids = {g["id"] for g in gaps}
    assert fid_a in ids
    assert fid_b not in ids
    conn.close()


# ---------------------------------------------------------------------------
# 列表检索（C12 按 worker / since 过滤）
# ---------------------------------------------------------------------------


def test_list_traffic_filters(env):
    eid = _seed_engagement(env["db_path"], targets=[("allowed.example.com", "domain", "authorized")])
    rel_req, rel_resp = _write_traffic(env["traffic_root"], REQ, RESP)
    _post_traffic(env, eid, _entry(rel_req, rel_resp, client="worker-1", client_ip="172.17.0.3"))
    _post_traffic(env, eid, _entry(rel_req, rel_resp, client="worker-2", client_ip="172.17.0.4"))

    by_worker = env["client"].get(
        f"/engagements/{eid}/traffic", params={"client": "worker-1"},
        headers={"Authorization": f"Bearer {env['main']}"},
    ).json()
    assert len(by_worker) == 1
    assert by_worker[0]["client"] == "worker-1"

    all_rows = env["client"].get(
        f"/engagements/{eid}/traffic", headers={"Authorization": f"Bearer {env['main']}"}
    ).json()
    assert len(all_rows) == 2
