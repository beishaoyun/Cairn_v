"""覆盖度子域服务层（Agent 21 · 核心差异化模块）。

权威依据：
- ``docs/coverage-engine-implementation-spec.md`` —— 本模块规格（§1 默认测试目录、
  §2 缺口/收敛/互斥/复测/审计伪代码、§3 输出契约、§5 验收）
- ``docs/rule-registry.md``：A1/A3/A5/B1/B4/C9/F3/F11
- ``docs/backend-module-skeleton.md`` §3（服务签名契约）
- ``docs/database-ddl-draft.md`` §3/§4.1（DDL 与 ID 映射）
- ``docs/architecture-research-report-pentest-v2.md`` §4.13/§8.13/§12 规则 13/33/38/41
- ``docs/human-workflow-guide.md`` §3（豁免/不适用/校准）

黄金不变量落地：
- 覆盖度收敛替代完成判定：无 ``complete`` 字段；reason 只输出缺口收敛（不变量 5）。
- ``compute_gaps``/``sample_audit``/热力图共用 ``priority_score()`` **实时计算**（A3），
  ``coverage_items.priority_score`` 仅作展示缓存、不作为排序依据。
- 格子互斥（B1）：``claim_item_for_intent``/``release_item_for_intent`` 语义严格；
  写回校验 ``current_intent_id == intent_id``。
- 写回幂等（C9）：``coverage_records`` 以 ``(item_id, intent_id)`` 应用层去重。
- ``not_applicable`` 只建议不置状态（B4）：item 的 ``status='not_applicable'`` 仅由人工
  建 ``waivers(kind='not_applicable')`` 后置。
- 复测重建（A5）：复用原行 ``retest_round+1`` + 状态重置，不新建（UNIQUE 约束下不冲突）。
- 枚举值一律复用 ``cairn.server.models``（与 DDL CHECK 逐字符一致，不变量 7）。
"""

from __future__ import annotations

import datetime
import json
import logging
import random
import sqlite3

from ..db import next_id, test_type_id
from ..errors import CairnError, ErrorCode
from ..models import (
    AuditVerdict,
    CoverageItemStatus,
    CoverageOutcome,
    SeedSource,
    TestDepth,
    WaiverKind,
)

logger = logging.getLogger("cairn.server.services.coverage")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: deep/standard 相对 baseline 的加成（收敛进 runtime.tuning；coverage spec §2）
DEPTH_BONUS = 0.2

#: D5：asset_criticality 默认来源（coverage spec §2；防止全员默认 0.5 导致排序退化为纯 risk）
DEFAULT_CRITICALITY = {
    "public_domain": 0.7,   # 公网域名/URL
    "public_ip":     0.8,   # 公网 IP
    "private_cidr":  0.6,   # 内网网段
    "private_host":  0.5,   # 内网主机
    "core_service":  0.9,   # 探测出 DB/认证/核心服务后自动上调
}

#: D5：探测出核心服务后 criticality 上调至 ≥0.9（coverage spec §2 infer_criticality）
CORE_SERVICE_KINDS = ("mysql", "postgres", "ldap", "redis", "auth", "ssh")

