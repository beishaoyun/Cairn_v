"""漏洞子域服务层（Agent 22 交付）。

权威依据：
- ``docs/database-ddl-draft.md`` §5 / §9.2 / §9.3 / §4.1
- ``docs/capture-verify-progress-spec.md`` §4/§5/§6
- ``docs/backend-module-skeleton.md`` §3（services/findings.py 签名）
- ``docs/architecture-research-report-pentest-v2.md`` §4.9 / §8.10 / §12 规则 4/18/26/28-36
- ``docs/human-workflow-guide.md`` §4/§5

职责：去重 / target 解析（B1 未知资产 auto_created）/ 状态机与审计 / 证据（文件 + 请求响应包 + 命令）
/ verify 落定（三分支 + max_reverify 升级）/ 复测账本（C10/A2）/ closed 门槛 gate（规则 26/31）/ triaged 计数。

约定：
- 所有状态流转写入 ``finding_history``（actor 必填）。
- 证据字节不落 DB：文件证据只存引用路径 + mime/size（落盘在路由层）。
- ``verified`` 由 verify confirmed 自动置（severity 取 ``verified_severity`` 双轨）；
  ``fixed/closed/false_positive/accepted`` 仅人工（actor='human'）。
- 服务函数无状态、每请求短事务；是否 commit 由路由层控制。
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone

from ..db import next_id
from ..errors import CairnError, ErrorCode

# ---------------------------------------------------------------------------
# 跨包调用点（并行期 import 守卫）
#
# - ``check_scope_allowed``   属于 20-engagement-scope（services/scope.py），未交付时占位放行，
#   交接物标注「需 20 交付后收紧」。
# - ``derive_http_from_capture`` 属于 23-capture（services/capture.py），未交付时仅登记与关联，
#   捕获字节派生由 23 提供后自动接线。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 20 未交付时走占位
    from cairn.server.services.scope import check_scope_allowed as _scope_check  # noqa: F401
except Exception:  # noqa: BLE001 —— 并行开发：20 未交付
    _scope_check = None

try:  # pragma: no cover - 23 未交付时走占位
    from cairn.server.services.capture import derive_http_from_capture as _derive_http  # noqa: F401
except Exception:  # noqa: BLE001 —— 并行开发：23 未交付
    _derive_http = None

try:  # pragma: no cover - 23 未交付时走占位
    from cairn.server.services.capture import link_finding_traffic as _capture_link_traffic  # noqa: F401
except Exception:  # noqa: BLE001 —— 并行开发：23 未交付
    _capture_link_traffic = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_FINDING_STATUSES = frozenset(
    {"open", "pending_verify", "pending_false_positive", "verified", "needs_review",
     "fixed", "false_positive", "accepted", "closed"}
)
_SEVERITIES = frozenset({"critical", "high", "medium", "low", "info"})

#: 仅人工可置的终态/状态（v2 §6.2 / 规则 4；Agent 只能建 open + 补证据）
_HUMAN_ONLY = frozenset({"fixed", "closed", "false_positive", "accepted"})

#: 非人工（agent/dispatcher/verify 自动路径）允许的相邻流转（capture spec §5 状态机）。
#: 人工（actor='human'）可在非 closed 源状态下任意流转（human-workflow §4：人工可登记任意态）。
_MACHINE_EDGES = {
    "open": frozenset({"pending_verify"}),
    "pending_verify": frozenset({"verified", "pending_false_positive", "needs_review", "open"}),
    "pending_false_positive": frozenset({"false_positive", "open", "verified"}),
    "verified": frozenset({"fixed", "needs_review", "open", "accepted"}),
    "needs_review": frozenset({"open", "verified", "fixed", "pending_false_positive", "false_positive", "accepted"}),
    "fixed": frozenset({"closed", "open", "verified"}),
    "false_positive": frozenset({"open"}),
    "accepted": frozenset({"closed"}),
    "closed": frozenset(),  # 终态
}

#: triaged() 未分诊口径（open/pending_verify/pending_false_positive/needs_review 计未分诊；verified 已分诊）
_UNTRIAGED = ("open", "pending_verify", "pending_false_positive", "needs_review")

#: 请求/响应体截断上限（规则 21 / DDL 注释：body 超 64KB 截断并在 note 标注）
_HTTP_BODY_CAP = 64 * 1024

_RETEST_KINDS = frozenset({"replay", "verify", "human"})


def _now() -> str:
    """ISO8601 UTC 时间戳（黄金不变量 8）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finding_eid(conn: sqlite3.Connection, fid: str) -> str:
    row = conn.execute("SELECT engagement_id FROM findings WHERE id=?", (fid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    return row["engagement_id"]


def _get_finding(conn: sqlite3.Connection, fid: str) -> dict:
    row = conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    return _row_to_dict(row)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for col in ("references_", "evidence_summary"):
        if d.get(col):
            try:
                d[col] = json.loads(d[col])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


def _write_history(
    conn: sqlite3.Connection, fid: str, from_status: str | None, to_status: str, *,
    note: str | None = None, actor: str,
) -> dict:
    """写一条 finding_history 审计（actor 必填）。"""
    eid = _finding_eid(conn, fid)
    hid = next_id(conn, "finding_history", engagement_id=eid)
    conn.execute(
        "INSERT INTO finding_history (id, finding_id, from_status, to_status, note, actor, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (hid, fid, from_status, to_status, note, actor, _now()),
    )
    return dict(conn.execute("SELECT * FROM finding_history WHERE id=?", (hid,)).fetchone())


# ---------------------------------------------------------------------------
# B3 去重 / 规范化
# ---------------------------------------------------------------------------


def _normalize_title(title: str) -> str:
    """标题规范化：NFKC + casefold + 空白折叠 + 去除尾部句子标点。

    使 ``"SQL Injection."`` == ``"sql injection"``（B3 / 规则 39：同 target 同名命中去重）。
    """
    t = unicodedata.normalize("NFKC", title or "").casefold()
    t = re.sub(r"\s+", " ", t).strip()
    t = t.rstrip(".！!？?。；;")
    return t


def dedup_key(engagement_id: str, target_id: str, title: str) -> str:
    """去重键 ``(engagement_id, target_id, 规范化 title)``（规则 5 / 规则 39 / B3）。

    返回规范键字符串；``(engagement_id, target_id, title)`` 走 DDL 索引
    ``idx_findings_title_hash``。规范化 title 相同的两条会被判为同一漏洞。
    """
    return f"{engagement_id}\x1f{target_id}\x1f{_normalize_title(title)}"


def _find_duplicate(conn: sqlite3.Connection, eid: str, target_id: str, title: str) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT id, title FROM findings WHERE engagement_id=? AND target_id=?", (eid, target_id)
    ).fetchall()
    nt = _normalize_title(title)
    for r in rows:
        if _normalize_title(r["title"]) == nt:
            return r
    return None


# ---------------------------------------------------------------------------
# B1 未知资产 → resolve_target（scope 校验 + auto_created）
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"^(?P<scheme>https?://)?(?P<host>[^/:?#]+)(?P<port>:\d+)?(?P<rest>[/?#].*)?$",
    re.IGNORECASE,
)


