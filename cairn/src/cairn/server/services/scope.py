"""授权范围子域服务（Agent 20 · skeleton §3 ``services/scope.py``）。

职责：Engagement 状态机 / 授权窗口 / 熔断 / scope guard 判定 / targets CRUD
（v2 §8.9；契约见 ``dev-agents/20-engagement-scope.md`` §2）。

关键语义：
- 状态机 ``planning→active→paused→completed→archived``（DDL CHECK 枚举，A2）；
- 非法转换 → 409 ``ENGAGEMENT_INVALID_STATE``（黄金不变量 7）；
- ``prohibited`` 命中 → 403 ``SCOPE_DENIED``，**禁止 fallback**（v2 §12 规则 1）；
- B5：窗口到期自动 paused，同时清全部 open intent worker + reason 租约
  （调 25 的 ``services/graph.freeze_project_leases(conn, pid)``，import 守卫）；
- D3/§8.9：创建 engagement 时预置默认测试项目录（调 21 的
  ``services/coverage.seed_default_test_types(conn, eid)``，import 守卫）。

跨包依赖（并行期 import 守卫，缺失不阻塞）：
- 21 ``seed_default_test_types(conn, eid) -> None``；
- 25 ``freeze_project_leases(conn, pid) -> None``。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from ..db import next_id
from ..errors import CairnError, ErrorCode
from ..models import EngagementStatus, TargetKind

logger = logging.getLogger("cairn.server.services.scope")

#: DDL CHECK 允许的 target.kind 值
_TARGET_KINDS = frozenset(k.value for k in TargetKind)

#: 状态机合法转换表（v2 §4.1 / human-workflow §1）。
#: ``archived`` 单向不可逆；``completed→active``（复测）需显式 ``retest=true``。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "planning": frozenset({"active"}),
    "active": frozenset({"paused", "completed"}),
    "paused": frozenset({"active", "completed"}),
    "completed": frozenset({"active", "archived"}),
    "archived": frozenset(),
}

#: findings 终态（已结算）：closed / false_positive / accepted 之外均视为未结算
_SETTLED_FINDING_STATUS = frozenset({"closed", "false_positive", "accepted"})
#: coverage_items 未结算状态（删除 target 应用层 gate 的判据）
_UNSETTLED_COVERAGE_STATUS = frozenset({"untested", "in_progress"})


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    """ISO8601 UTC 字符串（``Z`` 后缀，黄金不变量 8）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str | None) -> datetime | None:
    """解析 ISO8601 时间戳（兼容 ``Z`` 与 ``+00:00`` 后缀）；非法返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _host_of(value: str) -> str:
    """抽取资产的主机名/地址部分（小写、去端口/路径）用于范围匹配。"""
    v = value.strip().rstrip("/")
    if "://" in v:
        parsed = urlparse(v)
        return (parsed.hostname or "").lower()
    if ":" in v:  # host:port（无 scheme）
        head, _, tail = v.rpartition(":")
        if tail.isdigit():
            v = head
    return v.lower().rstrip(".")


def _classify(value: str) -> str:
    """按格式推断 target.kind：url / ip / cidr / domain / hostname（v2 §7.4）。"""
    v = value.strip().rstrip("/")
    if "://" in v:
        return "url"
    # 裸 IP 须先于 CIDR 判定（ip_network(..., strict=False) 也接受裸 IP 作 /32）
    try:
        ipaddress.ip_address(v)
        return "ip"
    except ValueError:
        pass
    # CIDR（host/prefix）优先于 host 拆分判定，避免 "10.0.0.0/8" 被误判为 ip
    try:
        ipaddress.ip_network(v, strict=False)
        return "cidr"
    except ValueError:
        pass
    host = v.split("/", 1)[0]
    return _classify_host(host)


def _classify_host(host: str) -> str:
    h = host.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(h)
        return "ip"
    except ValueError:
        pass
    if "/" in h:
        try:
            ipaddress.ip_network(h, strict=False)
            return "cidr"
        except ValueError:
            pass
    if "." in h:
        return "domain"
    return "hostname"


def _cidr_contains(net_str: str, value: str) -> bool:
    """CIDR 目标是否包含 queried value（IP 或 CIDR）。"""
    try:
        net = ipaddress.ip_network(_host_of(net_str), strict=False)
    except ValueError:
        return False
    v = _host_of(value)
    try:
        return ipaddress.ip_address(v) in net
    except ValueError:
        pass
    try:
        return net.supernet_of(ipaddress.ip_network(v, strict=False))
    except ValueError:
        return False


def _matches(value: str, target_value: str, target_kind: str) -> bool:
    """queried ``value`` 是否落在 target（按 kind 的包含/精确语义）。"""
    if target_kind == "cidr":
        return _cidr_contains(target_value, value)
    v_host = _host_of(value)
    t_host = _host_of(target_value)
    if not v_host or not t_host:
        return False
    if target_kind in ("domain", "url"):
        return v_host == t_host or v_host.endswith("." + t_host)
    # hostname / ip：精确匹配
    return v_host == t_host


def _window_valid(start: str | None, end: str | None) -> bool:
    """授权窗口合法：两者皆空（无固定窗口）或都有且 ``start < end``。"""
    s, e = _parse_iso(start), _parse_iso(end)
    if s is None and e is None:
        return True
    if s is None or e is None:
        return False
    return s < e


def _engagement_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "authorized_start_at": row["authorized_start_at"],
        "authorized_end_at": row["authorized_end_at"],
        "scope_policy": json.loads(row["scope_policy"] or "{}"),
        "kill_switch": row["kill_switch"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _target_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "engagement_id": row["engagement_id"],
        "value": row["value"],
        "kind": row["kind"],
        "scope_status": row["scope_status"],
        "criticality": row["criticality"],
        "auto_created": row["auto_created"],
        "note": row["note"],
        "added_by": row["added_by"],
        "added_at": row["added_at"],
    }


# ---------------------------------------------------------------------------
# 跨包调用（21 / 25 import 守卫）
# ---------------------------------------------------------------------------


def _seed_default_test_types(conn: sqlite3.Connection, eid: str) -> None:
    """契约：``services.coverage.seed_default_test_types(conn, eid) -> None``（21）。

    D3/§8.9：创建 engagement 时预置默认测试项目录（enabled=1）。
    21 未就绪时留 TODO，阶段 1 末联调验证播种。
    """
    try:
        from .coverage import seed_default_test_types  # type: ignore[import-not-found]
    except ImportError:
        # TODO(21)：seed_default_test_types 就绪后此处即真调用；当前并行期跳过播种
        logger.warning(
            "services.coverage.seed_default_test_types 未就绪（Agent 21），跳过 test_types 播种: eid=%s",
            eid,
        )
        return
    seed_default_test_types(conn, eid)


def _freeze_engagement_leases(conn: sqlite3.Connection, eid: str) -> None:
    """B5：engagement 置 paused 时冻结其全部 project 的 intent/reason 租约。

    调 25 的 ``services.graph.freeze_project_leases(conn, pid)``；25 未就绪时
    留 TODO，阶段 1 末联调验证。
    """
    try:
        from .graph import freeze_project_leases  # type: ignore[import-not-found]
    except ImportError:
        # TODO(25)：freeze_project_leases 就绪后此处即真调用
        logger.warning(
            "services.graph.freeze_project_leases 未就绪（Agent 25），跳过 B5 租约冻结: eid=%s",
            eid,
        )
        return
    pids = [
        r["id"]
        for r in conn.execute("SELECT id FROM projects WHERE engagement_id=?", (eid,))
    ]
    for pid in pids:
        freeze_project_leases(conn, pid)
    conn.commit()


# ---------------------------------------------------------------------------
# Engagement 读取 / 列表 / 更新 / 删除
# ---------------------------------------------------------------------------


def get_engagement(conn: sqlite3.Connection, eid: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM engagements WHERE id=?", (eid,)).fetchone()
    return _engagement_to_dict(row) if row else None


def _require_engagement(conn: sqlite3.Connection, eid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM engagements WHERE id=?", (eid,)).fetchone()
    if row is None:
        raise CairnError(
            ErrorCode.NOT_FOUND, message="engagement 不存在", detail={"id": eid}
        )
    return row


def list_engagements(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM engagements"
    params: list[Any] = []
    if status is not None:
        if status not in {s.value for s in EngagementStatus}:
            raise CairnError(
                ErrorCode.VALIDATION, message=f"未知 engagement status: {status!r}"
            )
        sql += " WHERE status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC, id LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [_engagement_to_dict(r) for r in conn.execute(sql, params)]


def update_engagement(
    conn: sqlite3.Connection,
    eid: str,
    *,
    title: str | None = None,
    authorized_start_at: str | None = None,
    authorized_end_at: str | None = None,
    scope_policy: dict | None = None,
) -> dict[str, Any]:
    _require_engagement(conn, eid)
    current = get_engagement(conn, eid)  # type: ignore[assignment]
    new_title = title if title is not None else current["title"]
    new_start = authorized_start_at if authorized_start_at is not None else current["authorized_start_at"]
    new_end = authorized_end_at if authorized_end_at is not None else current["authorized_end_at"]
    new_policy = scope_policy if scope_policy is not None else current["scope_policy"]
    if not _window_valid(new_start, new_end):
        raise CairnError(
            ErrorCode.VALIDATION,
            message="授权窗口非法：需 start<end 或两者皆空",
            detail={"start": new_start, "end": new_end},
        )
    conn.execute(
        "UPDATE engagements SET title=?, authorized_start_at=?, authorized_end_at=?, scope_policy=? "
        "WHERE id=?",
        (
            new_title,
            new_start,
            new_end,
            json.dumps(new_policy, ensure_ascii=False),
            eid,
        ),
    )
    conn.commit()
    return get_engagement(conn, eid)  # type: ignore[return-value]


def delete_engagement(conn: sqlite3.Connection, eid: str) -> None:
    """物理删除（级联清理全部子表，DDL §11）。"""
    _require_engagement(conn, eid)
    conn.execute("DELETE FROM engagements WHERE id=?", (eid,))
    conn.commit()


# ---------------------------------------------------------------------------
# Engagement 创建 / 状态机
# ---------------------------------------------------------------------------


def create_engagement(
    conn: sqlite3.Connection,
    *,
    title: str,
    window_start: str | None = None,
    window_end: str | None = None,
    scope_policy: dict | None = None,
    created_by: str = "human",
) -> dict[str, Any]:
    title = (title or "").strip()
    if not title:
        raise CairnError(ErrorCode.VALIDATION, message="title 不能为空")
    if not _window_valid(window_start, window_end):
        raise CairnError(
            ErrorCode.VALIDATION,
            message="授权窗口非法：需 start<end 或两者皆空",
            detail={"start": window_start, "end": window_end},
        )
    eid = next_id(conn, "engagement")
    conn.execute(
        "INSERT INTO engagements (id, title, status, authorized_start_at, authorized_end_at, "
        "scope_policy, kill_switch, created_by, created_at) "
        "VALUES (?, ?, 'planning', ?, ?, ?, 0, ?, ?)",
        (
            eid,
            title,
            window_start,
            window_end,
            json.dumps(scope_policy or {}, ensure_ascii=False),
            created_by,
            _now_utc(),
        ),
    )
    conn.commit()
    # D3/§8.9：预置默认测试项目录（21 契约，import 守卫）。
    # 集成修复（40）：seed 的 INSERT 需在同一事务内 commit，否则请求级连接关闭回滚，
    # test_types 目录永远为空 → bootstrap 播种覆盖项全部跳过（Dispatcher 无法收敛）。
    _seed_default_test_types(conn, eid)
    conn.commit()
    return get_engagement(conn, eid)  # type: ignore[return-value]


def transition_status(
    conn: sqlite3.Connection,
    eid: str,
    new_status: str,
    *,
    retest: bool = False,
) -> dict[str, Any]:
    """状态机流转（v2 §4.1 / human-workflow §1）。非法转换 → 409。"""
    if new_status not in {s.value for s in EngagementStatus}:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知状态: {new_status!r}")
    row = _require_engagement(conn, eid)
    current = row["status"]
    if current == new_status:
        return _engagement_to_dict(row)  # 幂等 no-op
    if current not in _ALLOWED_TRANSITIONS or new_status not in _ALLOWED_TRANSITIONS[current]:
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message=f"状态转换非法: {current} → {new_status}",
            detail={"from": current, "to": new_status},
        )
    if current == "planning" and new_status == "active":
        _check_activate_preconditions(conn, eid, row)
    if current == "completed" and new_status == "active" and not retest:
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message="completed→active（复测）必须显式 retest=true",
            detail={"retest": False},
        )
    completed_at = row["completed_at"]
    if new_status == "completed":
        completed_at = _now_utc()
    elif current == "completed":
        completed_at = None  # 复测回 active 清 completed_at
    conn.execute(
        "UPDATE engagements SET status=?, completed_at=? WHERE id=?",
        (new_status, completed_at, eid),
    )
    conn.commit()
    if new_status == "paused":
        # B5：自动冻结与人工 paused 同语义（清 intent claim + reason lease）
        _freeze_engagement_leases(conn, eid)
    return get_engagement(conn, eid)  # type: ignore[return-value]


def _check_activate_preconditions(
    conn: sqlite3.Connection, eid: str, row: sqlite3.Row
) -> None:
    """planning→active 前置：scope 非空 + 窗口合法 + kill off（human-workflow §1）。"""
    n_authorized = conn.execute(
        "SELECT count(*) FROM targets WHERE engagement_id=? AND scope_status='authorized'",
        (eid,),
    ).fetchone()[0]
    if n_authorized == 0:
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message="激活前必须至少登记一个 authorized target",
            detail={"authorized_targets": 0},
        )
    if not _window_valid(row["authorized_start_at"], row["authorized_end_at"]):
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message="授权窗口非法（需 start<end 或两者皆空）",
            detail={
                "start": row["authorized_start_at"],
                "end": row["authorized_end_at"],
            },
        )
    if row["kill_switch"]:
        raise CairnError(
            ErrorCode.KILL_SWITCH_ON,
            message="kill switch 已开启，无法激活",
            detail={"kill_switch": row["kill_switch"]},
        )


# ---------------------------------------------------------------------------
# 守卫：writable / scope / kill switch
# ---------------------------------------------------------------------------


def check_engagement_writable(conn: sqlite3.Connection, eid: str) -> None:
    """探索写操作前置（v2 §6.3）：engagement 须 active 且窗口内。

    非 active → 409 ``ENGAGEMENT_INVALID_STATE``；窗口外 → 403
    ``OUT_OF_AUTHORIZATION_WINDOW``。
    """
    row = _require_engagement(conn, eid)
    if row["status"] != "active":
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message="engagement 非 active，不可写",
            detail={"status": row["status"]},
        )
    now = datetime.now(timezone.utc)
    start = _parse_iso(row["authorized_start_at"])
    end = _parse_iso(row["authorized_end_at"])
    if end is not None and now > end:
        raise CairnError(
            ErrorCode.OUT_OF_AUTHORIZATION_WINDOW,
            message="授权窗口已结束",
            detail={"authorized_end_at": row["authorized_end_at"]},
        )
    if start is not None and now < start:
        raise CairnError(
            ErrorCode.OUT_OF_AUTHORIZATION_WINDOW,
            message="授权窗口尚未开始",
            detail={"authorized_start_at": row["authorized_start_at"]},
        )


def check_scope_allowed(
    conn: sqlite3.Connection, eid: str, target_value: str
) -> dict[str, Any] | None:
    """scope guard（v2 §12 规则 1，不可跳过）：``target_value`` ∈ authorized 集合。

    - ``prohibited`` 命中 → 403 ``SCOPE_DENIED``，**禁止 fallback**；
    - authorized 精确命中 → 返回既有 target；
    - authorized 包含命中（子域/CIDR 内）→ auto_created=1 建 target（F11/规则 22）后返回；
    - 未命中任何 → 返回 None（歧义，调用方跳过）。
    """
    _require_engagement(conn, eid)
    rows = conn.execute(
        "SELECT * FROM targets WHERE engagement_id=? AND scope_status IN ('authorized','prohibited')",
        (eid,),
    ).fetchall()

    # prohibited 优先（命中即拒，无 fallback）
    for r in rows:
        if r["scope_status"] == "prohibited" and _matches(
            target_value, r["value"], r["kind"]
        ):
            # 审计：scope 拒入留痕（无 task/finding 上下文时仅日志；有上下文由 30/40 记 task_events）
            logger.warning(
                "SCOPE_DENIED eid=%s value=%r prohibited_target=%r", eid, target_value, r["value"]
            )
            raise CairnError(
                ErrorCode.SCOPE_DENIED,
                message=f"目标 {target_value!r} 命中 prohibited 目标 {r['value']!r}",
                detail={
                    "target_value": target_value,
                    "prohibited_target": r["value"],
                    "prohibited_id": r["id"],
                },
            )

    for r in rows:
        if r["scope_status"] == "authorized" and _matches(
            target_value, r["value"], r["kind"]
        ):
            canonical = _host_of(target_value) or target_value.strip()
            exact = conn.execute(
                "SELECT * FROM targets WHERE engagement_id=? AND value=?", (eid, canonical)
            ).fetchone()
            if exact is not None:
                return _target_to_dict(exact)
            # 包含命中且无精确行 → auto_created 建 target（F11/规则 22）
            tid = next_id(conn, "target", engagement_id=eid)
            conn.execute(
                "INSERT INTO targets (id, engagement_id, value, kind, scope_status, criticality, "
                "auto_created, note, added_by, added_at) "
                "VALUES (?, ?, ?, ?, 'authorized', 0.5, 1, 'scope guard auto-created', 'agent', ?)",
                (tid, eid, canonical, _classify(canonical), _now_utc()),
            )
            conn.commit()
            created = conn.execute(
                "SELECT * FROM targets WHERE id=?", (tid,)
            ).fetchone()
            return _target_to_dict(created)
    return None


def check_kill_switch(conn: sqlite3.Connection, eid: str) -> None:
    """熔断（v2 §4.12/§6.3）：全局或项目 kill → 423 ``KILL_SWITCH_ON``。"""
    s = conn.execute(
        "SELECT global_kill_switch FROM settings WHERE rowid=1"
    ).fetchone()
    if s is not None and s["global_kill_switch"]:
        raise CairnError(
            ErrorCode.KILL_SWITCH_ON,
            message="全局 kill switch 已开启",
            detail={"scope": "global"},
        )
    row = _require_engagement(conn, eid)
    if row["kill_switch"]:
        raise CairnError(
            ErrorCode.KILL_SWITCH_ON,
            message="项目 kill switch 已开启",
            detail={"scope": "engagement", "id": eid},
        )


def set_kill_switch(conn: sqlite3.Connection, eid: str, on: bool) -> dict[str, Any]:
    """设置项目熔断开关（C1：触发即 SIGKILL 语义由 40/11 依据本标志落实）。"""
    _require_engagement(conn, eid)
    conn.execute(
        "UPDATE engagements SET kill_switch=? WHERE id=?", (1 if on else 0, eid)
    )
    conn.commit()
    return get_engagement(conn, eid)  # type: ignore[return-value]


def expire_engagements(conn: sqlite3.Connection) -> None:
    """授权窗口到期自动 pause（v2 §4.2.3/§9.1；B5）。惰性执行（读时/每轮调度）。"""
    now = datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT id, authorized_end_at FROM engagements "
        "WHERE status='active' AND authorized_end_at IS NOT NULL"
    ).fetchall()
    for r in rows:
        end = _parse_iso(r["authorized_end_at"])
        if end is not None and end <= now:
            try:
                transition_status(conn, r["id"], "paused")
            except CairnError as exc:  # 单条失败不阻断其余
                logger.warning("expire_engagements: %s 置 paused 失败: %s", r["id"], exc)
    conn.commit()


# ---------------------------------------------------------------------------
# targets CRUD
# ---------------------------------------------------------------------------


def get_target(conn: sqlite3.Connection, eid: str, tid: str) -> dict[str, Any] | None:
    _require_engagement(conn, eid)
    row = conn.execute(
        "SELECT * FROM targets WHERE id=? AND engagement_id=?", (tid, eid)
    ).fetchone()
    return _target_to_dict(row) if row else None


def _require_target(conn: sqlite3.Connection, eid: str, tid: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM targets WHERE id=? AND engagement_id=?", (tid, eid)
    ).fetchone()
    if row is None:
        raise CairnError(
            ErrorCode.NOT_FOUND,
            message="target 不存在",
            detail={"id": tid, "engagement_id": eid},
        )
    return row


def list_targets(
    conn: sqlite3.Connection,
    eid: str,
    *,
    scope_status: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    _require_engagement(conn, eid)
    sql = "SELECT * FROM targets WHERE engagement_id=?"
    params: list[Any] = [eid]
    if scope_status is not None:
        sql += " AND scope_status=?"
        params.append(scope_status)
    sql += " ORDER BY added_at, id LIMIT ? OFFSET ?"
    params += [limit, offset]
    return [_target_to_dict(r) for r in conn.execute(sql, params)]


def create_target(
    conn: sqlite3.Connection,
    eid: str,
    *,
    value: str,
    scope_status: str = "authorized",
    kind: str | None = None,
    criticality: float = 0.5,
    note: str | None = None,
    auto_created: bool = False,
    added_by: str = "human",
) -> dict[str, Any]:
    _require_engagement(conn, eid)
    value = (value or "").strip()
    if not value:
        raise CairnError(ErrorCode.VALIDATION, message="target value 不能为空")
    if scope_status not in ("authorized", "prohibited"):
        raise CairnError(ErrorCode.VALIDATION, message=f"未知 scope_status: {scope_status!r}")
    if kind is None:
        kind = _classify(value)
    if kind not in _TARGET_KINDS:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知 target kind: {kind!r}")
    if not 0 <= criticality <= 1:
        raise CairnError(
            ErrorCode.VALIDATION,
            message="criticality 必须在 [0,1]",
            detail={"criticality": criticality},
        )
    dup = conn.execute(
        "SELECT id, scope_status FROM targets WHERE engagement_id=? AND value=?",
        (eid, value),
    ).fetchone()
    if dup is not None:
        # UNIQUE(engagement_id, value) 冲突：409 + 明细（COVERAGE_DUP 不适用）
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message="target 已存在（同 engagement 同 value）",
            detail={"value": value, "existing_id": dup["id"], "scope_status": dup["scope_status"]},
        )
    tid = next_id(conn, "target", engagement_id=eid)
    conn.execute(
        "INSERT INTO targets (id, engagement_id, value, kind, scope_status, criticality, "
        "auto_created, note, added_by, added_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tid,
            eid,
            value,
            kind,
            scope_status,
            criticality,
            1 if auto_created else 0,
            note,
            added_by,
            _now_utc(),
        ),
    )
    conn.commit()
    return get_target(conn, eid, tid)  # type: ignore[return-value]


def update_target(
    conn: sqlite3.Connection,
    eid: str,
    tid: str,
    *,
    value: str | None = None,
    scope_status: str | None = None,
    kind: str | None = None,
    criticality: float | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    row = _require_target(conn, eid, tid)
    new_value = value.strip() if value is not None else row["value"]
    new_scope = scope_status if scope_status is not None else row["scope_status"]
    new_kind = kind if kind is not None else row["kind"]
    new_crit = criticality if criticality is not None else row["criticality"]
    new_note = note if note is not None else row["note"]
    if new_scope not in ("authorized", "prohibited"):
        raise CairnError(ErrorCode.VALIDATION, message=f"未知 scope_status: {new_scope!r}")
    if new_kind not in _TARGET_KINDS:
        raise CairnError(ErrorCode.VALIDATION, message=f"未知 target kind: {new_kind!r}")
    if not 0 <= new_crit <= 1:
        raise CairnError(ErrorCode.VALIDATION, message="criticality 必须在 [0,1]")
    if new_value != row["value"]:
        dup = conn.execute(
            "SELECT id FROM targets WHERE engagement_id=? AND value=? AND id<>?",
            (eid, new_value, tid),
        ).fetchone()
        if dup is not None:
            raise CairnError(
                ErrorCode.ENGAGEMENT_INVALID_STATE,
                message="target 已存在（同 engagement 同 value）",
                detail={"value": new_value, "existing_id": dup["id"]},
            )
    conn.execute(
        "UPDATE targets SET value=?, kind=?, scope_status=?, criticality=?, note=? WHERE id=?",
        (new_value, new_kind, new_scope, new_crit, new_note, tid),
    )
    conn.commit()
    return get_target(conn, eid, tid)  # type: ignore[return-value]


def _target_references(
    conn: sqlite3.Connection, eid: str, tid: str
) -> dict[str, list[dict[str, Any]]]:
    """删除 gate 判据：未结算的 findings / coverage_items 引用清单（human-workflow §2）。"""
    findings = [
        {"id": r["id"], "status": r["status"]}
        for r in conn.execute(
            "SELECT id, status FROM findings WHERE target_id=? "
            "AND status NOT IN (?, ?, ?)",
            (tid, *_SETTLED_FINDING_STATUS),
        )
    ]
    coverage = [
        {"id": r["id"], "status": r["status"]}
        for r in conn.execute(
            "SELECT id, status FROM coverage_items WHERE target_id=? "
            f"AND status IN ({','.join('?' * len(_UNSETTLED_COVERAGE_STATUS))})",
            (tid, *_UNSETTLED_COVERAGE_STATUS),
        )
    ]
    return {"findings": findings, "coverage_items": coverage}


def delete_target(conn: sqlite3.Connection, eid: str, tid: str) -> None:
    """删除 target（应用层 gate，human-workflow §2）。

    仍被未结算 findings/coverage_items 引用 → 409 + 引用清单；DB 层保持 CASCADE
    （勿改 RESTRICT——会与 DELETE engagement 级联顺序冲突）。
    """
    _require_target(conn, eid, tid)
    refs = _target_references(conn, eid, tid)
    if refs["findings"] or refs["coverage_items"]:
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message="target 仍被未结算引用，需先人工结算（关闭/改挂/豁免）后再删",
            detail=refs,
        )
    conn.execute("DELETE FROM targets WHERE id=?", (tid,))
    conn.commit()