#: 默认测试项目录模板（coverage spec §1.1；创建 engagement 时预置，enabled=1）。
#: 元组：``(slug, name, category, risk, default_depth)`` —— id 走 ``tt_<slug>`` 幂等键。
DEFAULT_TEST_TYPES: tuple[tuple[str, str, str, float, str], ...] = (
    ("asset_discovery",        "资产发现（子域/端口/服务）",  "recon",   0.7, "standard"),
    ("service_identification", "服务识别（版本/banner）",    "recon",   0.6, "standard"),
    ("tech_fingerprint",       "技术栈指纹",                "recon",   0.5, "baseline"),
    ("osint_gathering",        "OSINT 情报搜集",            "osint",   0.4, "baseline"),
    ("port_scan",              "端口扫描",                  "scan",    0.6, "baseline"),
    ("vuln_scanning",          "漏洞扫描（nuclei 模板集等）", "scan",   0.7, "standard"),
    ("ssl_tls_scan",           "TLS/证书配置检查",          "scan",    0.4, "baseline"),
    ("directory_bruteforce",   "目录/接口爆破",             "scan",    0.6, "standard"),
    ("web_sqli",               "SQL 注入",                  "webapp",  0.9, "deep"),
    ("web_xss",                "跨站脚本（XSS）",           "webapp",  0.8, "standard"),
    ("web_csrf",               "CSRF",                      "webapp",  0.5, "standard"),
    ("web_auth_bypass",        "认证绕过",                  "webapp",  0.8, "deep"),
    ("web_weak_credentials",   "弱口令/默认凭据",           "auth",    0.9, "standard"),
    ("web_session",            "会话管理",                  "webapp",  0.7, "standard"),
    ("web_file_upload",        "文件上传",                  "webapp",  0.8, "standard"),
    ("web_ssti",               "服务端模板注入（SSTI）",    "webapp",  0.8, "deep"),
    ("web_command_injection",  "命令注入",                  "webapp",  0.9, "deep"),
    ("web_path_traversal",     "路径穿越",                  "webapp",  0.8, "standard"),
    ("web_ssrf",               "SSRF",                      "webapp",  0.8, "standard"),
    ("web_cors",               "CORS 配置",                 "webapp",  0.4, "baseline"),
    ("web_open_redirect",      "开放重定向",                "webapp",  0.4, "baseline"),
    ("web_info_disclosure",    "信息泄露/敏感文件",         "config",  0.6, "standard"),
    ("net_service_hardening",  "服务弱配置/加固",           "network", 0.5, "standard"),
    ("net_ssh_brute",          "SSH 弱口令",                "auth",    0.8, "standard"),
    ("net_snmp",               "SNMP 枚举",                 "network", 0.6, "standard"),
    ("net_default_creds",      "默认凭据（设备/中间件）",   "config",  0.8, "baseline"),
    ("cfg_insecure_config",    "不安全配置",                "config",  0.5, "baseline"),
    ("cfg_encryption",         "传输/存储加密缺失",         "config",  0.6, "standard"),
)

#: 收敛策略默认值（coverage spec §2；engagement.scope_policy.coverage / settings 可覆盖）
DEFAULT_COVERAGE_POLICY = {
    "min_priority_threshold": 0.30,   # 低于该优先级的缺口视为低价值
    "target_coverage": 0.95,          # 整体覆盖率目标
    "require_all_findings_triaged": True,  # finalize 前 findings 无未分诊
    "require_depth": "standard",      # 高优先级项必须达到的最小深度
    "auto_created_closure": {         # F11：auto_created 目标项不阻塞收敛
        "max_extra_depth": "baseline",
        "excluded_from_report_ready": True,
    },
    "audit_sampling": {               # F3：覆盖质量抽样复核
        "enabled": True,
        "high_priority_sample_rate": 0.10,
        "discrepancy_trigger": True,
    },
    "reason_escalation": {            # C8：reason 空转升级人工
        "max_consecutive_failures": 3,
        "max_finalize_rejected": 3,
        "escalate_to": "needs_review",
    },
}

#: scheduler_state key 前缀（C8 计数由 Dispatcher 30/40 写入，服务端只读判定）
REASON_ESCALATION_KEY = "reason_escalation"


def utcnow() -> str:
    """ISO8601 UTC 时间戳（带微秒，保证 coverage_records 排序确定性）。"""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ---------------------------------------------------------------------------
# 优先级 / criticality（A3/D5：实时口径，全模块共用同一函数）
# ---------------------------------------------------------------------------


def priority_score(asset_criticality: float, test_type_risk: float, depth: str) -> float:
    """优先级 = 资产重要性 × 测试项风险 × 深度加成（coverage spec §2）。

    ``depth == 'baseline'`` 不加成；standard/deep 加成 ``DEPTH_BONUS=0.2``。
    所有消费方（compute_gaps / sample_audit / 热力图）**必须**调用本函数实时计算，
    不得读 ``coverage_items.priority_score`` 缓存列（A3）。
    """
    bonus = DEPTH_BONUS if depth != TestDepth.baseline.value else 0.0
    return asset_criticality * test_type_risk * (1.0 + bonus)


def infer_criticality(kind: str, service_kind: str | None = None) -> float:
    """D5：按资产类型/暴露面推断 criticality；探测出核心服务自动上调至 ≥0.9。

    bootstrap/recon 播种与 targets 登记时调用；人工可覆盖（D5）。
    """
    base = DEFAULT_CRITICALITY.get(kind, 0.5)
    if service_kind in CORE_SERVICE_KINDS:
        return max(base, 0.9)
    return base


# ---------------------------------------------------------------------------
# 默认测试项目录播种（20 在 create_engagement 时调用；硬依赖）
# ---------------------------------------------------------------------------