def _target_variants(asset: str) -> list[str]:
    """资产规范化变体（规则 39：scheme/默认端口/尾斜杠/大小写归一；host 级 + 全量）。"""
    a = (asset or "").strip()
    if not a:
        return []
    m = _URL_RE.match(a)
    seen: set[str] = set()
    out: list[str] = []
    if m:
        host = m.group("host").casefold().rstrip(".")
        scheme = (m.group("scheme") or "").casefold()
        port = m.group("port") or ""
        if scheme and port in (":80", ":443"):
            port = ""
        for cand in (f"{host}{port}".rstrip("/"), host, a.casefold().rstrip("/").rstrip(".")):
            if cand and cand not in seen:
                seen.add(cand)
                out.append(cand)
    else:
        cand = a.casefold().rstrip("/").rstrip(".")
        out.append(cand)
    return out


def _infer_kind(asset: str) -> str:
    a = (asset or "").strip().casefold()
    if a.startswith(("http://", "https://")):
        return "url"
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}/\d+$", a):
        return "cidr"
    if re.match(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$", a):
        return "ip"
    if ":" in a and not a.startswith("["):
        return "hostname"
    if re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$", a):
        return "domain"
    return "url"


def _find_target(conn: sqlite3.Connection, eid: str, variants: list[str]) -> sqlite3.Row | None:
    """精确/host 级匹配（含端口归并、大小写归一）。子域/CIDR 包含命中交给 20 的
    ``check_scope_allowed``（其内部正确处理 prohibited 优先，规则 1）。"""
    for v in variants:
        row = conn.execute(
            "SELECT * FROM targets WHERE engagement_id=? AND value=?", (eid, v)
        ).fetchone()
        if row is not None:
            return row
    return None


def _find_target_contained(conn: sqlite3.Connection, eid: str, variants: list[str]) -> sqlite3.Row | None:
    """20 未交付时的降级：子域→父域匹配（仅 authorized；不覆盖 prohibited 语义）。"""
    for v in variants:
        if "." not in v:
            continue
        parts = v.split(".")
        for i in range(1, len(parts) - 1):
            parent = ".".join(parts[i:])
            row = conn.execute(
                "SELECT * FROM targets WHERE engagement_id=? AND value=? AND scope_status='authorized' "
                "AND kind IN ('domain','hostname')",
                (eid, parent),
            ).fetchone()
            if row is not None:
                return row
    return None


def resolve_target(conn: sqlite3.Connection, eid: str, asset: str, *, scope=None) -> dict | None:
    """B1：asset 已登记 → 复用；未登记 → scope 校验通过则 auto_created 建 target。

    - 已登记（精确/host/子域→父域）→ 复用既有 target；
    - 未登记且 20 已交付：委托 ``scope.check_scope_allowed`` —— prohibited 命中 → 403 SCOPE_DENIED；
      authorized 包含命中（域/CIDR 内）→ 其内部 auto_created=1 建 target 并返回；未命中任何 → 403（不在范围）；
    - 未登记且 20 未交付（并行占位）：直接以 ``auto_created=1`` 建 target（交接物标注需 20 交付后收紧）。
    """
    asset = (asset or "").strip()
    if not asset:
        return None
    variants = _target_variants(asset)
    if not variants:
        return None
    t = _find_target(conn, eid, variants)
    if t is not None:
        if t["scope_status"] == "prohibited":
            raise CairnError(
                ErrorCode.SCOPE_DENIED,
                message="目标为 prohibited，拒绝登记漏洞（规则 1，禁止 fallback）",
                detail={"asset": asset, "target_id": t["id"]},
            )
        return dict(t)
    if _scope_check is not None:
        # 20 提供：子域/CIDR 包含命中 + prohibited 优先 + auto_created；返回既有/新建 target dict，或 None/403
        result = _scope_check(conn, eid, asset)
        if result is None:
            raise CairnError(
                ErrorCode.SCOPE_DENIED,
                message="资产不在授权范围，拒绝登记漏洞（规则 22）",
                detail={"asset": asset, "engagement_id": eid},
            )
        return result
    # 20 未交付降级：子域→父域匹配 + auto_created（规则 22 语义由 20 交付后接管）
    tc = _find_target_contained(conn, eid, variants)
    if tc is not None:
        return dict(tc)
    value = variants[0]
    tid = next_id(conn, "target", engagement_id=eid)
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, criticality, "
        "auto_created, added_by, added_at) VALUES (?, ?, ?, ?, 'authorized', 0.5, 1, 'agent', ?)",
        (tid, eid, value, _infer_kind(asset), _now()),
    )
    return dict(conn.execute("SELECT * FROM targets WHERE id=?", (tid,)).fetchone())


