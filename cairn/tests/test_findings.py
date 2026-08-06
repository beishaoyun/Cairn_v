"""22-findings 验收测试。

对照 ``dev-agents/22-findings.md`` §3 五项验收：
1. 状态机全路径 + 权限 gate（agent 不能置 fixed/closed）；
2. 去重：同 target + 规范化 title 第二次建 → 命中已有并追加证据（FINDING_DUP）；
3. verify 三分支落定 + max_reverify 升级（F6 / TV-20 语义）；
4. 复测账本幂等 + closed 门槛 403（TV-31/TV-44/TV-46 语义）；
5. ``triaged()`` 口径（open/pending_verify/pending_false_positive/needs_review 计未分诊；verified 不算）。
"""

from __future__ import annotations

import base64
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cairn.config import ServerConfig
from cairn.server import db as db_module
from cairn.server.app import create_app
from cairn.server.errors import ErrorCode
from cairn.server.services import findings as svc

NOW = "2026-08-06T00:00:00Z"


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


def seed_engagement(conn: sqlite3.Connection, *, with_target: bool = True, scope_policy=None) -> tuple[str, str | None]:
    eid = db_module.next_id(conn, "engagement")
    sp = json.dumps(scope_policy or {})
    conn.execute(
        "INSERT INTO engagements (id, title, status, scope_policy, created_at) VALUES (?, ?, 'active', ?, ?)",
        (eid, "E", sp, NOW),
    )
    tid = None
    if with_target:
        tid = db_module.next_id(conn, "target", engagement_id=eid)
        conn.execute(
            "INSERT INTO targets (id, engagement_id, value, kind, scope_status, added_at) "
            "VALUES (?, ?, 'example.com', 'domain', 'authorized', ?)",
            (tid, eid, NOW),
        )
    conn.commit()
    return eid, tid