def seed_default_test_types(conn: sqlite3.Connection, eid: str) -> None:
    """创建 engagement 时预置默认测试项目录（coverage spec §1.1，enabled=1）。

    幂等：id=``tt_<slug>`` 主键 + ``(engagement_id, name)`` UNIQUE，INSERT OR IGNORE。
    **接口签名固定 ``(conn, eid) -> None``** —— 20 的 ``services.scope`` 已按此契约调用。
    只写目录（test_types），不生成覆盖项（覆盖项由 bootstrap 播种/人工播种产生）。
    """
    for slug, name, category, risk, depth in DEFAULT_TEST_TYPES:
        conn.execute(
            "INSERT OR IGNORE INTO test_types "
            "(id, engagement_id, name, category, risk, default_depth, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (test_type_id(slug), eid, name, category, risk, depth),
        )


# ---------------------------------------------------------------------------
# 缺口清单 / 汇总（A3：实时 priority；B1：exclude_in_progress）
# ---------------------------------------------------------------------------


def compute_gaps(
    conn: sqlite3.Connection,
    eid: str,
    *,
    threshold: float = 0.0,
    exclude_in_progress: bool = False,
    limit: int | None = 50,
) -> list[dict]:
    """确定性缺口清单：untested（+可选的 in_progress），排除已豁免/不适用。

    B1 格子互斥：reason 消费缺口时必须 ``exclude_in_progress=True`` —— in_progress 格已被
    某 explore intent 认领（current_intent_id 非空），不得再为它派第二个 explore。
    limit：reason 消费按 priority 降序取前 N（默认 50），防缺口列表撑爆 prompt。

    A3：priority 始终实时计算（criticality/risk 变更即生效）；缓存列仅作展示。
    """
    status_filter = (
        "ci.status IN ('untested','in_progress')"
        if not exclude_in_progress
        else "ci.status = 'untested' AND ci.current_intent_id IS NULL"
    )
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
        (eid,),
    ).fetchall()
    gaps: list[dict] = []
    for r in rows:
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


def coverage_summary(
    conn: sqlite3.Connection,
    eid: str,
    *,
    exclude_item_ids: set[str] | None = None,
) -> dict:
    """覆盖率汇总（热力图顶栏数据）。``exclude_item_ids``：F11 收敛口径排除集。

    C9：``partial`` 单独计数——部分覆盖格不算充分覆盖（热力图半色），不阻塞收敛但明示。
    """
    exclude_item_ids = exclude_item_ids or set()
    total = covered = untested = in_progress = na = waived = with_finding = partial = 0
    for (item_id, status,) in conn.execute(
        "SELECT id, status FROM coverage_items WHERE engagement_id = ?", (eid,)
    ).fetchall():
        if item_id in exclude_item_ids:
            continue
        total += 1
        if status == CoverageItemStatus.tested_no_issue.value:
            covered += 1
            partial += conn.execute(
                "SELECT COUNT(*) FROM coverage_records WHERE item_id=? AND partial=1", (item_id,)
            ).fetchone()[0]
        elif status == CoverageItemStatus.tested_with_finding.value:
            covered += 1
            with_finding += 1
        elif status == CoverageItemStatus.not_applicable.value:
            covered += 1
            na += 1
        elif status == CoverageItemStatus.waived.value:
            covered += 1
            waived += 1
        elif status == CoverageItemStatus.in_progress.value:
            in_progress += 1
        else:
            untested += 1
    return {
        "total": total, "covered": covered,
        "coverage_ratio": round(covered / total, 4) if total else 1.0,
        "untested": untested, "in_progress": in_progress,
        "not_applicable": na, "waived": waived, "with_finding": with_finding,
        "partial": partial,
    }


def _auto_created_item_ids(conn: sqlite3.Connection, eid: str) -> set[str]:
    """F11：auto_created 目标的覆盖项 id（report-ready 收敛口径排除用）。"""
    return {
        r["id"] for r in conn.execute(
            """
            SELECT ci.id FROM coverage_items ci
            JOIN targets t ON ci.target_id = t.id
            WHERE ci.engagement_id = ? AND t.auto_created = 1
            """,
            (eid,),
        ).fetchall()
    }


# ---------------------------------------------------------------------------
# 收敛判定（report-ready）—— finalize 的 gate
# ---------------------------------------------------------------------------