# ---------------------------------------------------------------------------
# 创建（B3 去重 / 规则 4：agent 只能 open）
# ---------------------------------------------------------------------------


def _infer_evidence_kind(path: str) -> str:
    ext = (path or "").lower().rsplit(".", 1)[-1] if "." in (path or "") else ""
    if ext in {"png", "jpg", "jpeg", "gif", "webp", "bmp"}:
        return "screenshot"
    if "command" in (path or "").lower() or ext in {"log", "cmd"}:
        return "command_log"
    return "file"


def _record_file_evidence(conn: sqlite3.Connection, fid: str, item) -> dict:
    if isinstance(item, str):
        path = item
        kind = _infer_evidence_kind(path)
        mime = size = None
    else:
        path = item.get("path") or ""
        kind = item.get("kind") or _infer_evidence_kind(path)
        mime = item.get("mime")
        size = item.get("size")
    return attach_evidence(conn, fid, kind=kind, path=path, mime=mime, size=size)


def create_finding(
    conn: sqlite3.Connection, eid: str, *, payload: dict, detected_by: str, actor: str = "agent"
) -> dict:
    """登记漏洞（B3 去重 / 规则 4）。

    - actor='agent' 只能建 ``open``（否则 403）；actor='human' 可登记任意态。
    - 去重命中（同 target + 规范化 title）→ ``FINDING_DUP`` 409，detail 携带已有 finding_id
      （客户端处理成「命中已有 → 追加证据」，规则 5）。
    - payload 支持 ``asset`` 或 ``target_id``；未知资产走 :func:`resolve_target`。
    """
    status = (payload.get("status") or "open")
    if status not in _FINDING_STATUSES:
        raise CairnError(ErrorCode.VALIDATION, message="非法 finding 状态", detail={"status": status})
    if actor != "human" and status != "open":
        raise CairnError(
            ErrorCode.SCOPE_DENIED,
            message="agent 只能创建 open 态漏洞（规则 4）",
            detail={"actor": actor, "status": status},
        )
    eng = conn.execute("SELECT id FROM engagements WHERE id=?", (eid,)).fetchone()
    if eng is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"engagement_id": eid})

    # target 解析（B1）
    target_id = (payload.get("target_id") or "").strip()
    if not target_id:
        asset = payload.get("asset") or payload.get("target") or ""
        target = resolve_target(conn, eid, asset, scope=payload.get("scope"))
        if target is None:
            raise CairnError(
                ErrorCode.VALIDATION, message="需提供 target_id 或 asset（或 asset 已授权登记）",
                detail={"asset": asset},
            )
        target_id = target["id"]
    trow = conn.execute("SELECT * FROM targets WHERE id=? AND engagement_id=?", (target_id, eid)).fetchone()
    if trow is None:
        raise CairnError(
            ErrorCode.NOT_FOUND, message="target 不存在或不属于本 engagement",
            detail={"target_id": target_id},
        )

    title = (payload.get("title") or "").strip()
    if not title:
        raise CairnError(ErrorCode.VALIDATION, message="title 必填")
    dup = _find_duplicate(conn, eid, target_id, title)
    if dup is not None:
        raise CairnError(
            ErrorCode.FINDING_DUP,
            message="漏洞去重：同 target + 规范化 title 已存在，命中已有（追加证据而非重复建单）",
            detail={"finding_id": dup["id"], "dedup_key": dedup_key(eid, target_id, title)},
        )

    severity = payload.get("severity")
    if severity not in _SEVERITIES:
        raise CairnError(ErrorCode.VALIDATION, message="severity 非法", detail={"severity": severity})

    fid = next_id(conn, "finding", engagement_id=eid)
    now = _now()
    conn.execute(
        """INSERT INTO findings (
            id, engagement_id, target_id, title, severity, agent_severity, verified_severity,
            verify_status, retest_pass, retest_round, reverify_count, cvss_score, cvss_vector,
            cwe_id, category, status, description, remediation, references_, detected_by,
            source_fact_id, coverage_item_id, evidence_summary, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'none', 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fid, eid, target_id, title, severity, severity, payload.get("verified_severity"),
            payload.get("cvss_score"), payload.get("cvss_vector"), payload.get("cwe_id"),
            payload.get("category"), status, payload.get("description") or "",
            payload.get("remediation"), json.dumps(payload.get("references") or []),
            detected_by, payload.get("source_fact_id"), payload.get("coverage_item_id"),
            payload.get("evidence_summary"), now, now,
        ),
    )

    for item in payload.get("evidence_refs") or []:
        _record_file_evidence(conn, fid, item)
    for he in payload.get("http") or []:
        he_obj = dict(he)
        # C2：agent 上报的 http[] 无论标注 source 为何，均属「语义注释」；captured 真相行由 23 的
        # derive_http_from_capture（经 traffic_ids 触发）派生。这里以 agent_typed 登记注释，
        # 避免与 derive 产生的 ``(fid, traffic_id, source='captured')`` 行撞 dedup（23 derive 的查重）。
        if he_obj.get("source") == "captured" or he_obj.get("traffic_id"):
            he_obj["source"] = "agent_typed"
        add_http_evidence(conn, fid, http_obj=he_obj)
    for ce in payload.get("commands") or []:
        add_command_evidence(conn, fid, command_obj=ce)
    for trid in payload.get("traffic_ids") or []:
        _link_traffic(conn, fid, trid, role="trigger")
    # 规则 19：finding 登记不改覆盖状态——覆盖项由 21 的 coverage writer（outcome=finding_created）置 tested_with_finding。

    _write_history(conn, fid, None, status, note="创建 finding", actor=actor)
    return _get_finding(conn, fid)


# ---------------------------------------------------------------------------
# 状态机（规则 4 / 规则 26 / capture spec §5）
# ---------------------------------------------------------------------------


def transition_finding(
    conn: sqlite3.Connection, fid: str, *, to_status: str, note: str | None = None, actor: str = "human"
) -> dict:
    """状态流转（写入 finding_history，actor 必填）。

    权限 gate：
    - 非人工置 ``fixed/closed/false_positive/accepted`` → 403（规则 4）；
    - 非人工非法相邻流转 → 409（capture spec §5）；
    - ``closed`` 终态不可再流转 → 409；
    - ``to_status == 'closed'`` 未过复测门槛 → 403（规则 26/31，见 :func:`_assert_closed_gate`）。

    副作用：``fixed`` → fixed_at + retest_round+1 + retest_pass 归零；``closed`` → closed_at；
    ``open`` → reverify_count 归零。
    """
    row = conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    from_status = row["status"]
    if to_status not in _FINDING_STATUSES:
        raise CairnError(ErrorCode.VALIDATION, message="非法目标状态", detail={"to_status": to_status})
    if to_status == from_status:
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE, message="状态未变化", detail={"status": from_status}
        )
    if from_status == "closed":
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE, message="closed 是终态，不可流转",
            detail={"from": from_status, "to": to_status},
        )
    if to_status in _HUMAN_ONLY and actor != "human":
        raise CairnError(
            ErrorCode.SCOPE_DENIED,
            message="仅人工可置 fixed/closed/false_positive/accepted（规则 4）",
            detail={"actor": actor, "to": to_status},
        )
    if actor != "human" and to_status not in _MACHINE_EDGES.get(from_status, ()):
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE, message="非法状态流转",
            detail={"from": from_status, "to": to_status, "actor": actor},
        )
    if to_status == "closed":
        _assert_closed_gate(conn, row)

    now = _now()
    sets = ["status=?", "updated_at=?"]
    params: list = [to_status, now]
    if to_status == "fixed":
        sets.append("fixed_at=?")
        sets.append("retest_round=retest_round+1")
        sets.append("retest_pass=0")
        params.append(now)
    if to_status == "closed":
        sets.append("closed_at=?")
        params.append(now)
    if to_status == "open":
        sets.append("reverify_count=0")
    conn.execute(f"UPDATE findings SET {', '.join(sets)} WHERE id=?", (*params, fid))
    _write_history(conn, fid, from_status, to_status, note=note, actor=actor)
    return _get_finding(conn, fid)


# ---------------------------------------------------------------------------
# verify 落定（F1/F6）
# ---------------------------------------------------------------------------


def _max_reverify(conn: sqlite3.Connection, eid: str) -> int:
    """读取 engagement.scope_policy.verify_policy.max_reverify，默认 3（F6）。"""
    eng = conn.execute("SELECT scope_policy FROM engagements WHERE id=?", (eid,)).fetchone()
    default = 3
    if eng is None or not eng["scope_policy"]:
        return default
    try:
        sp = json.loads(eng["scope_policy"])
    except (json.JSONDecodeError, TypeError):
        return default
    try:
        return int((sp.get("verify_policy") or {}).get("max_reverify", default))
    except (TypeError, ValueError):
        return default


def bump_reverify(conn: sqlite3.Connection, fid: str, *, max_reverify: int | None = None) -> bool:
    """F6：reverify_count+1，返回是否已超 ``max_reverify``（True → 升级人工 needs_review）。"""
    row = conn.execute("SELECT reverify_count, engagement_id FROM findings WHERE id=?", (fid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    limit = max_reverify if max_reverify is not None else _max_reverify(conn, row["engagement_id"])
    new = row["reverify_count"] + 1
    conn.execute("UPDATE findings SET reverify_count=?, updated_at=? WHERE id=?", (new, _now(), fid))
    return new > limit


def apply_verify_runs(conn: sqlite3.Connection, fid: str, *, vr: dict) -> dict:
    """F1：verify verdict 三分支落定（写入 verify_runs + 状态机 + history）。

    - ``confirmed`` → ``verified`` + ``verified_severity``（severity 双轨：同时生效）+ verify_status='confirmed'；
    - ``rejected`` → ``pending_false_positive``（非终态，人工确认后才终态）；
    - ``needs_more_evidence`` → :func:`bump_reverify`；超 ``max_reverify`` → ``needs_review``（升级人工，停止自动循环），
      否则回 ``open``（补证 explore 重新入队）。

    verify 只写 verdict 相关字段，不改 finding 其他内容。
    """
    row = conn.execute("SELECT * FROM findings WHERE id=?", (fid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    verdict = vr.get("verdict")
    if verdict not in ("confirmed", "rejected", "needs_more_evidence"):
        raise CairnError(ErrorCode.VALIDATION, message="verdict 非法", detail={"verdict": verdict})
    if verdict == "confirmed" and vr.get("verified_severity") not in _SEVERITIES:
        raise CairnError(
            ErrorCode.VALIDATION, message="confirmed 需要合法 verified_severity",
            detail={"verified_severity": vr.get("verified_severity")},
        )
    eid = row["engagement_id"]
    actor = vr.get("actor") or "verify"
    now = _now()
    vrid = next_id(conn, "verify_run", engagement_id=eid)
    conn.execute(
        """INSERT INTO verify_runs (id, finding_id, task_run_id, stage, independence,
           input_traffic_digest, observations, verdict, verified_severity, reason,
           verified_traffic_ids, suggested_action, created_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            vrid, fid, vr.get("task_run_id"), vr.get("stage") or "comparison",
            vr.get("independence") or "none", vr.get("input_traffic_digest"),
            json.dumps(vr.get("observations") or []), verdict, vr.get("verified_severity"),
            vr.get("reason"), json.dumps(vr.get("verified_traffic_ids") or []),
            vr.get("suggested_action"), now, now,
        ),
    )

    if verdict == "confirmed":
        vs = vr["verified_severity"]
        conn.execute(
            "UPDATE findings SET status='verified', verify_status='confirmed', verified_severity=?, "
            "severity=?, updated_at=? WHERE id=?",
            (vs, vs, now, fid),
        )
        _write_history(conn, fid, row["status"], "verified", note=vr.get("reason"), actor=actor)
    elif verdict == "rejected":
        conn.execute(
            "UPDATE findings SET status='pending_false_positive', verify_status='rejected', updated_at=? WHERE id=?",
            (now, fid),
        )
        _write_history(conn, fid, row["status"], "pending_false_positive", note=vr.get("reason"), actor=actor)
    else:  # needs_more_evidence
        over = bump_reverify(conn, fid)
        if over:
            conn.execute("UPDATE findings SET status='needs_review', updated_at=? WHERE id=?", (now, fid))
            _write_history(conn, fid, row["status"], "needs_review",
                           note=vr.get("reason") or "needs_more_evidence 超 max_reverify，升级人工（F6）", actor=actor)
        else:
            conn.execute("UPDATE findings SET status='open', updated_at=? WHERE id=?", (now, fid))
            _write_history(conn, fid, row["status"], "open",
                           note=vr.get("reason") or "needs_more_evidence ≤ max_reverify，回 open 补证（F6）", actor=actor)
    return _get_finding(conn, fid)