def _basic_payload(eid, tid, **overrides) -> dict:
    payload = {
        "title": "SQL Injection in /login",
        "severity": "high",
        "asset": "http://example.com/login",
        "target_id": tid,
        "description": "login endpoint reflects SQL error",
        "remediation": "parameterize queries",
        "cvss_score": 8.1,
        "cvss_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        "cwe_id": "CWE-89",
        "category": "webapp",
        "references": ["https://cwe.mitre.org/data/definitions/89.html"],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. 状态机 + 权限 gate
# ---------------------------------------------------------------------------


def test_agent_can_only_create_open(conn):
    eid, tid = seed_engagement(conn)
    with pytest.raises(svc.CairnError) as exc:
        svc.create_finding(conn, eid, payload=_basic_payload(eid, tid, status="verified"), detected_by="w1", actor="agent")
    assert exc.value.error_code is ErrorCode.SCOPE_DENIED  # 403

    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    assert f["status"] == "open"
    assert f["id"].startswith("fd-")
    conn.commit()


def test_human_can_register_any_state(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(
        conn, eid,
        payload=_basic_payload(eid, tid, status="verified", verified_severity="critical", severity="critical"),
        detected_by="human", actor="human",
    )
    assert f["status"] == "verified"
    assert f["severity"] == "critical"
    conn.commit()


def test_state_machine_full_path_and_gates(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]

    # agent：open → pending_verify（合法相邻）
    f = svc.transition_finding(conn, fid, to_status="pending_verify", actor="agent")
    assert f["status"] == "pending_verify"

    # agent：pending_verify → verified（合法；severity 由 verify 决定，此处仅状态）
    f = svc.transition_finding(conn, fid, to_status="verified", actor="agent")
    assert f["status"] == "verified"

    # agent 不能置 fixed/closed/false_positive/accepted → 403
    for target in ("fixed", "closed", "false_positive", "accepted"):
        with pytest.raises(svc.CairnError) as exc:
            svc.transition_finding(conn, fid, to_status=target, actor="agent")
        assert exc.value.error_code is ErrorCode.SCOPE_DENIED, target

    # 非法相邻流转（verified → pending_verify 不在机器边）→ 409
    with pytest.raises(svc.CairnError) as exc:
        svc.transition_finding(conn, fid, to_status="pending_verify", actor="agent")
    assert exc.value.error_code is ErrorCode.ENGAGEMENT_INVALID_STATE

    # 人工可任意态（verified → needs_review → open → pending_verify → pending_false_positive → false_positive）
    svc.transition_finding(conn, fid, to_status="needs_review", actor="human")
    svc.transition_finding(conn, fid, to_status="open", actor="human")
    svc.transition_finding(conn, fid, to_status="pending_verify", actor="human")
    svc.transition_finding(conn, fid, to_status="pending_false_positive", actor="human")
    svc.transition_finding(conn, fid, to_status="false_positive", actor="human")  # 人工终态
    # 人工作为仲裁者可重开 FP（false_positive 非 closed 终态）
    f = svc.transition_finding(conn, fid, to_status="open", actor="human")
    assert f["status"] == "open"
    conn.commit()


def test_fixed_bumps_retest_round(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    assert f["retest_round"] == 0
    f = svc.transition_finding(conn, fid, to_status="fixed", actor="human")
    assert f["retest_round"] == 1
    assert f["retest_pass"] == 0
    assert f["fixed_at"] is not None
    conn.commit()


# ---------------------------------------------------------------------------
# 2. 去重（B3）
# ---------------------------------------------------------------------------


def test_dedup_key_normalization():
    k1 = svc.dedup_key("eng_001", "t-001", "SQL Injection in /login.")
    k2 = svc.dedup_key("eng_001", "t-001", "  sql injection in /login ")
    k3 = svc.dedup_key("eng_001", "t-002", "sql injection in /login")
    assert k1 == k2
    assert k1 != k3


def test_dedup_second_create_hits_existing(conn):
    eid, tid = seed_engagement(conn)
    f1 = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    # 规范化 title 相同的第二次建单 → FINDING_DUP（命中已有）
    with pytest.raises(svc.CairnError) as exc:
        svc.create_finding(conn, eid, payload=_basic_payload(eid, tid, title="sql injection in /login  "),
                           detected_by="w1", actor="agent")
    assert exc.value.error_code is ErrorCode.FINDING_DUP
    assert exc.value.detail["finding_id"] == f1["id"]
    # 未重复建单
    n = conn.execute("SELECT COUNT(*) AS n FROM findings WHERE engagement_id=?", (eid,)).fetchone()["n"]
    assert n == 1
    # 客户端「命中已有 → 追加证据」路径：给已有 finding 追加证据成功
    ev = svc.attach_evidence(conn, f1["id"], kind="screenshot", path="e-001/shot.png", mime="image/png", size=10)
    assert ev["finding_id"] == f1["id"]
    conn.commit()


def test_dedup_is_per_target(conn):
    eid, tid = seed_engagement(conn)
    svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    # 同 title 不同 target → 不判重
    tid2 = db_module.next_id(conn, "target", engagement_id=eid)
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, added_at) "
        "VALUES (?, ?, 'example.org', 'domain', 'authorized', ?)",
        (tid2, eid, NOW),
    )
    f2 = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid2, title="sql injection in /login"),
                            detected_by="w1", actor="agent")
    assert f2["id"] != "fd-001"
    conn.commit()


# ---------------------------------------------------------------------------
# 3. B1 resolve_target（scope 校验 + auto_created）
# ---------------------------------------------------------------------------


def test_resolve_target_existing(conn):
    eid, tid = seed_engagement(conn)
    # 规范化（scheme/默认端口/大小写/尾斜杠）命中已有 → 复用
    t = svc.resolve_target(conn, eid, "HTTP://Example.com:80/")
    assert t is not None
    assert t["id"] == tid
    # 子域命中父域 → 20 语义：auto_created 具体子域 target（F11：auto_created 不阻塞收敛）
    t2 = svc.resolve_target(conn, eid, "http://sub.example.com/login")
    assert t2["value"] == "sub.example.com"
    assert t2["auto_created"] == 1
    conn.commit()