def _untriaged_count(conn: sqlite3.Connection, eid: str) -> int:
    """未分诊 findings 计数（verified 已分诊，不阻塞 finalize）。

    **跨包只读依赖**：读 22 的 ``services.findings.triaged(conn, eid)``；
    import 守卫 —— 22 未提供/导入失败时回落本地等价查询（语义一致，不阻塞）。
    """
    try:
        from .findings import triaged  # type: ignore[import-not-found]
        return triaged(conn, eid)
    except Exception:  # noqa: BLE001 —— 并行期 22 未就绪时的兜底
        logger.warning("services.findings.triaged 未就绪，回落本地等价查询（report_ready）")
        return conn.execute(
            "SELECT COUNT(*) FROM findings WHERE engagement_id = ? AND status IN "
            "('open','pending_verify','pending_false_positive','needs_review')",
            (eid,),
        ).fetchone()[0]


def report_ready(
    conn: sqlite3.Connection,
    eid: str,
    policy: dict | None = None,
) -> tuple[bool, dict]:
    """判定是否达到 report-ready（finalize 的 gate，覆盖率收敛核心）。

    达标 = 无高优先缺口 + 深度达标 + 覆盖率 ≥ 目标 + findings 全分诊（策略可配置）。

    F11 闭环：auto_created 目标的覆盖项**不参与** report-ready 的深度校验与覆盖率分母，
    避免「发现新资产 → 新增未覆盖项 → 覆盖率下降 → 永远无法收敛」的无限回退。
    它们仍显示在热力图并照常测试，只是不阻塞 finalize。
    """
    policy = policy or DEFAULT_COVERAGE_POLICY
    closure = policy.get("auto_created_closure", {})
    excluded = _auto_created_item_ids(conn, eid) if closure.get("excluded_from_report_ready") else set()

    uncovered_high = [
        g for g in compute_gaps(conn, eid, threshold=policy.get("min_priority_threshold", 0.30))
        if g["item_id"] not in excluded
    ]
    DEPTH_RANK = {"baseline": 0, "standard": 1, "deep": 2}
    min_depth = DEPTH_RANK.get(policy.get("require_depth", "standard"), 1)
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
        (eid,),
    ).fetchall():
        if item_id in excluded:
            continue  # F11：auto_created 目标项不参与深度达标校验
        # C1：按深度等级比较（baseline=0/standard=1/deep=2），禁止字符串字典序比较
        if last_depth and DEPTH_RANK.get(last_depth, 0) < min_depth:
            depth_shortfall += 1

    summary = coverage_summary(conn, eid, exclude_item_ids=excluded)
    untriaged = _untriaged_count(conn, eid)

    ok = (
        not uncovered_high
        and depth_shortfall == 0
        and summary["coverage_ratio"] >= policy.get("target_coverage", 0.95)
        and (not policy.get("require_all_findings_triaged", True) or untriaged == 0)
    )
    return ok, {
        "uncovered_high": uncovered_high,
        "depth_shortfall": depth_shortfall,
        "summary": summary,
        "untriaged_findings": untriaged,
        "policy": policy,
    }


# ---------------------------------------------------------------------------
# 覆盖项（人工播种 / 复测兜底）
# ---------------------------------------------------------------------------


def upsert_coverage_item(
    conn: sqlite3.Connection,
    eid: str,
    target_id: str,
    test_type_id_: str,
    depth: str,
    *,
    seed_source: str = SeedSource.auto.value,
) -> sqlite3.Row:
    """人工播种/异常兜底：同 ``(target, test_type)`` 已存在则返回原行，否则新建。

    ``priority_score`` 列仅作展示缓存：按 target.criticality × test_type.risk × 深度加成
    实时计算后写入（A3 缓存列，不作为排序依据）。返回 coverage_items 行。
    """
    existing = conn.execute(
        "SELECT * FROM coverage_items WHERE engagement_id=? AND target_id=? AND test_type_id=?",
        (eid, target_id, test_type_id_),
    ).fetchone()
    if existing is not None:
        return existing
    target = conn.execute(
        "SELECT criticality FROM targets WHERE id=? AND engagement_id=?", (target_id, eid)
    ).fetchone()
    if target is None:
        raise CairnError(ErrorCode.NOT_FOUND, message=f"目标不存在: {target_id}")
    tt = conn.execute(
        "SELECT risk, default_depth FROM test_types WHERE id=? AND engagement_id=?",
        (test_type_id_, eid),
    ).fetchone()
    if tt is None:
        raise CairnError(ErrorCode.NOT_FOUND, message=f"测试项不存在: {test_type_id_}")
    valid_depths = {d.value for d in TestDepth}
    depth = depth if depth in valid_depths else tt["default_depth"]
    prio = priority_score(target["criticality"], tt["risk"], depth)
    item_id = next_id(conn, "coverage_item", engagement_id=eid)
    conn.execute(
        "INSERT INTO coverage_items "
        "(id, engagement_id, target_id, test_type_id, depth_required, priority_score, "
        " status, seed_source, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (item_id, eid, target_id, test_type_id_, depth, round(prio, 3),
         CoverageItemStatus.untested.value, seed_source, utcnow()),
    )
    return conn.execute("SELECT * FROM coverage_items WHERE id=?", (item_id,)).fetchone()