# ---------------------------------------------------------------------------
# 证据（文件 / 请求响应包 / 命令回显）
# ---------------------------------------------------------------------------


def attach_evidence(conn: sqlite3.Connection, fid: str, *, kind: str, path: str, mime=None, size=None) -> dict:
    """登记文件证据引用（字节落盘由路由层负责，DB 只存 path + mime + size）。"""
    kind_ok = {"screenshot", "file", "command_log", "raw"}
    if kind not in kind_ok:
        raise CairnError(ErrorCode.VALIDATION, message="evidence kind 非法", detail={"kind": kind})
    eid = _finding_eid(conn, fid)
    eid_ev = next_id(conn, "evidence", engagement_id=eid)
    conn.execute(
        "INSERT INTO finding_evidence (id, finding_id, kind, path, mime, size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (eid_ev, fid, kind, path, mime, size, _now()),
    )
    return dict(conn.execute("SELECT * FROM finding_evidence WHERE id=?", (eid_ev,)).fetchone())


def add_http_evidence(conn: sqlite3.Connection, fid: str, *, http_obj: dict) -> dict:
    """登记请求/响应包证据（规则 21）。

    ``source='captured'`` 且带 ``traffic_id``：仅登记与关联（内容以捕获字节为准，C2），
    由 23 的 :func:`derive_http_from_capture` 派生（未交付时仅登记，交接物标注）。
    body 超 64KB 截断并在 note 标注（规则 21）。
    """
    eid = _finding_eid(conn, fid)
    source = http_obj.get("source") or "agent_typed"
    if source not in ("captured", "agent_typed"):
        raise CairnError(ErrorCode.VALIDATION, message="http source 非法", detail={"source": source})
    method = (http_obj.get("method") or "").strip()
    url = (http_obj.get("url") or "").strip()
    if not method or not url:
        raise CairnError(ErrorCode.VALIDATION, message="http 证据需要 method/url")
    resp_status = http_obj.get("response_status")
    if resp_status is not None and not (100 <= int(resp_status) <= 599):
        raise CairnError(
            ErrorCode.VALIDATION, message="response_status 需在 100-599", detail={"response_status": resp_status}
        )
    traffic_id = http_obj.get("traffic_id")
    if traffic_id:
        t = conn.execute(
            "SELECT id FROM traffic_entries WHERE id=? AND engagement_id=?", (traffic_id, eid)
        ).fetchone()
        if t is None:
            raise CairnError(
                ErrorCode.NOT_FOUND, message="traffic 不存在或不属于本 engagement", detail={"traffic_id": traffic_id}
            )
    seq = http_obj.get("seq") or 1
    heid = next_id(conn, "http_evidence", engagement_id=eid)
    body = http_obj.get("response_body")
    note = http_obj.get("note")
    if body and len(body) > _HTTP_BODY_CAP:
        body = body[:_HTTP_BODY_CAP]
        note = (note or "") + f" [response_body truncated >{_HTTP_BODY_CAP}B]"
    conn.execute(
        """INSERT INTO finding_http_evidence (id, finding_id, seq, traffic_id, source, method, url,
           request_headers, request_body, response_status, response_headers, response_body, note, captured_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            heid, fid, seq, traffic_id, source, method, url,
            http_obj.get("request_headers"), http_obj.get("request_body"), resp_status,
            http_obj.get("response_headers"), body, note, http_obj.get("captured_at") or _now(),
        ),
    )
    # captured 来源：内容以捕获字节为准（C2）。23 未交付时仅登记关联；交付后自动接线派生。
    if source == "captured" and traffic_id and _derive_http is not None:
        _derive_http(conn, fid, traffic_id)  # 23-capture 提供
    return dict(conn.execute("SELECT * FROM finding_http_evidence WHERE id=?", (heid,)).fetchone())


def add_command_evidence(conn: sqlite3.Connection, fid: str, *, command_obj: dict) -> dict:
    """登记非 HTTP 命令回显证据（finding_command_evidence）。"""
    eid = _finding_eid(conn, fid)
    command = (command_obj.get("command") or "").strip()
    if not command:
        raise CairnError(ErrorCode.VALIDATION, message="command 必填")
    seq = command_obj.get("seq") or 1
    existing = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS m FROM finding_command_evidence WHERE finding_id=?", (fid,)
    ).fetchone()["m"]
    seq = max(int(seq or 1), existing + 1)
    ceid = next_id(conn, "command_evidence", engagement_id=eid)
    conn.execute(
        """INSERT INTO finding_command_evidence (id, finding_id, seq, command, cwd, exit_code,
           stdout, stderr, started_at, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ceid, fid, seq, command, command_obj.get("cwd"), command_obj.get("exit_code"),
            command_obj.get("stdout"), command_obj.get("stderr"),
            command_obj.get("started_at"), command_obj.get("duration_ms"),
        ),
    )
    return dict(conn.execute("SELECT * FROM finding_command_evidence WHERE id=?", (ceid,)).fetchone())