def test_resolve_target_auto_create_via_scope_check(conn):
    eid, _ = seed_engagement(conn, with_target=True)
    # 增加一个授权 CIDR，让 20 的 check_scope_allowed 对未知 IP 走 auto_created 建 target
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, criticality, added_at) "
        "VALUES (?, ?, '10.0.0.0/24', 'cidr', 'authorized', 0.8, ?)",
        (db_module.next_id(conn, "target", engagement_id=eid), eid, NOW),
    )
    conn.commit()
    t = svc.resolve_target(conn, eid, "http://10.0.0.5:8080/admin")
    assert t is not None
    assert t["value"] == "10.0.0.5"
    assert t["auto_created"] == 1
    conn.commit()


def test_resolve_target_out_of_scope_denied(conn):
    eid, _ = seed_engagement(conn)
    # 与任何 authorized/prohibited target 无关的域名 → SCOPE_DENIED
    with pytest.raises(svc.CairnError) as exc:
        svc.resolve_target(conn, eid, "http://elsewhere.org/x")
    assert exc.value.error_code is ErrorCode.SCOPE_DENIED
    conn.commit()


def test_resolve_target_prohibited_denied(conn):
    eid, _ = seed_engagement(conn)
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, added_at) "
        "VALUES (?, ?, 'evil.example.com', 'domain', 'prohibited', ?)",
        (db_module.next_id(conn, "target", engagement_id=eid), eid, NOW),
    )
    conn.commit()
    with pytest.raises(svc.CairnError) as exc:
        svc.resolve_target(conn, eid, "evil.example.com")
    assert exc.value.error_code is ErrorCode.SCOPE_DENIED
    conn.commit()


def test_create_finding_with_unknown_asset_auto_target(conn):
    eid, _ = seed_engagement(conn, with_target=True)
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, added_at) "
        "VALUES (?, ?, '10.0.0.0/24', 'cidr', 'authorized', ?)",
        (db_module.next_id(conn, "target", engagement_id=eid), eid, NOW),
    )
    conn.commit()
    f = svc.create_finding(
        conn, eid,
        payload=_basic_payload(eid, None, asset="http://10.0.0.5:8080/admin", target_id=None),
        detected_by="w1", actor="agent",
    )
    t = conn.execute("SELECT * FROM targets WHERE id=?", (f["target_id"],)).fetchone()
    assert t["value"] == "10.0.0.5" and t["auto_created"] == 1  # B1 未知资产 auto_created 建 target
    conn.commit()


# ---------------------------------------------------------------------------
# 4. verify 三分支 + max_reverify 升级（F1/F6）
# ---------------------------------------------------------------------------


def _pending(fid):
    return fid