# ---------------------------------------------------------------------------
# 格子互斥（B1）：claim / release / 写回校验
# ---------------------------------------------------------------------------


def claim_item_for_intent(conn: sqlite3.Connection, item_id: str, intent_id: str) -> bool:
    """explore 派发前调用：item 未覆盖（untested 且 current_intent_id IS NULL）→
    置 ``in_progress`` + ``current_intent_id=intent_id``，返回 True；否则返回 False。

    B1 格子互斥：并发下两 intent 认领同一格时第二个返回 False（不派发）。
    """
    cur = conn.execute(
        "UPDATE coverage_items SET status='in_progress', current_intent_id=? "
        "WHERE id=? AND status='untested' AND current_intent_id IS NULL",
        (intent_id, item_id),
    )
    return cur.rowcount > 0


def release_item_for_intent(conn: sqlite3.Connection, item_id: str, intent_id: str) -> None:
    """任务失败/取消/超时：仅当 ``current_intent_id == intent_id`` 才回退 untested。

    B1 语义：NULL 不放行——未认领格（current_intent_id IS NULL）调用 release 是 no-op，
    SQL 比较 ``NULL = ?`` 为 NULL 自然不命中；防止误清他人认领。40 的重启 reconcile 依赖此语义。
    """
    conn.execute(
        "UPDATE coverage_items SET status='untested', current_intent_id=NULL "
        "WHERE id=? AND current_intent_id=?",
        (item_id, intent_id),
    )