def _link_traffic(conn: sqlite3.Connection, fid: str, traffic_id: str, *, role: str = "trigger",
                  source: str = "captured", actor: str = "human") -> dict:
    """finding ↔ 流量关联（role: trigger/related/verification/replay）。"""
    eid = _finding_eid(conn, fid)
    if role not in ("trigger", "related", "verification", "replay"):
        raise CairnError(ErrorCode.VALIDATION, message="traffic link role 非法", detail={"role": role})
    t = conn.execute("SELECT id FROM traffic_entries WHERE id=? AND engagement_id=?", (traffic_id, eid)).fetchone()
    if t is None:
        raise CairnError(
            ErrorCode.NOT_FOUND, message="traffic 不存在或不属于本 engagement", detail={"traffic_id": traffic_id}
        )
    lid = next_id(conn, "finding_traffic_link", engagement_id=eid)
    conn.execute(
        "INSERT OR IGNORE INTO finding_traffic_links (id, finding_id, traffic_id, role, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (lid, fid, traffic_id, role, source, _now()),
    )
    conn.execute("UPDATE traffic_entries SET finding_linked=1 WHERE id=?", (traffic_id,))
    row = conn.execute(
        "SELECT * FROM finding_traffic_links WHERE finding_id=? AND traffic_id=? AND role=?",
        (fid, traffic_id, role),
    ).fetchone()
    return dict(row) if row is not None else dict(conn.execute("SELECT * FROM finding_traffic_links WHERE id=?", (lid,)).fetchone())