def test_verify_confirmed_sets_severity_double_track(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    svc.transition_finding(conn, fid, to_status="pending_verify", actor="agent")
    f = svc.apply_verify_runs(conn, fid, vr={
        "verdict": "confirmed", "verified_severity": "critical",
        "reason": "digest 回显 SQL 错误，与观察一致", "stage": "comparison", "independence": "cross_worker",
        "observations": [{"vuln": "SQLi", "severity": "critical"}],
    })
    assert f["status"] == "verified"
    assert f["verify_status"] == "confirmed"
    assert f["verified_severity"] == "critical"
    assert f["severity"] == "critical"  # 生效 severity 取 verified_severity（规则 27）
    assert f["agent_severity"] == "high"  # 双轨保留 agent 初判
    vr = conn.execute("SELECT * FROM verify_runs WHERE finding_id=?", (fid,)).fetchone()
    assert vr is not None and vr["verdict"] == "confirmed"
    conn.commit()


def test_verify_rejected_goes_pending_false_positive(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    svc.transition_finding(conn, fid, to_status="pending_verify", actor="agent")
    f = svc.apply_verify_runs(conn, fid, vr={"verdict": "rejected", "reason": "观察不到该漏洞"})
    assert f["status"] == "pending_false_positive"  # 非终态
    assert f["verify_status"] == "rejected"
    # 人工确认终态
    f = svc.transition_finding(conn, fid, to_status="false_positive", actor="human")
    assert f["status"] == "false_positive"
    conn.commit()


def test_verify_needs_more_bumps_reverify(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    svc.transition_finding(conn, fid, to_status="pending_verify", actor="agent")
    f = svc.apply_verify_runs(conn, fid, vr={"verdict": "needs_more_evidence", "reason": "盲注无可观测差异"})
    assert f["status"] == "open"  # ≤max_reverify 回 open 补证
    assert f["reverify_count"] == 1
    conn.commit()


def test_bump_reverify_overflow(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    assert svc.bump_reverify(conn, fid) is False  # 1
    assert svc.bump_reverify(conn, fid) is False  # 2
    assert svc.bump_reverify(conn, fid) is False  # 3（默认 max=3，3 不超限）
    assert svc.bump_reverify(conn, fid) is True   # 4 > 3 → 超限
    conn.commit()


def test_needs_more_escalates_to_needs_review(conn):
    eid, tid = seed_engagement(conn, scope_policy={"verify_policy": {"max_reverify": 1}})
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    svc.transition_finding(conn, fid, to_status="pending_verify", actor="agent")
    f = svc.apply_verify_runs(conn, fid, vr={"verdict": "needs_more_evidence", "reason": "r1"})
    assert f["status"] == "open"  # 1 ≤ max(1)
    f = svc.apply_verify_runs(conn, fid, vr={"verdict": "needs_more_evidence", "reason": "r2"})
    assert f["status"] == "needs_review"  # 2 > 1 → 升级人工，停止自动循环
    assert f["reverify_count"] == 2
    conn.commit()


def test_verify_only_writes_verdict_fields(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid, description="original desc"),
                           detected_by="w1", actor="agent")
    fid = f["id"]
    svc.transition_finding(conn, fid, to_status="pending_verify", actor="agent")
    f = svc.apply_verify_runs(conn, fid, vr={"verdict": "confirmed", "verified_severity": "medium", "reason": "x"})
    assert f["description"] == "original desc"  # verify 不改其他字段
    conn.commit()


# ---------------------------------------------------------------------------
# 5. 复测账本（C10/A2）幂等 + closed 门槛（规则 26/31）
# ---------------------------------------------------------------------------


def test_retest_ledger_idempotent(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    svc.transition_finding(conn, fid, to_status="fixed", actor="human")  # retest_round → 1

    svc.record_retest_confirmation(conn, fid, kind="replay", actor="replay-engine")
    svc.record_retest_confirmation(conn, fid, kind="replay", actor="replay-engine")  # 同轮同类型幂等
    ledger = svc.retest_pass_count(conn, fid)
    assert ledger["count"] == 1
    assert ledger["retest_round"] == 1

    svc.record_retest_confirmation(conn, fid, kind="verify", actor="w2")
    assert svc.retest_pass_count(conn, fid)["count"] == 2
    svc.record_retest_confirmation(conn, fid, kind="human", actor="human")
    assert svc.retest_pass_count(conn, fid)["count"] == 3
    # 重复任意类型不再 +1
    svc.record_retest_confirmation(conn, fid, kind="verify", actor="w2")
    assert svc.retest_pass_count(conn, fid)["count"] == 3
    # findings.retest_pass 已刷新
    f = svc._get_finding(conn, fid)
    assert f["retest_pass"] == 3
    conn.commit()


def test_closed_gate_http_class_requires_replay(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    # 标记 HTTP 类（请求/响应包证据）
    svc.add_http_evidence(conn, fid, http_obj={
        "source": "agent_typed", "method": "POST", "url": "http://example.com/login",
        "request_body": "user=admin&pass=admin", "response_status": 302,
    })
    svc.transition_finding(conn, fid, to_status="fixed", actor="human")

    # 0 次确认 → 403
    with pytest.raises(svc.CairnError) as exc:
        svc.transition_finding(conn, fid, to_status="closed", actor="human")
    assert exc.value.error_code is ErrorCode.SCOPE_DENIED

    # verify + human 各 1（retest_pass=2，2 类型）但缺 replay → 仍 403（规则 26/31）
    svc.record_retest_confirmation(conn, fid, kind="verify", actor="w2")
    svc.record_retest_confirmation(conn, fid, kind="human", actor="human")
    with pytest.raises(svc.CairnError) as exc:
        svc.transition_finding(conn, fid, to_status="closed", actor="human")
    assert exc.value.error_code is ErrorCode.SCOPE_DENIED
    assert "replay" in json.dumps(exc.value.detail)

    # 补 replay → closed 通过
    svc.record_retest_confirmation(conn, fid, kind="replay", actor="replay-engine")
    f = svc.transition_finding(conn, fid, to_status="closed", actor="human")
    assert f["status"] == "closed"
    assert f["closed_at"] is not None
    # closed 终态不可流转（即使人工）
    with pytest.raises(svc.CairnError) as exc:
        svc.transition_finding(conn, fid, to_status="open", actor="human")
    assert exc.value.error_code is ErrorCode.ENGAGEMENT_INVALID_STATE
    conn.commit()


def test_closed_gate_non_http_also_requires_replay(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid, asset="ssh://example.com"),
                           detected_by="w1", actor="agent")
    fid = f["id"]
    svc.add_command_evidence(conn, fid, command_obj={
        "command": "sshpass -p 'admin' ssh admin@example.com", "exit_code": 0, "stdout": "Last login: ...",
    })
    svc.transition_finding(conn, fid, to_status="fixed", actor="human")
    svc.record_retest_confirmation(conn, fid, kind="verify", actor="w2")
    svc.record_retest_confirmation(conn, fid, kind="human", actor="human")
    # 非 HTTP 类也必须过命令确定性重放（kind=replay）→ 403
    with pytest.raises(svc.CairnError) as exc:
        svc.transition_finding(conn, fid, to_status="closed", actor="human")
    assert exc.value.error_code is ErrorCode.SCOPE_DENIED
    svc.record_retest_confirmation(conn, fid, kind="replay", actor="replay-engine")
    f = svc.transition_finding(conn, fid, to_status="closed", actor="human")
    assert f["status"] == "closed"
    conn.commit()


def test_retest_round_not_inherited(conn):
    """轮次递增时旧轮确认不继承。"""
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    svc.transition_finding(conn, fid, to_status="fixed", actor="human")  # round=1
    svc.record_retest_confirmation(conn, fid, kind="replay", actor="replay-engine")
    svc.record_retest_confirmation(conn, fid, kind="verify", actor="w2")
    assert svc.retest_pass_count(conn, fid)["count"] == 2
    # 发现仍存在 → 回 verified，retest_round+1 新轮（retest_pass 归零）
    svc.transition_finding(conn, fid, to_status="verified", actor="human")
    svc.transition_finding(conn, fid, to_status="fixed", actor="human")  # round=2
    ledger = svc.retest_pass_count(conn, fid)
    assert ledger["retest_round"] == 2
    assert ledger["count"] == 0  # 旧轮确认不继承
    conn.commit()


# ---------------------------------------------------------------------------
# 6. triaged 口径（21 report_ready 依赖）
# ---------------------------------------------------------------------------


def test_triaged_counts_untriaged_statuses(conn):
    eid, tid = seed_engagement(conn)
    # 直接播种不同状态
    def _mk(status):
        fid = db_module.next_id(conn, "finding", engagement_id=eid)
        conn.execute(
            "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, "
            "status, description, detected_by, created_at, updated_at) VALUES (?, ?, ?, ?, 'high', 'high', ?, 'd', 'w', ?, ?)",
            (fid, eid, tid, f"f-{status}", status, NOW, NOW),
        )
    for s in ("open", "pending_verify", "pending_false_positive", "needs_review"):
        _mk(s)
    for s in ("verified", "fixed", "false_positive", "accepted", "closed"):
        _mk(s)
    conn.commit()
    # 未分诊口径：4 个；verified 不算
    assert svc.triaged(conn, eid) == 4
    # 其他 engagement 不受影响（targets.id 为全局 PK，勿在无 target 需求时建 target 撞 id）
    eid2, _ = seed_engagement(conn, with_target=False)
    assert svc.triaged(conn, eid2) == 0


# ---------------------------------------------------------------------------
# 6b. C2：captured 来源由 23 派生，本子域登记与关联
# ---------------------------------------------------------------------------


def _seed_traffic(conn, eid) -> str:
    trid = db_module.next_id(conn, "traffic", engagement_id=eid)
    conn.execute(
        "INSERT INTO traffic_entries (id, engagement_id, seq, captured_at, method, url, req_path, req_bytes) "
        "VALUES (?, ?, 1, ?, 'POST', 'http://example.com/login', 'traffic/x.req', 10)",
        (trid, eid, NOW),
    )
    conn.commit()
    return trid


def test_create_finding_captured_annotation_does_not_block_derive(conn):
    """C2：agent 上报的 http[]（即使标注 captured）以 agent_typed 注释登记，不占用
    ``(fid, traffic_id, source='captured')`` 去重槽，23 的 derive 可随后派生出真相行。"""
    eid, tid = seed_engagement(conn)
    trid = _seed_traffic(conn, eid)
    f = svc.create_finding(
        conn, eid,
        payload=_basic_payload(eid, tid, traffic_ids=[trid],
                               http=[{"source": "captured", "traffic_id": trid, "method": "POST",
                                      "url": "http://example.com/login", "request_body": "a=b",
                                      "response_status": 302}]),
        detected_by="w1", actor="agent",
    )
    rows = conn.execute(
        "SELECT source, traffic_id, method FROM finding_http_evidence WHERE finding_id=?", (f["id"],)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "agent_typed"  # 注释，不占 captured 槽
    assert rows[0]["traffic_id"] == trid
    # trigger 关联已建（traffic_ids → role=trigger）
    link = conn.execute(
        "SELECT role FROM finding_traffic_links WHERE finding_id=? AND traffic_id=?", (f["id"], trid)
    ).fetchone()
    assert link is not None and link["role"] == "trigger"
    conn.commit()


def test_add_http_evidence_calls_derive_hook(conn, monkeypatch):
    """23 提供 derive 时，add_http_evidence(source='captured') 触发派生调用点。"""
    eid, tid = seed_engagement(conn)
    trid = _seed_traffic(conn, eid)
    fid = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")["id"]
    calls = []

    def fake_derive(_conn, _fid, traffic_id, **_kw):
        calls.append((_fid, traffic_id))
        return {"method": "POST"}

    monkeypatch.setattr(svc, "_derive_http", fake_derive)
    he = svc.add_http_evidence(conn, fid, http_obj={
        "source": "captured", "traffic_id": trid, "method": "POST", "url": "http://example.com/login",
    })
    assert calls == [(fid, trid)]
    assert he["source"] == "captured" and he["traffic_id"] == trid
    conn.commit()


# ---------------------------------------------------------------------------
# 7. history 审计
# ---------------------------------------------------------------------------


def test_history_audit_stream(conn):
    eid, tid = seed_engagement(conn)
    f = svc.create_finding(conn, eid, payload=_basic_payload(eid, tid), detected_by="w1", actor="agent")
    fid = f["id"]
    svc.transition_finding(conn, fid, to_status="pending_verify", actor="agent", note="dispatch verify")
    svc.transition_finding(conn, fid, to_status="fixed", actor="human", note="人工修复")
    rows = conn.execute(
        "SELECT from_status, to_status, actor, note FROM finding_history WHERE finding_id=? ORDER BY created_at",
        (fid,),
    ).fetchall()
    transitions = [(r["from_status"], r["to_status"], r["actor"]) for r in rows]
    assert transitions == [
        (None, "open", "agent"),
        ("open", "pending_verify", "agent"),
        ("pending_verify", "fixed", "human"),
    ]
    conn.commit()


# ---------------------------------------------------------------------------
# 8. 路由层（TestClient）
# ---------------------------------------------------------------------------


def _app_and_eid(db_path):
    app = create_app(make_config(db_path))
    conn = db_module.connect(db_path)
    eid, tid = seed_engagement(conn)
    conn.close()
    return app, eid, tid


def _h():
    return {"Authorization": "Bearer secret"}


def test_router_create_list_detail_dup(db_path):
    app, eid, tid = _app_and_eid(db_path)
    c = TestClient(app)
    payload = _basic_payload(eid, tid)
    r = c.post(f"/engagements/{eid}/findings", headers=_h(), json=payload)
    assert r.status_code == 201, r.text
    fid = r.json()["id"]

    # 列表
    lst = c.get(f"/engagements/{eid}/findings", headers=_h())
    assert lst.status_code == 200
    assert lst.json()["total"] == 1
    assert lst.json()["items"][0]["id"] == fid

    # 过滤
    lst = c.get(f"/engagements/{eid}/findings?status=open", headers=_h())
    assert lst.json()["total"] == 1
    lst = c.get(f"/engagements/{eid}/findings?status=verified", headers=_h())
    assert lst.json()["total"] == 0

    # 详情
    d = c.get(f"/engagements/{eid}/findings/{fid}", headers=_h())
    assert d.status_code == 200
    assert d.json()["evidence"] == [] and d.json()["retest"]["count"] == 0

    # 去重 409 FINDING_DUP（detail 带已有 finding_id）
    r2 = c.post(f"/engagements/{eid}/findings", headers=_h(), json=_basic_payload(eid, tid, title="sql injection in /login  "))
    assert r2.status_code == 409
    body = r2.json()
    assert body["error_code"] == "FINDING_DUP"
    assert body["detail"]["finding_id"] == fid
    assert c.get(f"/engagements/{eid}/findings", headers=_h()).json()["total"] == 1


def test_router_agent_cannot_set_human_terminal(db_path):
    app, eid, tid = _app_and_eid(db_path)
    c = TestClient(app)
    fid = c.post(f"/engagements/{eid}/findings", headers=_h(), json=_basic_payload(eid, tid)).json()["id"]
    r = c.put(f"/engagements/{eid}/findings/{fid}", headers=_h(), json={"status": "closed", "actor": "agent"})
    assert r.status_code == 403
    assert r.json()["error_code"] == "SCOPE_DENIED"


def test_router_verify_and_closed_gate(db_path):
    app, eid, tid = _app_and_eid(db_path)
    c = TestClient(app)
    fid = c.post(f"/engagements/{eid}/findings", headers=_h(), json=_basic_payload(eid, tid)).json()["id"]
    # dispatch verify
    c.put(f"/engagements/{eid}/findings/{fid}", headers=_h(), json={"status": "pending_verify", "actor": "agent"})
    # verify confirmed
    r = c.post(f"/engagements/{eid}/findings/{fid}/verify", headers=_h(),
               json={"verdict": "confirmed", "verified_severity": "high", "reason": "ok", "stage": "comparison"})
    assert r.status_code == 201
    assert r.json()["status"] == "verified"
    assert r.json()["severity"] == "high"
    # closed gate 403（无复测账本）
    c.put(f"/engagements/{eid}/findings/{fid}", headers=_h(), json={"status": "fixed", "actor": "human"})
    r = c.put(f"/engagements/{eid}/findings/{fid}", headers=_h(), json={"status": "closed", "actor": "human"})
    assert r.status_code == 403
    assert r.json()["error_code"] == "SCOPE_DENIED"


def test_router_http_commands_and_traffic(db_path):
    app, eid, tid = _app_and_eid(db_path)
    c = TestClient(app)
    fid = c.post(f"/engagements/{eid}/findings", headers=_h(), json=_basic_payload(eid, tid)).json()["id"]

    # http 证据（T）
    r = c.post(f"/engagements/{eid}/findings/{fid}/http", headers=_h(),
               json={"source": "agent_typed", "method": "POST", "url": "http://example.com/login",
                     "request_body": "user=admin&pass=admin", "response_status": 302})
    assert r.status_code == 201
    assert c.get(f"/engagements/{eid}/findings/{fid}/http", headers=_h()).json()["items"][0]["method"] == "POST"

    # 命令证据
    r = c.post(f"/engagements/{eid}/findings/{fid}/commands", headers=_h(),
               json={"command": "ssh admin@example.com", "exit_code": 0, "stdout": "ok"})
    assert r.status_code == 201
    assert c.get(f"/engagements/{eid}/findings/{fid}/commands", headers=_h()).json()["items"][0]["command"].startswith("ssh")

    # 流量关联（23 的 link_finding_traffic）
    conn2 = db_module.connect(db_path)
    try:
        trid = db_module.next_id(conn2, "traffic", engagement_id=eid)
        conn2.execute(
            "INSERT INTO traffic_entries (id, engagement_id, seq, captured_at, method, url, req_path, req_bytes) "
            "VALUES (?, ?, 1, ?, 'GET', 'http://example.com/login', 'traffic/x.req', 10)",
            (trid, eid, NOW),
        )
        conn2.commit()
    finally:
        conn2.close()
    r = c.post(f"/engagements/{eid}/findings/{fid}/traffic", headers=_h(),
               json={"traffic_ids": [trid], "role": "trigger", "source": "captured"})
    assert r.status_code == 201
    d = c.get(f"/engagements/{eid}/findings/{fid}", headers=_h()).json()
    assert len(d["traffic_links"]) == 1


def test_router_evidence_whitelist_and_path_safety(db_path, tmp_path):
    cfg = make_config(db_path)
    app = create_app(cfg)
    conn = db_module.connect(db_path)
    eid, tid = seed_engagement(conn)
    conn.close()
    c = TestClient(app)
    fid = c.post(f"/engagements/{eid}/findings", headers=_h(), json=_basic_payload(eid, tid)).json()["id"]

    b64 = base64.b64encode(b"\x89PNG fake").decode()
    r = c.post(f"/engagements/{eid}/findings/{fid}/evidence", headers=_h(),
               json={"kind": "screenshot", "path": "e-001/shot.png", "mime": "image/png", "content_b64": b64})
    assert r.status_code == 201, r.text
    assert r.json()["size"] == len(b"\x89PNG fake")
    # 字节落盘（evidence_root/{eid}/e-001/shot.png）
    assert (tmp_path / "test.db.evidence" / eid / "e-001" / "shot.png").exists()

    # 白名单外类型 → 422
    r = c.post(f"/engagements/{eid}/findings/{fid}/evidence", headers=_h(),
               json={"path": "evil.php", "mime": "application/x-php", "content_b64": b64})
    assert r.status_code == 422
    assert r.json()["error_code"] == "VALIDATION"

    # 路径穿越被净化（不逃逸 evidence_root）
    r = c.post(f"/engagements/{eid}/findings/{fid}/evidence", headers=_h(),
               json={"path": "../../../escape.png", "mime": "image/png", "content_b64": b64})
    assert r.status_code == 201
    assert ".." not in r.json()["path"]


def test_router_history_and_retest(db_path):
    app, eid, tid = _app_and_eid(db_path)
    c = TestClient(app)
    fid = c.post(f"/engagements/{eid}/findings", headers=_h(), json=_basic_payload(eid, tid)).json()["id"]
    c.put(f"/engagements/{eid}/findings/{fid}", headers=_h(), json={"status": "fixed", "actor": "human"})

    # 复测确认（幂等 + 账本）
    for _ in range(2):
        r = c.post(f"/engagements/{eid}/findings/{fid}/retest", headers=_h(),
                   json={"kind": "replay", "note": "remediated", "actor": "replay-engine"})
        assert r.status_code == 201
    ledger = r.json()
    assert ledger["count"] == 1
    assert ledger["retest_round"] == 1

    # history
    hist = c.get(f"/engagements/{eid}/findings/{fid}/history", headers=_h()).json()["items"]
    assert len(hist) == 2
    assert hist[0]["to_status"] == "open" and hist[0]["actor"] == "agent"
    assert hist[1]["to_status"] == "fixed" and hist[1]["actor"] == "human"


def test_router_export(db_path):
    app, eid, tid = _app_and_eid(db_path)
    c = TestClient(app)
    c.post(f"/engagements/{eid}/findings", headers=_h(), json=_basic_payload(eid, tid))
    r = c.get(f"/engagements/{eid}/findings/export?format=json", headers=_h())
    assert r.status_code == 200
    assert len(r.json()["findings"]) == 1