def write_coverage_result(
    conn: sqlite3.Connection,
    eid: str,
    *,
    item_ids: list[str],
    depth_achieved: str,
    outcome: str,
    fact_id: str | None = None,
    intent_id: str,
    evidence_refs: list[str] | None = None,
    tested_scope: object | None = None,
    partial: bool = False,
) -> None:
    """explore 写回（C9/B1）：校验格子归属 + 本次 intent 认领 → 写 coverage_records +
    更新 item 状态 + 清空 current_intent_id。同事务（由路由层 commit）。

    - 校验：``covered_items ⊆ 本 engagement``（否则 COVERAGE_NOT_APPLICABLE）；
      且 ``current_intent_id == intent_id``（否则 COVERAGE_ALREADY_COVERED，NULL 不放行）。
    - 幂等（C9）：``(item_id, intent_id)`` 应用层去重——coverage_records 已存在则跳过，
      防「服务端已成功、Dispatcher 超时重发」重复记账。
    - ``outcome=not_applicable`` **只建议**：写 coverage_records，不置 item
      ``status='not_applicable'``（B4；item 保持 untested，reason 仍可见为低优先缺口）。
    - C9 充分性：``outcome=no_issue`` 必须声明 ``tested_scope``（覆盖不明确被要求补注）；
      ``partial=True`` 记 coverage_records.partial=1（热力图半色，不算充分覆盖）。
    """
    if not item_ids:
        raise CairnError(ErrorCode.VALIDATION, message="covered_items 不能为空", detail={"item_ids": item_ids})
    valid_depths = {d.value for d in TestDepth}
    if depth_achieved not in valid_depths:
        raise CairnError(ErrorCode.VALIDATION, message=f"非法 depth_achieved: {depth_achieved!r}")
    valid_outcomes = {o.value for o in CoverageOutcome}
    if outcome not in valid_outcomes:
        raise CairnError(ErrorCode.VALIDATION, message=f"非法 outcome: {outcome!r}")

    # C9：outcome=no_issue 且未声明 tested_scope（覆盖不明确）→ 要求补注
    if outcome == CoverageOutcome.no_issue.value and not tested_scope:
        raise CairnError(
            ErrorCode.VALIDATION,
            message="outcome=no_issue 必须声明 tested_scope（C9：覆盖不明确被要求补注）",
            detail={"item_ids": item_ids},
        )

    effective_partial = bool(partial) or (
        isinstance(tested_scope, dict) and bool(tested_scope.get("partial"))
    )
    now = utcnow()
    for iid in item_ids:
        item = conn.execute("SELECT * FROM coverage_items WHERE id=?", (iid,)).fetchone()
        if item is None or item["engagement_id"] != eid:
            raise CairnError(
                ErrorCode.COVERAGE_NOT_APPLICABLE,
                message=f"intent 引用了非本 engagement 覆盖项: {iid}",
                detail={"item_id": iid},
            )
        # C9 幂等：同一 (item_id, intent_id) 已写过 coverage_records → 跳过（不重复记账）。
        # 必须在认领校验**之前**——「服务端已成功、Dispatcher 超时重发」时 current_intent_id
        # 已被清空，重发应为 no-op 成功而非 COVERAGE_ALREADY_COVERED。
        dup = conn.execute(
            "SELECT 1 FROM coverage_records WHERE item_id=? AND intent_id=? LIMIT 1",
            (iid, intent_id),
        ).fetchone()
        if dup is not None:
            continue
        if item["current_intent_id"] != intent_id:
            # B1：仅本次 intent 认领者可写回；NULL 不放行——未认领格必须由调度器先 claim。
            # 并发下他格被认领是预期分支：写回作废 + release，下轮 reason 重排。
            raise CairnError(
                ErrorCode.COVERAGE_ALREADY_COVERED,
                message=f"覆盖项已被测/他人认领: {iid}",
                detail={"item_id": iid, "claimed_by": item["current_intent_id"]},
            )

        conn.execute(
            "INSERT INTO coverage_records "
            "(id, item_id, engagement_id, depth_achieved, outcome, source_fact_id, intent_id, "
            " evidence_refs, tested_scope, partial, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                next_id(conn, "coverage_record", engagement_id=eid),
                iid, eid, depth_achieved, outcome, fact_id, intent_id,
                json.dumps(evidence_refs, ensure_ascii=False) if evidence_refs else None,
                json.dumps(tested_scope, ensure_ascii=False) if tested_scope is not None else None,
                1 if effective_partial else 0,
                now,
            ),
        )
        if outcome == CoverageOutcome.not_applicable.value:
            # B4：只建议不置状态——回退 claim（status 恢复 untested + 清认领），
            # 不置 status='not_applicable'；reason 仍可见为低优先缺口。
            conn.execute(
                "UPDATE coverage_items SET status='untested', last_result=?, tested_at=?, "
                "current_intent_id=NULL WHERE id=?",
                (outcome, now, iid),
            )
        else:
            new_status = (
                CoverageItemStatus.tested_with_finding.value
                if outcome == CoverageOutcome.finding_created.value
                else CoverageItemStatus.tested_no_issue.value
            )
            conn.execute(
                "UPDATE coverage_items SET status=?, last_result=?, tested_at=?, current_intent_id=NULL "
                "WHERE id=?",
                (new_status, outcome, now, iid),
            )


# ---------------------------------------------------------------------------
# 豁免（B4：not_applicable 必须建 waiver 才置 item 状态；仅人工）
# ---------------------------------------------------------------------------