def link_finding_traffic(conn: sqlite3.Connection, fid: str, traffic_ids, *, role: str = "trigger",
                         source: str = "captured", actor: str = "human"):
    """批量关联流量。

    23 交付后委托 ``capture.link_finding_traffic``（返回 link id 列表，幂等）；
    未交付时用本子域最小实现（返回 link dict 列表）。二者签名/返回形状不同——
    路由层只透传 ``items``，不依赖具体元素形状。
    """
    if _capture_link_traffic is not None:
        return _capture_link_traffic(conn, fid, list(traffic_ids), role=role, source=source)
    out = []
    for trid in traffic_ids:
        out.append(_link_traffic(conn, fid, trid, role=role, source=source, actor=actor))
    return out


# ---------------------------------------------------------------------------
# 复测账本（C10 / A2：同轮同类型幂等 + closed 门槛）
# ---------------------------------------------------------------------------


def record_retest_confirmation(conn: sqlite3.Connection, fid: str, *, kind: str, note=None, actor: str = "human") -> dict:
    """写入 ``finding_retest_confirmations``（``UNIQUE(finding_id, retest_round, kind)`` 幂等，
    同轮同类型重复不计）+ 刷新 ``findings.retest_pass``（当前轮账本行数）。

    kind ∈ {replay, verify, human}（replay = 确定性重放 / 命令确定性重放；verify = 复核 confirmed；human = 人工签收）。
    """
    row = conn.execute("SELECT id, engagement_id, retest_round FROM findings WHERE id=?", (fid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    if kind not in _RETEST_KINDS:
        raise CairnError(ErrorCode.VALIDATION, message="复测确认 kind 非法", detail={"kind": kind})
    rid = next_id(conn, "retest_confirmation", engagement_id=row["engagement_id"])
    conn.execute(
        "INSERT OR IGNORE INTO finding_retest_confirmations "
        "(id, finding_id, retest_round, kind, note, actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rid, fid, row["retest_round"], kind, note, actor, _now()),
    )
    _refresh_retest_pass(conn, fid)
    return _get_finding(conn, fid)


def _refresh_retest_pass(conn: sqlite3.Connection, fid: str) -> None:
    row = conn.execute("SELECT retest_round FROM findings WHERE id=?", (fid,)).fetchone()
    if row is None:
        return
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM finding_retest_confirmations WHERE finding_id=? AND retest_round=?",
        (fid, row["retest_round"]),
    ).fetchone()["n"]
    conn.execute("UPDATE findings SET retest_pass=? WHERE id=?", (total, fid))


def retest_pass_count(conn: sqlite3.Connection, fid: str) -> dict:
    """返回当前 retest_round 下复测确认账本明细（含 kind/note/actor/created_at + count）。

    注：skeleton §3 签名标注 ``-> int``，任务契约 F 要求「返回当前轮账本明细」，
    本实现返回 dict（含 count 字段），偏离 skeleton 返回类型，见交接物。
    """
    row = conn.execute("SELECT retest_round, retest_pass FROM findings WHERE id=?", (fid,)).fetchone()
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="finding 不存在", detail={"finding_id": fid})
    rows = conn.execute(
        "SELECT kind, note, actor, created_at FROM finding_retest_confirmations "
        "WHERE finding_id=? AND retest_round=? ORDER BY created_at",
        (fid, row["retest_round"]),
    ).fetchall()
    return {
        "retest_round": row["retest_round"],
        "count": row["retest_pass"],
        "details": [dict(r) for r in rows],
    }


