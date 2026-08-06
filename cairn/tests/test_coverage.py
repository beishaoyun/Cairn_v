"""21-coverage-engine 验收测试。

对照 ``dev-agents/21-coverage-engine.md`` §3 验收标准 + ``docs/coverage-engine-implementation-spec.md`` §5 验收要点：
1. seed 默认测试项目录行数 ≥ 25；
2. compute_gaps 排序（priority 降序）/ limit / exclude_in_progress；
3. claim/release 格子互斥（B1：并发二次 claim 第二次 False；release 仅 owner 回退）；
4. 写回幂等（C9：(item_id, intent_id) 去重，重复写不重复记账）；
5. not_applicable 无 waiver 仍算缺口（B4：只建议不置状态）；
6. report_ready 各策略分支（未达标 / 达标 / F11 auto_created 不阻塞 / depth 短欠）；
7. A3 口径统一（compute_gaps / sample_audit 同一实时 priority_score，缓存列变化不影响排序）；
8. A5 复测重建复用原行（retest_round+1，不新建）；
9. F3 抽样复核（audit_runs 落库；coverage_discrepancy → item 回退 untested + 缺口重排）；
10. D5 criticality 推断 + 人工覆盖生效；
11. C8 reason 空转升级（reason_escalation_state）；
12. 路由冒烟（矩阵/gaps/waive/result/claim/release/audit）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cairn.config import ServerConfig
from cairn.server import db as db_module
from cairn.server.app import create_app
from cairn.server.errors import CairnError, ErrorCode
from cairn.server.services import coverage as coverage_module
from cairn.server.services.coverage import (
    DEFAULT_COVERAGE_POLICY,
    DEFAULT_TEST_TYPES,
    apply_audit_verdict,
    claim_item_for_intent,
    closure_rule,
    compute_gaps,
    coverage_summary,
    infer_criticality,
    priority_score,
    reason_escalation_state,
    rebuild_for_retest,
    release_item_for_intent,
    report_ready,
    sample_audit,
    seed_default_test_types,
    upsert_coverage_item,
    waive_item,
    write_coverage_result,
)

NOW = "2026-08-06T00:00:00.000000Z"


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


def _make_target(conn, eid, value, *, criticality=0.8, auto_created=0, kind="ip") -> str:
    tid = db_module.next_id(conn, "target", engagement_id=eid)
    conn.execute(
        "INSERT INTO targets "
        "(id, engagement_id, value, kind, scope_status, criticality, auto_created, added_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (tid, eid, value, kind, "authorized", criticality, auto_created, NOW),
    )
    conn.commit()
    return tid


def _tt(conn, eid, slug):
    """取指定 slug 的 test_type id（默认目录预置后）。"""
    tid = f"tt_{slug}"
    assert conn.execute(
        "SELECT 1 FROM test_types WHERE id=? AND engagement_id=?", (tid, eid)
    ).fetchone(), f"test_type {tid} 未播种"
    return tid


def _claim_and_test(conn, eid, item_id, *, depth="standard", outcome="no_issue", intent=None,
                    tested_scope=None):
    """claim + write 一把梭（outcome=no_issue 时默认带 tested_scope）。"""
    intent = intent or f"i-{item_id}"
    assert claim_item_for_intent(conn, item_id, intent)
    write_coverage_result(
        conn, eid, item_ids=[item_id], depth_achieved=depth, outcome=outcome,
        fact_id="f1", intent_id=intent, tested_scope=tested_scope
        if tested_scope is not None else {"endpoints": ["/"]},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. seed 默认测试项目录（≥25 行；幂等；enabled=1）
# ---------------------------------------------------------------------------


def test_seed_default_test_types(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    db_conn.commit()
    rows = db_conn.execute("SELECT * FROM test_types WHERE engagement_id=?", (eid,)).fetchall()
    assert len(rows) >= 25
    assert len(rows) == len(DEFAULT_TEST_TYPES)
    assert all(r["enabled"] == 1 for r in rows)
    ids = {r["id"] for r in rows}
    assert "tt_web_sqli" in ids
    assert "tt_net_ssh_brute" in ids
    # 幂等：重复播种不翻倍
    seed_default_test_types(db_conn, eid)
    db_conn.commit()
    assert db_conn.execute(
        "SELECT COUNT(*) FROM test_types WHERE engagement_id=?", (eid,)
    ).fetchone()[0] == len(DEFAULT_TEST_TYPES)


def test_seed_default_test_types_is_importable_by_20():
    """20 的 services.scope._seed_default_test_types 已按 ``(conn, eid) -> None`` 契约导入。"""
    from cairn.server.services.scope import _seed_default_test_types  # noqa: F401
    import inspect
    sig = inspect.signature(seed_default_test_types)
    assert list(sig.parameters) == ["conn", "eid"]
    # ``from __future__ import annotations`` 使 ``-> None`` 在运行时为字符串 'None'
    assert sig.return_annotation in (None, "None")


# ---------------------------------------------------------------------------
# 2. priority_score / infer_criticality（A3/D5）
# ---------------------------------------------------------------------------


def test_priority_score_formula():
    assert priority_score(0.8, 0.9, "baseline") == pytest.approx(0.8 * 0.9 * 1.0)
    assert priority_score(0.8, 0.9, "standard") == pytest.approx(0.8 * 0.9 * 1.2)
    assert priority_score(0.8, 0.9, "deep") == pytest.approx(0.8 * 0.9 * 1.2)


def test_infer_criticality_d5():
    assert infer_criticality("public_domain") == 0.7
    assert infer_criticality("public_ip") == 0.8
    assert infer_criticality("private_cidr") == 0.6
    assert infer_criticality("private_host") == 0.5
    assert infer_criticality("unknown") == 0.5
    # 核心服务上调至 ≥0.9
    assert infer_criticality("hostname", "mysql") == 0.9
    assert infer_criticality("hostname", "ssh") == 0.9
    assert infer_criticality("hostname", "http") == 0.5


# ---------------------------------------------------------------------------
# 3. compute_gaps：排序 / limit / threshold / exclude_in_progress / 豁免排除
# ---------------------------------------------------------------------------


def test_compute_gaps_sort_limit_exclude(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t_high = _make_target(db_conn, eid, "10.0.0.1", criticality=0.9)
    t_low = _make_target(db_conn, eid, "10.0.0.2", criticality=0.4)
    item_high = upsert_coverage_item(db_conn, eid, t_high, _tt(db_conn, eid, "web_sqli"), "deep", seed_source="auto")
    item_mid = upsert_coverage_item(db_conn, eid, t_low, _tt(db_conn, eid, "web_xss"), "standard", seed_source="auto")
    item_low = upsert_coverage_item(db_conn, eid, t_low, _tt(db_conn, eid, "web_cors"), "baseline", seed_source="auto")
    db_conn.commit()

    gaps = compute_gaps(db_conn, eid)
    assert [g["item_id"] for g in gaps] == [item_high["id"], item_mid["id"], item_low["id"]]
    prios = [g["priority"] for g in gaps]
    assert prios == sorted(prios, reverse=True)
    assert gaps[0]["item_id"] == item_high["id"]
    # A3：priority 与 priority_score 公式一致
    assert gaps[0]["priority"] == pytest.approx(round(priority_score(0.9, 0.9, "deep"), 3))

    # limit
    assert len(compute_gaps(db_conn, eid, limit=2)) == 2
    assert len(compute_gaps(db_conn, eid, limit=None)) == 3

    # threshold
    gaps_t = compute_gaps(db_conn, eid, threshold=0.5)
    assert all(g["priority"] >= 0.5 for g in gaps_t)
    assert item_low["id"] not in {g["item_id"] for g in gaps_t}

    # exclude_in_progress（B1）：认领 high 格后排除
    assert claim_item_for_intent(db_conn, item_high["id"], "i001")
    db_conn.commit()
    gaps_excl = compute_gaps(db_conn, eid, exclude_in_progress=True)
    assert item_high["id"] not in {g["item_id"] for g in gaps_excl}
    gaps_all = compute_gaps(db_conn, eid, exclude_in_progress=False)
    assert item_high["id"] in {g["item_id"] for g in gaps_all}


def test_compute_gaps_excludes_waived_and_not_applicable(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1", criticality=0.9)
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "deep", seed_source="auto")
    db_conn.commit()
    waive_item(db_conn, eid, item["id"], kind="risk_accepted", reason="客户接受风险", by="human")
    db_conn.commit()
    gaps = compute_gaps(db_conn, eid)
    assert item["id"] not in {g["item_id"] for g in gaps}


# ---------------------------------------------------------------------------
# 4. claim/release 格子互斥（B1）
# ---------------------------------------------------------------------------


def test_claim_release_mutex(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    db_conn.commit()
    item_id = item["id"]

    # 首次认领成功
    assert claim_item_for_intent(db_conn, item_id, "i001") is True
    # 并发二次认领失败（B1 互斥）
    assert claim_item_for_intent(db_conn, item_id, "i002") is False
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "in_progress"
    assert row["current_intent_id"] == "i001"

    # 非 owner release 不放行（不误清他人认领）
    release_item_for_intent(db_conn, item_id, "i002")
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "in_progress"
    assert row["current_intent_id"] == "i001"

    # owner release 回退 untested
    release_item_for_intent(db_conn, item_id, "i001")
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "untested"
    assert row["current_intent_id"] is None

    # NULL 不放行：未认领格 release 是 no-op（仍 untested）
    release_item_for_intent(db_conn, item_id, "i003")
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "untested"


# ---------------------------------------------------------------------------
# 5. 写回：归属校验 / 认领校验 / 幂等 / not_applicable 只建议
# ---------------------------------------------------------------------------


def test_write_coverage_result_claims_and_idempotent(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    db_conn.commit()
    item_id = item["id"]

    _claim_and_test(db_conn, eid, item_id)
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "tested_no_issue"
    assert row["current_intent_id"] is None
    assert row["last_result"] == "no_issue"
    records = db_conn.execute(
        "SELECT * FROM coverage_records WHERE item_id=? AND intent_id=?", (item_id, "i-c-001")
    ).fetchall()
    assert len(records) == 1

    # C9 幂等：同 (item_id, intent_id) 重复写不重复记账（模拟 Dispatcher 超时重发）
    write_coverage_result(
        db_conn, eid, item_ids=[item_id], depth_achieved="standard", outcome="no_issue",
        fact_id="f1", intent_id="i-c-001", tested_scope={"endpoints": ["/"]},
    )
    db_conn.commit()
    records = db_conn.execute(
        "SELECT * FROM coverage_records WHERE item_id=?", (item_id,)
    ).fetchall()
    assert len(records) == 1


def test_write_coverage_result_rejects_unclaimed_and_foreign(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    db_conn.commit()
    item_id = item["id"]

    # 未认领格写回 → COVERAGE_ALREADY_COVERED（NULL 不放行）
    with pytest.raises(CairnError) as ei:
        write_coverage_result(
            db_conn, eid, item_ids=[item_id], depth_achieved="standard", outcome="no_issue",
            intent_id="i-x", tested_scope={"endpoints": ["/"]},
        )
    assert ei.value.error_code == ErrorCode.COVERAGE_ALREADY_COVERED

    # 他人认领（i001）后另一 intent（i002）写回 → COVERAGE_ALREADY_COVERED
    assert claim_item_for_intent(db_conn, item_id, "i001")
    with pytest.raises(CairnError) as ei:
        write_coverage_result(
            db_conn, eid, item_ids=[item_id], depth_achieved="standard", outcome="no_issue",
            intent_id="i002", tested_scope={"endpoints": ["/"]},
        )
    assert ei.value.error_code == ErrorCode.COVERAGE_ALREADY_COVERED

    # 跨 engagement 覆盖项 → COVERAGE_NOT_APPLICABLE（item 归属本 eid，传入异 eid）
    with pytest.raises(CairnError) as ei:
        write_coverage_result(
            db_conn, "eng_999", item_ids=[item_id], depth_achieved="standard", outcome="no_issue",
            intent_id="i001", tested_scope={"endpoints": ["/"]},
        )
    assert ei.value.error_code == ErrorCode.COVERAGE_NOT_APPLICABLE


def test_write_coverage_result_no_issue_requires_tested_scope(db_conn):
    """C9：outcome=no_issue 未声明 tested_scope 的写回被要求补注（验收点 14）。"""
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    db_conn.commit()
    assert claim_item_for_intent(db_conn, item["id"], "i001")
    with pytest.raises(CairnError) as ei:
        write_coverage_result(
            db_conn, eid, item_ids=[item["id"]], depth_achieved="standard", outcome="no_issue",
            intent_id="i001",
        )
    assert ei.value.error_code == ErrorCode.VALIDATION


def test_write_coverage_result_partial(db_conn):
    """C9：partial=true 的格热力图 partial 计数 + 摘要 partial 正确（验收点 14）。"""
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    db_conn.commit()
    _claim_and_test(db_conn, eid, item["id"], tested_scope={"endpoints": ["/admin"], "partial": True})
    summary = coverage_summary(db_conn, eid)
    assert summary["covered"] == 1
    assert summary["partial"] == 1
    # partial 只从 tested_scope 推导（未显式传 partial 参数）
    row = db_conn.execute(
        "SELECT partial FROM coverage_records WHERE item_id=?", (item["id"],)
    ).fetchone()
    assert row["partial"] == 1


# ---------------------------------------------------------------------------
# 6. not_applicable：只建议不置状态（B4）；waive 建 waiver 才置状态
# ---------------------------------------------------------------------------


def test_not_applicable_outcome_only_suggests(db_conn):
    """B4：explore 的 outcome=not_applicable 只写 coverage_records，item 保持 untested。"""
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    db_conn.commit()

    assert claim_item_for_intent(db_conn, item["id"], "i001")
    write_coverage_result(
        db_conn, eid, item_ids=[item["id"]], depth_achieved="standard", outcome="not_applicable",
        fact_id="f1", intent_id="i001",
    )
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item["id"],)).fetchone()
    assert row["status"] == "untested"          # 不置 not_applicable
    assert row["current_intent_id"] is None     # 认领已清
    # 仍算缺口（reason 可见为低优先补测）
    assert item["id"] in {g["item_id"] for g in compute_gaps(db_conn, eid)}
    # 已写 coverage_records 建议留痕
    assert db_conn.execute(
        "SELECT 1 FROM coverage_records WHERE item_id=? AND outcome='not_applicable'", (item["id"],)
    ).fetchone()


def test_waive_item_kind_reason_and_status(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    db_conn.commit()
    item_id = item["id"]

    # 空 reason → VALIDATION
    with pytest.raises(CairnError) as ei:
        waive_item(db_conn, eid, item_id, kind="risk_accepted", reason="  ", by="human")
    assert ei.value.error_code == ErrorCode.VALIDATION
    # 非法 kind → VALIDATION
    with pytest.raises(CairnError) as ei:
        waive_item(db_conn, eid, item_id, kind="bogus", reason="x", by="human")
    assert ei.value.error_code == ErrorCode.VALIDATION

    # not_applicable → 建 waiver + 置 status=not_applicable
    w = waive_item(db_conn, eid, item_id, kind="not_applicable", reason="该服务无此功能", by="human")
    db_conn.commit()
    assert w["kind"] == "not_applicable"
    assert w["reason"] == "该服务无此功能"
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "not_applicable"
    # 不再算缺口
    assert item_id not in {g["item_id"] for g in compute_gaps(db_conn, eid)}
    # B4：有 waiver 记录
    assert db_conn.execute(
        "SELECT 1 FROM waivers WHERE item_id=? AND kind='not_applicable'", (item_id,)
    ).fetchone()

    # risk_accepted → waived
    waive_item(db_conn, eid, item_id, kind="risk_accepted", reason="接受风险", by="human")
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item_id,)).fetchone()
    assert row["status"] == "waived"


# ---------------------------------------------------------------------------
# 7. report_ready：各策略分支
# ---------------------------------------------------------------------------


def test_report_ready_branches(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1", criticality=0.9)
    items = [
        upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, slug), "standard", seed_source="auto")
        for slug in ("web_sqli", "web_xss", "web_ssrf")
    ]
    db_conn.commit()

    # 全未覆盖 → 不 ready（存在高优先缺口）
    ok, detail = report_ready(db_conn, eid)
    assert ok is False
    assert len(detail["uncovered_high"]) == 3
    assert detail["summary"]["coverage_ratio"] == 0.0

    # 覆盖 2/3 → 覆盖率不足 + 仍有缺口
    _claim_and_test(db_conn, eid, items[0]["id"])
    _claim_and_test(db_conn, eid, items[1]["id"])
    ok, detail = report_ready(db_conn, eid)
    assert ok is False
    assert [g["item_id"] for g in detail["uncovered_high"]] == [items[2]["id"]]

    # 全部覆盖 + 无 findings → ready
    _claim_and_test(db_conn, eid, items[2]["id"])
    ok, detail = report_ready(db_conn, eid)
    assert ok is True
    assert detail["summary"]["coverage_ratio"] == 1.0
    assert detail["untriaged_findings"] == 0
    assert detail["depth_shortfall"] == 0

    # 人工豁免最后一格并回退一格 → 豁免格不计缺口，但覆盖率=2/3 <0.95 仍不 ready
    waive_item(db_conn, eid, items[2]["id"], kind="out_of_scope", reason="规则外", by="human")
    release_item_for_intent(db_conn, items[1]["id"], "i-c-002")
    db_conn.execute("UPDATE coverage_items SET status='untested', current_intent_id=NULL WHERE id=?", (items[1]["id"],))
    db_conn.commit()
    ok, detail = report_ready(db_conn, eid)
    assert ok is False


def test_report_ready_depth_shortfall(db_conn):
    """require_depth=deep：深度不足的已测格计 depth_shortfall，阻塞收敛。"""
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1", criticality=0.9)
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "deep", seed_source="auto")
    db_conn.commit()
    # 以 standard 深度测（< deep）
    _claim_and_test(db_conn, eid, item["id"], depth="standard")
    policy = dict(DEFAULT_COVERAGE_POLICY)
    policy["require_depth"] = "deep"
    ok, detail = report_ready(db_conn, eid, policy=policy)
    assert ok is False
    assert detail["depth_shortfall"] == 1
    # 以 deep 深度复测 → 达标
    rebuild_for_retest(db_conn, eid, item["target_id"], item["test_type_id"])
    db_conn.commit()
    _claim_and_test(db_conn, eid, item["id"], depth="deep", intent="i2")
    ok, detail = report_ready(db_conn, eid, policy=policy)
    assert ok is True
    assert detail["depth_shortfall"] == 0


def test_report_ready_f11_auto_created_not_blocking(db_conn):
    """F11：auto_created 目标新增覆盖项不阻塞 report-ready（不进分母/深度/缺口口径）。"""
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1", criticality=0.9)
    items = [
        upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, slug), "standard", seed_source="auto")
        for slug in ("web_sqli", "web_xss")
    ]
    # auto_created 高优先未覆盖项
    t_auto = _make_target(db_conn, eid, "10.0.0.9", criticality=0.9, auto_created=1)
    auto_item = upsert_coverage_item(
        db_conn, eid, t_auto, _tt(db_conn, eid, "web_command_injection"), "deep", seed_source="auto"
    )
    db_conn.commit()

    # 覆盖非 auto 两格后，auto 格未覆盖 → 仍 ready（F11 排除）
    for it in items:
        _claim_and_test(db_conn, eid, it["id"])
    ok, detail = report_ready(db_conn, eid)
    assert ok is True
    # 但热力图可见：全口径 total=3
    full = coverage_summary(db_conn, eid)
    assert full["total"] == 3

    # 对照：关闭 F11 排除 → auto 格阻塞收敛
    policy = json.loads(json.dumps(DEFAULT_COVERAGE_POLICY))
    policy["auto_created_closure"]["excluded_from_report_ready"] = False
    ok, detail = report_ready(db_conn, eid, policy=policy)
    assert ok is False
    assert any(g["item_id"] == auto_item["id"] for g in detail["uncovered_high"])


def test_report_ready_untriaged_findings_via_22(db_conn):
    """report_ready 的 untriaged 计数走 22 的 findings.triaged()（import 守卫 + 读依赖）。"""
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1", criticality=0.9)
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    _claim_and_test(db_conn, eid, item["id"])

    # 造一条 untriaged finding（open）→ require_all_findings_triaged 阻塞
    fid = db_module.next_id(db_conn, "finding", engagement_id=eid)
    db_conn.execute(
        "INSERT INTO findings (id, engagement_id, target_id, title, severity, agent_severity, status, "
        "description, detected_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (fid, eid, t, "SQLi", "high", "high", "open", "d", "worker-1", NOW, NOW),
    )
    db_conn.commit()
    ok, detail = report_ready(db_conn, eid)
    assert ok is False
    assert detail["untriaged_findings"] == 1
    # verified 不算未分诊（22 语义）
    db_conn.execute(
        "UPDATE findings SET status='verified' WHERE id=?", (fid,)
    )
    db_conn.commit()
    ok, detail = report_ready(db_conn, eid)
    assert ok is True
    assert detail["untriaged_findings"] == 0


# ---------------------------------------------------------------------------
# 8. A5 复测重建：复用原行 retest_round+1
# ---------------------------------------------------------------------------


def test_rebuild_for_retest_reuses_row(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "deep", seed_source="auto")
    _claim_and_test(db_conn, eid, item["id"], depth="deep", outcome="finding_created", tested_scope=None)
    db_conn.commit()
    row0 = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item["id"],)).fetchone()
    assert row0["status"] == "tested_with_finding"
    assert row0["retest_round"] == 0
    n_records_before = db_conn.execute(
        "SELECT COUNT(*) FROM coverage_records WHERE item_id=?", (item["id"],)
    ).fetchone()[0]
    assert n_records_before == 1

    # A5：复用原行（id 不变）+ retest_round+1 + 状态重置；coverage_records 历史保留
    rebuilt = rebuild_for_retest(db_conn, eid, item["target_id"], item["test_type_id"])
    db_conn.commit()
    assert rebuilt["id"] == item["id"]
    assert rebuilt["status"] == "untested"
    assert rebuilt["retest_round"] == 1
    assert rebuilt["last_result"] is None
    assert rebuilt["current_intent_id"] is None
    n_records_after = db_conn.execute(
        "SELECT COUNT(*) FROM coverage_records WHERE item_id=?", (item["id"],)
    ).fetchone()[0]
    assert n_records_after == 1  # 历史保留，未删除
    # UNIQUE 约束下不新建行
    assert db_conn.execute(
        "SELECT COUNT(*) FROM coverage_items WHERE engagement_id=? AND target_id=? AND test_type_id=?",
        (eid, item["target_id"], item["test_type_id"]),
    ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 9. F3 抽样复核：audit_runs 落库；coverage_discrepancy 回退 untested
# ---------------------------------------------------------------------------


def test_sample_audit_discrepancy_and_apply_verdict(db_conn, monkeypatch):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1", criticality=0.9)
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "deep", seed_source="auto")
    _claim_and_test(db_conn, eid, item["id"], depth="deep", outcome="finding_created", tested_scope=None)
    # 声称 finding_created 却无 finding → discrepancy 触发
    monkeypatch.setattr(coverage_module.random, "random", lambda: 1.0)  # 关闭 sampling
    targets = sample_audit(db_conn, eid)
    assert any(tg["item_id"] == item["id"] and tg["reason"] == "discrepancy" for tg in targets)

    # 落定 coverage_discrepancy → audit_runs 落库 + item 回退 untested + 缺口重排
    ar = apply_audit_verdict(db_conn, eid, item_id=item["id"], verdict="coverage_discrepancy",
                             auditor="worker-2", reason="discrepancy")
    db_conn.commit()
    assert ar["verdict"] == "coverage_discrepancy"
    assert db_conn.execute(
        "SELECT 1 FROM audit_runs WHERE id=?", (ar["id"],)
    ).fetchone()
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item["id"],)).fetchone()
    assert row["status"] == "untested"
    assert row["last_result"] == "audit_discrepancy"
    assert item["id"] in {g["item_id"] for g in compute_gaps(db_conn, eid)}  # 缺口重排


def test_sample_audit_sampling_and_covered_matches(db_conn, monkeypatch):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1", criticality=0.9)
    item = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "deep", seed_source="auto")
    _claim_and_test(db_conn, eid, item["id"], depth="deep")
    # 强制 sampling（random→0.0）
    monkeypatch.setattr(coverage_module.random, "random", lambda: 0.0)
    targets = sample_audit(db_conn, eid)
    assert any(tg["item_id"] == item["id"] and tg["reason"] == "sampling" for tg in targets)
    # covered_matches 不回退
    apply_audit_verdict(db_conn, eid, item_id=item["id"], verdict="covered_matches",
                        auditor="worker-2", reason="sampling")
    db_conn.commit()
    row = db_conn.execute("SELECT * FROM coverage_items WHERE id=?", (item["id"],)).fetchone()
    assert row["status"] == "tested_no_issue"


def test_sample_audit_a3_same_priority_engine(db_conn):
    """A3：sample_audit 与 compute_gaps 用同一 priority_score 实时计算（缓存列变化不影响）。"""
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1", criticality=0.5)
    low_tt = _tt(db_conn, eid, "web_cors")  # risk 0.4, baseline → 实时 prio 0.2 < 阈值 0.3
    item = upsert_coverage_item(db_conn, eid, t, low_tt, "baseline", seed_source="auto")
    _claim_and_test(db_conn, eid, item["id"])
    # 污染缓存列（A3：不得影响 sampling 判定——低优先不该被抽样）
    db_conn.execute("UPDATE coverage_items SET priority_score=9.99 WHERE id=?", (item["id"],))
    db_conn.commit()
    monkeypatch_rand = pytest.MonkeyPatch()
    monkeypatch_rand.setattr(coverage_module.random, "random", lambda: 0.0)
    try:
        targets = sample_audit(db_conn, eid)
    finally:
        monkeypatch_rand.undo()
    assert targets == []  # 实时 prio 0.4*0.4=0.16 < min_priority_threshold 0.3，不抽样


# ---------------------------------------------------------------------------
# 10. closure_rule（F11）与 C8 reason_escalation_state
# ---------------------------------------------------------------------------


def test_closure_rule_f11(db_conn):
    eid = _make_engagement(db_conn)
    seed_default_test_types(db_conn, eid)
    t = _make_target(db_conn, eid, "10.0.0.1")
    t_auto = _make_target(db_conn, eid, "10.0.0.9", auto_created=1)
    normal = upsert_coverage_item(db_conn, eid, t, _tt(db_conn, eid, "web_sqli"), "standard", seed_source="auto")
    auto = upsert_coverage_item(db_conn, eid, t_auto, _tt(db_conn, eid, "web_xss"), "standard", seed_source="auto")
    db_conn.commit()
    assert closure_rule(db_conn, eid, normal) is True   # 参与收敛（阻塞）
    assert closure_rule(db_conn, eid, auto) is False    # auto_created 不参与（不阻塞）
    assert closure_rule(db_conn, eid, normal["id"]) is True  # 字符串 id 兼容


def test_reason_escalation_state_c8(db_conn):
    eid = _make_engagement(db_conn)
    assert reason_escalation_state(db_conn, eid) is False
    key = f"reason_escalation:{eid}"
    # 连续 3 次校验失败 → 升级
    db_conn.execute(
        "INSERT INTO scheduler_state (key, value, updated_at) VALUES (?,?,?)",
        (key, json.dumps({"consecutive_failures": 3, "finalize_rejected": 0}), NOW),
    )
    db_conn.commit()
    assert reason_escalation_state(db_conn, eid) is True
    # 未达上限 → 不升级
    db_conn.execute(
        "UPDATE scheduler_state SET value=? WHERE key=?",
        (json.dumps({"consecutive_failures": 2, "finalize_rejected": 1}), key),
    )
    db_conn.commit()
    assert reason_escalation_state(db_conn, eid) is False
    # 连续 3 次 finalize 被拒 → 升级
    db_conn.execute(
        "UPDATE scheduler_state SET value=? WHERE key=?",
        (json.dumps({"consecutive_failures": 0, "finalize_rejected": 3}), key),
    )
    db_conn.commit()
    assert reason_escalation_state(db_conn, eid) is True
    # 显式 escalated → 升级
    db_conn.execute(
        "UPDATE scheduler_state SET value=? WHERE key=?",
        (json.dumps({"escalated": True}), key),
    )
    db_conn.commit()
    assert reason_escalation_state(db_conn, eid) is True


# ---------------------------------------------------------------------------
# 11. 路由冒烟（矩阵 / gaps / waive / result / claim / release / audit / export）
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
    item = upsert_coverage_item(conn, eid, tid, _tt(conn, eid, "web_sqli"), "deep", seed_source="auto")
    conn.commit()
    conn.close()
    return {"db_path": db_path, "eid": eid, "tid": tid, "item_id": item["id"], "token": "secret"}


def test_router_coverage_matrix_and_gaps(http_env):
    c = _app_client(http_env["db_path"])
    H = {"Authorization": f"Bearer {http_env['token']}"}
    r = c.get(f"/engagements/{http_env['eid']}/coverage", headers=H)
    assert r.status_code == 200
    data = r.json()
    assert {"targets", "test_types", "cells", "summary"} <= set(data)
    assert data["summary"]["total"] == 1
    assert data["cells"][0]["priority"] == pytest.approx(0.972)

    r2 = c.get(f"/engagements/{http_env['eid']}/coverage/gaps", headers=H)
    assert r2.status_code == 200
    gaps = r2.json()
    assert isinstance(gaps, list)
    prios = [g["priority"] for g in gaps]
    assert prios == sorted(prios, reverse=True)
    assert gaps[0]["item_id"] == http_env["item_id"]

    # 未授权 → 401
    assert c.get(f"/engagements/{http_env['eid']}/coverage").status_code == 401
    # 不存在 engagement → 404
    assert c.get("/engagements/eng_999/coverage", headers=H).status_code == 404


def test_router_waive_claim_release_writeback_audit(http_env):
    c = _app_client(http_env["db_path"])
    H = {"Authorization": f"Bearer {http_env['token']}"}
    eid, item_id = http_env["eid"], http_env["item_id"]

    # claim → release → claim（B1 路由层）
    r = c.post(f"/engagements/{eid}/coverage/items/{item_id}/claim", headers=H, json={"intent_id": "i001"})
    assert r.status_code == 200 and r.json()["claimed"] is True
    r = c.post(f"/engagements/{eid}/coverage/items/{item_id}/claim", headers=H, json={"intent_id": "i002"})
    assert r.status_code == 200 and r.json()["claimed"] is False  # 互斥
    r = c.post(f"/engagements/{eid}/coverage/items/{item_id}/release", headers=H, json={"intent_id": "i001"})
    assert r.status_code == 200 and r.json()["released"] is True

    # 写回（先认领）
    c.post(f"/engagements/{eid}/coverage/items/{item_id}/claim", headers=H, json={"intent_id": "i001"})
    r = c.post(f"/engagements/{eid}/coverage/result", headers=H, json={
        "item_ids": [item_id], "depth_achieved": "deep", "outcome": "finding_created",
        "fact_id": "f001", "intent_id": "i001", "tested_scope": {"endpoints": ["/inject"]},
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    # 幂等头重发 → 200 且不重复记账
    c.post(f"/engagements/{eid}/coverage/result", headers={**H, "Idempotency-Key": "k-1"}, json={
        "item_ids": [item_id], "depth_achieved": "deep", "outcome": "finding_created",
        "fact_id": "f001", "intent_id": "i001", "tested_scope": {"endpoints": ["/inject"]},
    })
    items = c.get(f"/engagements/{eid}/coverage/items", headers=H).json()
    assert items[0]["status"] == "tested_with_finding"

    # 未认领写回 → 409 COVERAGE_ALREADY_COVERED（预期分支；写回后 current_intent_id 已清空）
    r = c.post(f"/engagements/{eid}/coverage/result", headers=H, json={
        "item_ids": [item_id], "depth_achieved": "deep", "outcome": "no_issue",
        "intent_id": "i999", "tested_scope": {"endpoints": ["/"]},
    })
    assert r.status_code == 409
    assert r.json()["error_code"] == "COVERAGE_ALREADY_COVERED"

    # waive（人工）
    r = c.post(f"/engagements/{eid}/coverage/items/{item_id}/waive", headers=H,
               json={"kind": "out_of_scope", "reason": "已足够", "by": "analyst"})
    assert r.status_code == 200
    assert r.json()["kind"] == "out_of_scope"

    # audit：手动触发 pending → 确认 verdict
    r = c.post(f"/engagements/{eid}/coverage/items/{item_id}/audit", headers=H, json={"auditor": "worker-2"})
    assert r.status_code == 200 and r.json()["verdict"] is None
    r = c.post(f"/engagements/{eid}/coverage/items/{item_id}/audit", headers=H,
               json={"verdict": "coverage_discrepancy", "auditor": "worker-2", "reason": "manual"})
    assert r.status_code == 200 and r.json()["verdict"] == "coverage_discrepancy"
    audits = c.get(f"/engagements/{eid}/coverage/audit", headers=H).json()
    assert audits["total"] >= 2

    # export
    r = c.get(f"/engagements/{eid}/coverage/export", headers=H)
    assert r.status_code == 200
    assert r.json()["count"] >= 1


def test_router_human_seed_and_calibrate(http_env):
    c = _app_client(http_env["db_path"])
    H = {"Authorization": f"Bearer {http_env['token']}"}
    eid, tid = http_env["eid"], http_env["tid"]
    # POST 人工播种（同 target+test_type 已存在 → 幂等返回原行）
    r = c.post(f"/engagements/{eid}/coverage/items", headers=H,
               json={"target_id": tid, "test_type_id": "tt_web_xss", "depth": "deep"})
    assert r.status_code == 200
    new_id = r.json()["id"]
    assert r.json()["seed_source"] == "human"
    # PUT 校准深度
    r = c.put(f"/engagements/{eid}/coverage/items/{new_id}", headers=H, json={"depth_required": "baseline"})
    assert r.status_code == 200
    assert r.json()["depth_required"] == "baseline"