def waive_item(
    conn: sqlite3.Connection,
    eid: str,
    item_id: str,
    *,
    kind: str,
    reason: str,
    by: str,
) -> sqlite3.Row:
    """人工豁免（kind ∈ {not_applicable, out_of_scope, risk_accepted}，reason 必填）。

    B4：``kind=not_applicable`` 必须建 ``waivers(kind='not_applicable')`` 才置
    ``status='not_applicable'``；其余 kind 置 ``status='waived'``。写 waivers + 更新 item。
    「仅人工」由业务规则 + Agent 不持 token（C5）落实（服务端单 token 无法区分调用方）。
    """
    valid_kinds = {k.value for k in WaiverKind}
    if kind not in valid_kinds:
        raise CairnError(ErrorCode.VALIDATION, message=f"非法豁免类型: {kind!r}")
    if not reason or not reason.strip():
        raise CairnError(ErrorCode.VALIDATION, message="豁免必须填写理由（reason）", detail={"item_id": item_id})
    item = conn.execute(
        "SELECT * FROM coverage_items WHERE id=? AND engagement_id=?", (item_id, eid)
    ).fetchone()
    if item is None:
        raise CairnError(ErrorCode.NOT_FOUND, message=f"覆盖项不存在: {item_id}")

    waiver_id = next_id(conn, "waiver", engagement_id=eid)
    conn.execute(
        "INSERT INTO waivers (id, item_id, engagement_id, kind, reason, created_by, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (waiver_id, item_id, eid, kind, reason, by, utcnow()),
    )
    new_status = (
        CoverageItemStatus.not_applicable.value
        if kind == WaiverKind.not_applicable.value
        else CoverageItemStatus.waived.value
    )
    conn.execute(
        "UPDATE coverage_items SET status=?, last_result=?, current_intent_id=NULL WHERE id=?",
        (new_status, kind, item_id),
    )
    return conn.execute("SELECT * FROM waivers WHERE id=?", (waiver_id,)).fetchone()


# ---------------------------------------------------------------------------
# 复测重建（A5：复用原行 retest_round+1，不新建）
# ---------------------------------------------------------------------------


def rebuild_for_retest(
    conn: sqlite3.Connection,
    eid: str,
    target_id: str,
    test_type_id_: str,
    *,
    depth: str = "retest",
) -> sqlite3.Row:
    """A5：finding ``fixed`` 触发——找到 ``(target, test_type)`` 原覆盖项 →
    ``retest_round+1`` + 状态重置 untested；coverage_records 历史保留（复测前后对比用）。

    UNIQUE(engagement_id, target_id, test_type_id) 约束下复用原行，不新建；
    格子不存在时（异常）才 ``upsert_coverage_item`` 新建。返回覆盖项行。
    """
    item = conn.execute(
        "SELECT * FROM coverage_items WHERE engagement_id=? AND target_id=? AND test_type_id=?",
        (eid, target_id, test_type_id_),
    ).fetchone()
    if item is None:
        return upsert_coverage_item(
            conn, eid, target_id, test_type_id_, "standard", seed_source=SeedSource.human.value
        )
    new_depth = depth if depth != "retest" else item["depth_required"]
    conn.execute(
        "UPDATE coverage_items SET status='untested', last_result=NULL, tested_at=NULL, "
        "tested_by=NULL, current_intent_id=NULL, retest_round=retest_round+1, "
        "depth_required=?, created_at=? WHERE id=?",
        (new_depth, utcnow(), item["id"]),
    )
    return conn.execute("SELECT * FROM coverage_items WHERE id=?", (item["id"],)).fetchone()


# ---------------------------------------------------------------------------
# 抽样复核（F3）：sample_audit 选样 → apply_audit_verdict 落定
# ---------------------------------------------------------------------------


def sample_audit(
    conn: sqlite3.Connection,
    eid: str,
    policy: dict | None = None,
) -> list[dict]:
    """F3：选出需要独立复核的覆盖项（**不落库**；落库在 :func:`apply_audit_verdict`）。

    两类触发：
    - ``sampling``：高优先已测格按 ``high_priority_sample_rate`` 抽样（抽查自报已测是否真测）；
    - ``discrepancy``：声称 ``finding_created`` 但该覆盖项无任何 finding → 强制复核。

    A3：与 compute_gaps 同口径——实时 ``priority_score()`` 计算，不读缓存列。
    返回 ``[{"item_id": str, "reason": "sampling"|"discrepancy"}, ...]``。
    """
    policy = policy or DEFAULT_COVERAGE_POLICY
    audit = policy.get("audit_sampling", {})
    targets: list[dict] = []
    if not audit.get("enabled", True):
        return targets
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
        (eid,),
    ).fetchall()
    rate = float(audit.get("high_priority_sample_rate", 0.10))
    threshold = float(policy.get("min_priority_threshold", 0.30))
    for r in rows:
        # A3：同一查询路径，实时计算 priority，杜绝两套口径（验收点 1/10）
        prio = priority_score(r["criticality"], r["risk"], r["depth_required"])
        if prio >= threshold:
            if random.random() < rate:
                targets.append({"item_id": r["id"], "reason": "sampling"})
        # discrepancy：声称有 finding 但覆盖项下无 finding → 强制审计
        if audit.get("discrepancy_trigger") and r["outcome"] == CoverageOutcome.finding_created.value:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM findings f WHERE f.engagement_id=? AND f.coverage_item_id=?",
                (eid, r["id"]),
            ).fetchone()[0]
            if cnt == 0:
                targets.append({"item_id": r["id"], "reason": "discrepancy"})
    return targets


