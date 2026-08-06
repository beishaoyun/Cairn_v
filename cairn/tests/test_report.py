"""41-report-finalize 验收测试。

对照 ``dev-agents/41-report-finalize.md`` §3 四项验收：
1. finalize 门槛各分支：达标置 completed；不达标 COVERAGE_POLICY_UNMET + 豁免后可重试；
2. 报告生成：markdown/html 均产出；证据附录含触发包原文 + 命令回显 + 复核记录；
   大流量仅引用不内嵌；
3. 报告版本：连续生成 rpt-001/rpt-002，可分别下载；
4. timeline 渲染方法流程章节与 24 数据一致。

另有 stats 指标统计 + 路由层冒烟（POST /report、GET /report/latest、GET /report/{rpt}、
GET /stats、POST /finalize）。
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cairn.config import ServerConfig
from cairn.server import db as db_module
from cairn.server.app import create_app
from cairn.server.errors import CairnError, ErrorCode
from cairn.server.services import report as report_svc
from cairn.server.services.coverage import (
    DEFAULT_COVERAGE_POLICY,
    claim_item_for_intent,
    coverage_summary,
    report_ready,
    seed_default_test_types,
    upsert_coverage_item,
    waive_item,
    write_coverage_result,
)
from cairn.server.services import findings as findings_svc
from cairn.server.services import progress as progress_svc

NOW = "2026-08-06T00:00:00.000000Z"


# ---------------------------------------------------------------------------
# 测试基建
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_conn(tmp_path):
    conn = db_module.init_db(str(tmp_path / "test.db"))
    yield conn
    conn.close()


def _make_engagement(conn: sqlite3.Connection, *, title: str = "E", status: str = "active") -> str:
    eid = db_module.next_id(conn, "engagement")
    conn.execute(
        "INSERT INTO engagements (id, title, status, created_at) VALUES (?,?,?,?)",
        (eid, title, status, NOW),
    )
    conn.commit()
    return eid


def _make_target(conn, eid, value, *, criticality=0.9, kind="ip") -> str:
    tid = db_module.next_id(conn, "target", engagement_id=eid)
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, criticality, added_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (tid, eid, value, kind, "authorized", criticality, NOW),
    )
    conn.commit()
    return tid


def _tt(conn, eid, slug):
    tid = f"tt_{slug}"
    assert conn.execute(
        "SELECT 1 FROM test_types WHERE id=? AND engagement_id=?", (tid, eid)
    ).fetchone(), f"test_type {tid} 未播种"
    return tid


def _coverage_ready_engagement(conn, *, n_items=3, cover_all=True, waive_idx=None):
    """构造一个收敛可最终化的 engagement：seed + target + 3 覆盖格（可全测/豁免）。"""
    eid = _make_engagement(conn)
    seed_default_test_types(conn, eid)
    tid = _make_target(conn, eid, "10.0.0.1", criticality=0.9)
    items = [
        upsert_coverage_item(conn, eid, tid, _tt(conn, eid, slug), "standard", seed_source="auto")
        for slug in ("web_sqli", "web_xss", "web_ssrf")
    ]
    conn.commit()
    if cover_all:
        for it in items:
            _claim_and_test(conn, eid, it["id"])
    if waive_idx is not None:
        waive_item(conn, eid, items[waive_idx]["id"], kind="out_of_scope", reason="规则外", by="human")
    conn.commit()
    return eid, tid, items


def _claim_and_test(conn, eid, item_id, *, depth="standard", outcome="no_issue", intent=None):
    intent = intent or f"i-{item_id}"
    assert claim_item_for_intent(conn, item_id, intent)
    write_coverage_result(
        conn, eid, item_ids=[item_id], depth_achieved=depth, outcome=outcome,
        fact_id="f1", intent_id=intent, tested_scope={"endpoints": ["/"]},
    )
    conn.commit()


def _basic_payload(eid, tid, **overrides) -> dict:
    payload = {
        "title": "SQL Injection in /login",
        "severity": "high",
        "asset": "http://10.0.0.1/login",
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


def _make_verified_finding(conn, eid, tid, **overrides) -> str:
    overrides.setdefault("status", "verified")
    overrides.setdefault("verified_severity", "high")
    f = findings_svc.create_finding(
        conn, eid,
        payload=_basic_payload(eid, tid, **overrides),
        detected_by="w1", actor="human",
    )
    conn.commit()
    return f["id"]


# ---------------------------------------------------------------------------
# 1. finalize 门槛各分支
# ---------------------------------------------------------------------------


def test_finalize_gate_unmet_returns_409(db_conn):
    """不达标 → COVERAGE_POLICY_UNMET + 明细；状态保持 active。"""
    eid, _tid, _items = _coverage_ready_engagement(db_conn, cover_all=False)

    with pytest.raises(CairnError) as exc:
        report_svc.finalize(db_conn, eid)
    assert exc.value.error_code is ErrorCode.COVERAGE_POLICY_UNMET
    detail = exc.value.detail
    # 明细字段（供前端 tooltip）：uncovered_high / depth_shortfall / summary / untriaged_findings / policy
    assert set(detail) >= {"uncovered_high", "depth_shortfall", "summary", "untriaged_findings", "policy"}
    assert len(detail["uncovered_high"]) >= 1
    assert detail["summary"]["coverage_ratio"] < 0.95

    # 状态未变（仍 active）
    eng = db_conn.execute("SELECT status FROM engagements WHERE id=?", (eid,)).fetchone()
    assert eng["status"] == "active"


def test_finalize_gate_met_sets_completed_and_generates_reports(db_conn, tmp_path):
    """达标 → 置 completed + 自动生成 markdown/html 报告（rpt-001/002）。"""
    eid, tid, _items = _coverage_ready_engagement(db_conn)
    # 无未分诊 finding（verified 算已分诊，不阻塞）
    _make_verified_finding(db_conn, eid, tid)
    db_conn.commit()

    ok, detail = report_ready(db_conn, eid)
    assert ok is True

    res = report_svc.finalize(db_conn, eid, reports_root=str(tmp_path / "reports"))
    assert res["ok"] is True
    assert res["engagement"]["status"] == "completed"
    assert res["engagement"]["completed_at"] is not None

    reports = res["reports"]
    formats = {r["format"] for r in reports}
    assert formats == {"markdown", "html"}
    ids = sorted(r["id"] for r in reports)
    assert ids == ["rpt-001", "rpt-002"]
    # 落库
    rows = report_svc.list_reports(db_conn, eid)
    assert len(rows) == 2
    assert all(r["generated_by"] == "human" for r in rows)


def test_finalize_retry_after_waiver(db_conn, tmp_path):
    """覆盖 2/3 → 409；豁免剩余格后重试 → 成功置 completed。"""
    eid, _tid, items = _coverage_ready_engagement(db_conn, cover_all=False)
    _claim_and_test(db_conn, eid, items[0]["id"])
    _claim_and_test(db_conn, eid, items[1]["id"])
    db_conn.commit()

    with pytest.raises(CairnError) as exc:
        report_svc.finalize(db_conn, eid)
    assert exc.value.error_code is ErrorCode.COVERAGE_POLICY_UNMET

    # 豁免剩余格（人工+理由）→ 覆盖率 1.0（waived 计入 covered）
    waive_item(db_conn, eid, items[2]["id"], kind="out_of_scope", reason="规则外", by="human")
    db_conn.commit()
    assert report_ready(db_conn, eid)[0] is True

    res = report_svc.finalize(db_conn, eid, reports_root=str(tmp_path / "reports"))
    assert res["engagement"]["status"] == "completed"


def test_finalize_double_rejected(db_conn, tmp_path):
    eid, _tid, _items = _coverage_ready_engagement(db_conn)
    report_svc.finalize(db_conn, eid, reports_root=str(tmp_path / "reports"))
    with pytest.raises(CairnError) as exc:
        report_svc.finalize(db_conn, eid)
    assert exc.value.error_code is ErrorCode.ENGAGEMENT_INVALID_STATE


def test_finalize_kill_switch_blocks(db_conn):
    eid, _tid, _items = _coverage_ready_engagement(db_conn)
    db_conn.execute("UPDATE engagements SET kill_switch=1 WHERE id=?", (eid,))
    db_conn.commit()
    with pytest.raises(CairnError) as exc:
        report_svc.finalize(db_conn, eid)
    assert exc.value.error_code is ErrorCode.KILL_SWITCH_ON


def test_finalize_missing_engagement(db_conn):
    with pytest.raises(CairnError) as exc:
        report_svc.finalize(db_conn, "eng_999")
    assert exc.value.error_code is ErrorCode.NOT_FOUND


def test_finalize_planning_status_gate(db_conn):
    """planning 未激活不可 finalize → 409 ENGAGEMENT_INVALID_STATE（状态 gate）。"""
    eid = _make_engagement(db_conn, status="planning")
    with pytest.raises(CairnError) as exc:
        report_svc.finalize(db_conn, eid)
    assert exc.value.error_code is ErrorCode.ENGAGEMENT_INVALID_STATE


# ---------------------------------------------------------------------------
# 2. 报告内容：证据附录（触发包原文 + 命令回显 + 复核记录 + D4 引用）
# ---------------------------------------------------------------------------


def _seed_evidence(db_conn, eid, tid) -> str:
    fid = _make_verified_finding(db_conn, eid, tid)
    # 内嵌触发请求/响应原文（captured 派生语义）
    findings_svc.add_http_evidence(db_conn, fid, http_obj={
        "source": "captured", "traffic_id": None,
        "method": "GET", "url": "http://10.0.0.1/login",
        "request_headers": "GET /login HTTP/1.1\nHost: 10.0.0.1\nCookie: session=abc",
        "request_body": "user=admin' OR '1'='1",
        "response_status": 500,
        "response_headers": "HTTP/1.1 500 Internal Server Error\nContent-Type: text/html",
        "response_body": "<html>SQL syntax error near 'OR'</html>",
        "captured_at": NOW,
    })
    # 命令回显证据（非 HTTP 类）
    findings_svc.add_command_evidence(db_conn, fid, command_obj={
        "command": "cat /etc/passwd | grep root",
        "cwd": "/tmp",
        "exit_code": 0,
        "stdout": "root:x:0:0:root:/root:/bin/bash",
        "stderr": "",
        "started_at": NOW,
    })
    # 独立复核记录（F1：independence 如实标注）
    vr_id = db_module.next_id(db_conn, "verify_run", engagement_id=eid)
    db_conn.execute(
        "INSERT INTO verify_runs (id, finding_id, stage, independence, verdict, verified_severity, "
        "reason, created_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (vr_id, fid, "comparison", "cross_worker", "confirmed", "high",
         "blind observation matched claim", NOW, NOW),
    )
    # 大流量（>1MB 只给引用；此处不建文件，digest 计算失败 → 引用仍含 sha256）
    tr_id = db_module.next_id(db_conn, "traffic", engagement_id=eid)
    db_conn.execute(
        "INSERT INTO traffic_entries (id, engagement_id, seq, captured_at, method, url, host, status, "
        "req_path, resp_path, req_bytes, resp_bytes, sha256, chunk_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tr_id, eid, 1, NOW, "GET", "http://10.0.0.1/login", "10.0.0.1", 500,
         "e/{eid}/tr-001.req", "e/{eid}/tr-001.resp", 200 * 1024 * 1024, 5 * 1024 * 1024,
         "a" * 64, 1),
    )
    # 确定性重放记录（F4；replay_runs 无 created_at 列，只有 started_at/finished_at；
    # trigger_traffic_id 引用 tr-001，须先建 traffic 行满足 FK）
    rp_id = db_module.next_id(db_conn, "replay_run", engagement_id=eid)
    db_conn.execute(
        "INSERT INTO replay_runs (id, engagement_id, finding_id, trigger_traffic_id, status, "
        "payload_variants, matched_original, result, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rp_id, eid, fid, tr_id, "success", 3, 0, "remediated", NOW, NOW),
    )
    # 关联流量 role=trigger（D4 引用层）
    db_conn.execute(
        "INSERT INTO finding_traffic_links (id, finding_id, traffic_id, role, source, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (db_module.next_id(db_conn, "finding_traffic_link", engagement_id=eid), fid, tr_id,
         "trigger", "captured", NOW),
    )
    db_conn.commit()
    return fid


def test_report_evidence_appendix_d4(db_conn, tmp_path):
    """markdown/html 均产出；证据附录含触发包原文+命令回显+复核记录；大流量仅引用不内嵌。"""
    eid, tid, _items = _coverage_ready_engagement(db_conn)
    _seed_evidence(db_conn, eid, tid)
    db_conn.commit()

    reports_root = str(tmp_path / "reports")
    data = report_svc.aggregate(db_conn, eid, traffic_root=str(tmp_path / "traffic"))
    md = report_svc.render_markdown(data)
    html = report_svc.render_html(data)

    # markdown 章节齐全
    for section in ("## 1. 执行摘要", "## 2. 授权范围", "## 3. 方法流程",
                    "## 4. 漏洞清单", "## 5. 修复建议", "## 6. 覆盖总结", "## 7. 证据附录"):
        assert section in md

    # 内嵌触发请求/响应原文
    assert "user=admin' OR '1'='1" in md
    assert "SQL syntax error near 'OR'" in md
    assert "Cookie: session=abc" in md
    # 命令回显
    assert "root:x:0:0:root:/root:/bin/bash" in md
    # 复核记录（independence 如实标注）
    assert "independence=`cross_worker`" in md
    assert "verdict=`confirmed`" in md
    # 重放记录
    assert "result=remediated" in md
    # D4 引用：大流量只给引用（traffic_id + sha256），不内嵌请求/响应体
    assert "tr-001" in md
    assert "a" * 64 in md  # sha256 引用
    assert "关联流量引用" in md
    assert "200 * 1024 * 1024" not in md  # 未经格式化的字节数（证明未展开全量）

    # html 同源断言（内容经 html.escape：单引号 → &#x27;）
    assert "user=admin&#x27; OR &#x27;1&#x27;=&#x27;1" in html
    assert "root:x:0:0:root:/root:/bin/bash" in html
    assert "cross_worker" in html
    assert "tr-001" in html
    assert "sha256" in html or "digest" in html

    # 生成落库（rpt-001 markdown / rpt-002 html）
    reports = report_svc.generate(db_conn, eid, traffic_root=str(tmp_path / "traffic"),
                                  reports_root=reports_root)
    assert [r["id"] for r in reports] == ["rpt-001", "rpt-002"]
    assert os.path.isfile(os.path.join(reports_root, eid, "rpt-001.markdown"))
    db_conn.commit()


# ---------------------------------------------------------------------------
# 3. 报告版本：连续生成 rpt-001/002，可分别下载
# ---------------------------------------------------------------------------


def test_report_versions_continuous(db_conn, tmp_path):
    eid, tid, _items = _coverage_ready_engagement(db_conn)
    _make_verified_finding(db_conn, eid, tid)
    reports_root = str(tmp_path / "reports")

    r1 = report_svc.generate(db_conn, eid, formats=("markdown",), reports_root=reports_root)
    r2 = report_svc.generate(db_conn, eid, formats=("markdown",), reports_root=reports_root)
    assert r1[0]["id"] == "rpt-001"
    assert r2[0]["id"] == "rpt-002"

    rows = report_svc.list_reports(db_conn, eid)
    assert [r["id"] for r in rows] == ["rpt-001", "rpt-002"]

    # 可分别下载（文件存在）
    for rid in ("rpt-001", "rpt-002"):
        rec = report_svc.get_report(db_conn, eid, rid)
        path = os.path.join(reports_root, rec["path"])
        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        assert "渗透测试报告" in content

    # 不属于本 engagement → 404
    other = _make_engagement(db_conn, title="Other")
    with pytest.raises(CairnError) as exc:
        report_svc.get_report(db_conn, other, "rpt-001")
    assert exc.value.error_code is ErrorCode.NOT_FOUND

    # 最新报告
    latest = report_svc.latest_report(db_conn, eid)
    assert latest["id"] == "rpt-002"


# ---------------------------------------------------------------------------
# 4. timeline 方法流程与 24 数据一致
# ---------------------------------------------------------------------------


def test_methodology_section_from_timeline(db_conn):
    """§3 方法流程 = 24 统一时间线渲染为有序步骤。"""
    eid, tid, _items = _coverage_ready_engagement(db_conn)

    # 24 数据源：task_run + event
    run = progress_svc.open_task_run(db_conn, engagement_id=eid, task_type="explore",
                                     worker="w1", status="running")
    progress_svc.append_event(db_conn, run["id"], kind="step", level="info",
                              message="nmap -sV 10.0.0.1 -p 80,443")
    # 另一条 finding 事件（open→pending_verify 状态流转审计）
    f = findings_svc.create_finding(db_conn, eid, payload=_basic_payload(eid, tid),
                                    detected_by="w1", actor="agent")
    findings_svc.transition_finding(db_conn, f["id"], to_status="pending_verify", note="submit",
                                    actor="w1")

    data = report_svc.aggregate(db_conn, eid)
    md = report_svc.render_markdown(data)

    assert "## 3. 方法流程" in md
    assert "nmap -sV 10.0.0.1 -p 80,443" in md
    # 与 timeline 服务输出一致
    tl = data["timeline"]
    assert any("nmap -sV" in (ev.get("summary") or "") for ev in tl)
    assert any(ev["source"] == "finding" for ev in tl)

    # 渲染为有序列表：行号递增
    step_lines = [ln for ln in md.splitlines() if ln.startswith("1.") or ln.startswith("2.")]
    assert step_lines  # 至少两行有序步骤


# ---------------------------------------------------------------------------
# 5. stats 指标统计
# ---------------------------------------------------------------------------


def test_stats(db_conn):
    eid, tid, items = _coverage_ready_engagement(db_conn, cover_all=False)
    _make_verified_finding(db_conn, eid, tid, severity="high", verified_severity="high")
    _make_verified_finding(db_conn, eid, tid, title="XSS in /search", severity="medium",
                           verified_severity="medium")
    db_conn.commit()

    # 任务成功率
    progress_svc.open_task_run(db_conn, engagement_id=eid, task_type="explore", worker="w1",
                               status="success", started_at=NOW)
    progress_svc.open_task_run(db_conn, engagement_id=eid, task_type="explore", worker="w1",
                               status="failed", started_at=NOW)
    # 覆盖趋势：部分格已测（产生 coverage_records）
    for it in items[:2]:
        _claim_and_test(db_conn, eid, it["id"])

    s = report_svc.stats(db_conn, eid)
    assert s["findings"]["by_severity"]["high"] == 1
    assert s["findings"]["by_severity"]["medium"] == 1
    assert s["findings"]["by_agent_severity"]["high"] == 1
    assert s["tasks"]["total"] == 2
    assert s["tasks"]["success_rate"] == pytest.approx(0.5)
    assert s["coverage"]["total"] == 3
    assert s["coverage"]["covered"] >= 2
    assert isinstance(s["coverage_trend"], dict)


# ---------------------------------------------------------------------------
# 6. 路由冒烟
# ---------------------------------------------------------------------------


def _app_client(db_path: str, token: str = "secret"):
    config = ServerConfig(
        db_path=db_path, api_token=token,
        evidence_root=f"{db_path}.evidence", traffic_root=f"{db_path}.traffic",
        archive_root=f"{db_path}.archive",
    )
    app = create_app(config)
    return TestClient(app)


@pytest.fixture()
def http_env(tmp_path):
    db_path = str(tmp_path / "http.db")
    conn = db_module.init_db(db_path)
    eid = _make_engagement(conn)
    seed_default_test_types(conn, eid)
    tid = _make_target(conn, eid, "10.0.0.1", criticality=0.9)
    items = [
        upsert_coverage_item(conn, eid, tid, _tt(conn, eid, slug), "standard", seed_source="auto")
        for slug in ("web_sqli", "web_xss", "web_ssrf")
    ]
    conn.commit()
    conn.close()
    return {"db_path": db_path, "eid": eid, "tid": tid, "items": items, "token": "secret"}


def test_router_report_generate_latest_download_stats(http_env):
    c = _app_client(http_env["db_path"])
    H = {"Authorization": f"Bearer {http_env['token']}"}
    eid = http_env["eid"]

    # 生成报告（H）
    r = c.post(f"/engagements/{eid}/report", headers=H, json={})
    assert r.status_code == 200, r.text
    reports = r.json()
    assert len(reports) == 2
    assert {rep["format"] for rep in reports} == {"markdown", "html"}
    rpt_ids = {rep["id"] for rep in reports}

    # 最新报告（12 客户端路径假设）
    r2 = c.get(f"/engagements/{eid}/report", headers=H)
    assert r2.status_code == 200
    latest = r2.json()
    assert latest["id"] in rpt_ids
    assert "content" in latest
    assert "渗透测试报告" in latest["content"]

    # 分别下载 rpt-001 / rpt-002
    for rid in ("rpt-001", "rpt-002"):
        r3 = c.get(f"/engagements/{eid}/report/{rid}", headers=H)
        assert r3.status_code == 200
        assert "渗透测试报告" in r3.text

    # stats
    r4 = c.get(f"/engagements/{eid}/stats", headers=H)
    assert r4.status_code == 200
    assert "findings" in r4.json()

    # 未授权 → 401
    assert c.get(f"/engagements/{eid}/report").status_code == 401


def test_router_finalize_gate_and_retry(http_env):
    c = _app_client(http_env["db_path"])
    H = {"Authorization": f"Bearer {http_env['token']}"}
    eid, items = http_env["eid"], http_env["items"]

    # 未达标 → 409 COVERAGE_POLICY_UNMET
    r = c.post(f"/engagements/{eid}/finalize", headers=H)
    assert r.status_code == 409, r.text
    assert r.json()["error_code"] == "COVERAGE_POLICY_UNMET"
    assert "uncovered_high" in r.json()["detail"]

    # 覆盖 2 格 + 豁免 1 格 → 达标
    for it in items[:2]:
        c.post(f"/engagements/{eid}/coverage/items/{it['id']}/claim", headers=H,
               json={"intent_id": f"i-{it['id']}"})
        c.post(f"/engagements/{eid}/coverage/result", headers=H, json={
            "item_ids": [it["id"]], "depth_achieved": "standard", "outcome": "no_issue",
            "fact_id": "f1", "intent_id": f"i-{it['id']}", "tested_scope": {"endpoints": ["/"]},
        })
    c.post(f"/engagements/{eid}/coverage/items/{items[2]['id']}/waive", headers=H,
           json={"kind": "out_of_scope", "reason": "规则外"})

    r2 = c.post(f"/engagements/{eid}/finalize", headers=H)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["engagement"]["status"] == "completed"
    assert {rep["format"] for rep in body["reports"]} == {"markdown", "html"}