def _is_http_class(conn: sqlite3.Connection, fid: str) -> bool:
    """HTTP 类判定：有请求/响应包证据或 trigger 流量关联。"""
    n = conn.execute("SELECT COUNT(*) AS n FROM finding_http_evidence WHERE finding_id=?", (fid,)).fetchone()["n"]
    if n:
        return True
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM finding_traffic_links WHERE finding_id=? AND role='trigger'", (fid,)
    ).fetchone()["n"]
    return bool(n)


def _assert_closed_gate(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """closed 前置门槛（规则 26 / 31 / human-workflow §5）：

    1. ``retest_pass >= 2``（当前轮确认账本行数）；
    2. 含 ≥2 种不同类型（replay/verify/human 各 ≤1/轮）；
    3. 必须含 ``kind='replay'`` —— HTTP 类确定性重放 / 非 HTTP 类命令确定性重放。

    未过 → 403（SCOPE_DENIED）。
    """
    fid = row["id"]
    rnd = row["retest_round"]
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM finding_retest_confirmations "
        "WHERE finding_id=? AND retest_round=? GROUP BY kind",
        (fid, rnd),
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    total = sum(r["n"] for r in rows)
    if row["retest_pass"] != total:  # 兜底刷新列一致性
        conn.execute("UPDATE findings SET retest_pass=? WHERE id=?", (total, fid))
    missing: list[str] = []
    if total < 2:
        missing.append(f"retest_pass={total} < 2（需 ≥2 次确认）")
    if len(kinds) < 2:
        missing.append(f"确认类型数={len(kinds)} < 2（replay/verify/human 需 ≥2 种不同类型）")
    if "replay" not in kinds:
        missing.append("缺少确定性重放确认（kind=replay；HTTP 类走确定性重放，非 HTTP 类走命令确定性重放）")
    if missing:
        raise CairnError(
            ErrorCode.SCOPE_DENIED,
            message="closed 前置复测门槛未达成（规则 26/31）",
            detail={
                "finding_id": fid,
                "retest_pass": total,
                "kinds": sorted(kinds),
                "http_class": _is_http_class(conn, fid),
                "missing": missing,
            },
        )


# ---------------------------------------------------------------------------
# triaged（21 report_ready 依赖）
# ---------------------------------------------------------------------------


def triaged(conn: sqlite3.Connection, eid: str) -> int:
    """未分诊计数：open/pending_verify/pending_false_positive/needs_review 计未分诊；
    verified 已分诊不计数（fixed/false_positive/accepted/closed 已结算也不计数）。"""
    placeholders = ",".join("?" for _ in _UNTRIAGED)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM findings WHERE engagement_id=? AND status IN ({placeholders})",
        (eid, *_UNTRIAGED),
    ).fetchone()
    return row["n"]