def apply_audit_verdict(
    conn: sqlite3.Connection,
    eid: str,
    *,
    item_id: str,
    verdict: str,
    auditor: str,
    reason: str = "sampling",
    depth_reached: str | None = None,
    note: str | None = None,
) -> sqlite3.Row:
    """F3：审计落定。``audit_runs`` 留痕；``coverage_discrepancy`` → 覆盖项回退 untested +
    缺口重排（reason 会重新把该格排进缺口）。返回 audit_runs 行。

    ``reason`` 由调用方传入（sampling/discrepancy/manual），不得硬编码——
    discrepancy/manual 触发路径会失真（coverage spec §2）。
    """
    valid_verdicts = {v.value for v in AuditVerdict}
    if verdict not in valid_verdicts:
        raise CairnError(ErrorCode.VALIDATION, message=f"非法审计结论: {verdict!r}")
    item = conn.execute(
        "SELECT * FROM coverage_items WHERE id=? AND engagement_id=?", (item_id, eid)
    ).fetchone()
    if item is None:
        raise CairnError(ErrorCode.NOT_FOUND, message=f"覆盖项不存在: {item_id}")

    audit_id = next_id(conn, "audit_run", engagement_id=eid)
    now = utcnow()
    conn.execute(
        "INSERT INTO audit_runs "
        "(id, engagement_id, coverage_item_id, reason, auditor, verdict, depth_reached, note, "
        " created_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (audit_id, eid, item_id, reason, auditor, verdict, depth_reached, note, now, now),
    )
    if verdict == AuditVerdict.coverage_discrepancy.value:
        conn.execute(
            "UPDATE coverage_items SET status='untested', last_result='audit_discrepancy', "
            "tested_at=NULL, current_intent_id=NULL WHERE id=?",
            (item_id,),
        )
    return conn.execute("SELECT * FROM audit_runs WHERE id=?", (audit_id,)).fetchone()


# ---------------------------------------------------------------------------
# F11 / C8：收敛口径辅助 + reason 空转升级
# ---------------------------------------------------------------------------


def closure_rule(conn: sqlite3.Connection, eid: str, item) -> bool:
    """F11：该覆盖项是否参与 report-ready 收敛口径（True=参与、阻塞 finalize）。

    - 返回 ``True``：该项**不豁免**，参与 report-ready 校验（高优先缺口/深度不达标会阻塞）；
    - 返回 ``False``：该项来自 auto_created 目标，不进收敛口径（不阻塞，F11）。
    接受 coverage_items 行或 item_id 字符串。
    """
    if isinstance(item, str):
        row = conn.execute("SELECT * FROM coverage_items WHERE id=?", (item,)).fetchone()
        if row is None or row["engagement_id"] != eid:
            return False
        item = row
    auto_ids = _auto_created_item_ids(conn, eid)
    return item["id"] not in auto_ids


def reason_escalation_state(
    conn: sqlite3.Connection,
    eid: str,
    policy: dict | None = None,
) -> bool:
    """C8：reason 是否已升级 ``needs_review``（停止自动重试，仅人工恢复）。

    计数落 ``scheduler_state``（key = ``'reason_escalation:{eid}'``，JSON）：
    ``{"consecutive_failures": N, "finalize_rejected": N, "escalated": bool}``。
    **Dispatcher（30/40）写入**；本函数只读判定。连续校验失败 / finalize 建议被拒超限
    （``reason_escalation.max_consecutive_failures`` / ``max_finalize_rejected``）→ 升级。
    """
    policy = policy or DEFAULT_COVERAGE_POLICY
    esc = policy.get("reason_escalation", {})
    row = conn.execute(
        "SELECT value FROM scheduler_state WHERE key=?", (f"{REASON_ESCALATION_KEY}:{eid}",)
    ).fetchone()
    if row is None:
        return False
    data = json.loads(row["value"] or "{}")
    if data.get("escalated"):
        return True
    max_fail = int(esc.get("max_consecutive_failures", 3))
    max_rej = int(esc.get("max_finalize_rejected", 3))
    return (
        int(data.get("consecutive_failures", 0)) >= max_fail
        or int(data.get("finalize_rejected", 0)) >= max_rej
    )
