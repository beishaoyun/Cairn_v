"""探索图子域（Agent 25）· 权威规格 ``docs/exploration-graph-spec.md``。

从 0 重建 v2 探索图协议（无 v1 代码可迁移）：

- **Fact 只增不改**：无更新/删除路径；重复 description 幂等跳过；``facts.created_at``
  供 D3 时间线（24 只读）。
- **Intent 超边**：from = intent_sources 引用的 fact 集，to = to_fact_id（可空）；
  ``creator`` 不可变；``worker`` 状态机 NULL ⇄ worker ⇄ 释放/conclude。
- **Hint 图外输入**：active/stopped 皆可写（最宽松）。
- **双租约**：intent 级（worker 列）+ 项目级 reason（reason_* 列）。
- **ID**：project 作用域 ``proj_###`` 走全局 ``counters``（name='project'）；
  图内 ``f###/i###/h###`` 走 ``scoped_counters``（各自独立 %03d），禁裸自增。

不变量对齐：黄金不变量 7（枚举与 DDL CHECK 逐字符一致）、8（时间戳 ISO8601 UTC）。
错误码按 spec §2.4：VALIDATION(400)/PROJECT_INACTIVE(403)/LEASE_CONFLICT(409)/
NOT_FOUND(404)/ENGAGEMENT_INVALID_STATE(409)。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

import yaml

from ..errors import CairnError, ErrorCode

logger = logging.getLogger("cairn.server.services.graph")

#: scoped_counters kind → ID 前缀（spec §1：f###/i###/h### 各自独立 %03d）
SCOPED_PREFIX = {"fact": "f", "intent": "i", "hint": "h"}

#: project 全局计数器 name（``proj_###`` 走全局 counters，非 engagement_counters）
_PROJECT_COUNTER = "project"

_ACTIVE = "active"
_STOPPED = "stopped"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def utcnow() -> str:
    """ISO8601 UTC 时间戳（带微秒，保证 facts.created_at 排序确定性，D3 时间线源）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(ts: str | None) -> datetime | None:
    """解析 ISO8601 UTC（兼容 ``Z`` 与 ``+00:00``）。"""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover —— 历史脏数据兜底
        return None


def _settings_timeout(conn: sqlite3.Connection, col: str) -> int:
    row = conn.execute(
        "SELECT intent_timeout, reason_timeout FROM settings WHERE rowid=1"
    ).fetchone()
    if row is None:
        return 15
    return int(row[col] or 15)


# ---------------------------------------------------------------------------
# ID 生成（spec §1 / DDL §4.1：proj_### 全局 counters；f/i/h 走 scoped_counters）
# ---------------------------------------------------------------------------


def next_project_id(conn: sqlite3.Connection) -> str:
    """项目全局 ID：``proj_###``（counters 表 name='project'，与 engagement 独立）。"""
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES (?, 0)", (_PROJECT_COUNTER,))
    row = conn.execute(
        "UPDATE counters SET value = value + 1 WHERE name = ? RETURNING value",
        (_PROJECT_COUNTER,),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError("counters.project 初始化失败")
    return f"proj_{row['value']:03d}"


def next_scoped_id(conn: sqlite3.Connection, pid: str, kind: str) -> str:
    """图作用域 ID：``f###/i###/h###``（scoped_counters，各自独立 %03d）。

    ``kind`` ∈ {fact, intent, hint}。禁止裸自增。
    """
    prefix = SCOPED_PREFIX.get(kind)
    if prefix is None:
        raise ValueError(f"未知 scoped_counters kind: {kind!r}（应为 fact/intent/hint）")
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (pid, kind),
    )
    row = conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ? RETURNING value",
        (pid, kind),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError(f"scoped_counters 初始化失败: {pid}/{kind}")
    return f"{prefix}{row['value']:03d}"


# ---------------------------------------------------------------------------
# 内部行助手
# ---------------------------------------------------------------------------


def _project_row(conn: sqlite3.Connection, pid: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
    return dict(row) if row else None


def _require_project(conn: sqlite3.Connection, pid: str) -> dict[str, Any]:
    row = _project_row(conn, pid)
    if row is None:
        raise CairnError(ErrorCode.NOT_FOUND, message="project 不存在", detail={"project_id": pid})
    return row


def _require_active(conn: sqlite3.Connection, pid: str) -> dict[str, Any]:
    project = _require_project(conn, pid)
    if project["status"] != _ACTIVE:
        raise CairnError(
            ErrorCode.PROJECT_INACTIVE,
            message="项目非 active 状态",
            detail={"project_id": pid, "status": project["status"]},
        )
    return project


def _intent_to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    sources = conn.execute(
        "SELECT fact_id FROM intent_sources WHERE intent_id=? AND project_id=? ORDER BY fact_id",
        (row["id"], row["project_id"]),
    ).fetchall()
    d["from_fact_ids"] = [s["fact_id"] for s in sources]
    return d


def _require_intent(conn: sqlite3.Connection, pid: str, iid: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM intents WHERE id=? AND project_id=?", (iid, pid)).fetchone()
    if row is None:
        raise CairnError(
            ErrorCode.NOT_FOUND,
            message="intent 不存在",
            detail={"intent_id": iid, "project_id": pid},
        )
    return _intent_to_dict(conn, row)


def _goal_fact_id(conn: sqlite3.Connection, pid: str) -> str | None:
    """项目内 ``goal`` 特殊事实 id（不存在返回 None，v1 老库兼容）。"""
    row = conn.execute(
        "SELECT id FROM facts WHERE project_id=? AND description='goal' LIMIT 1", (pid,)
    ).fetchone()
    return row["id"] if row else None


def _fact_description(item: Any) -> str | None:
    """conclude facts 元素归一：字符串即 description，dict 取 description 字段。"""
    if isinstance(item, dict):
        return (item.get("description") or "").strip() or None
    if isinstance(item, str):
        return item.strip() or None
    return None


# ---------------------------------------------------------------------------
# 项目 CRUD（spec §3 / skeleton §3）
# ---------------------------------------------------------------------------


def create_project(conn: sqlite3.Connection, *, engagement_id: str, title: str,
                   bootstrap_enabled: bool = True) -> dict[str, Any]:
    """创建项目并播种 ``origin`` + ``goal`` 特殊事实（spec §2.1）。

    - engagement 必须存在（404）且 active（409 ENGAGEMENT_INVALID_STATE）；
    - 项目 id ``proj_###`` 走全局 counters；origin/goal 占用 f001/f002。
    """
    title = (title or "").strip()
    if not title:
        raise CairnError(ErrorCode.VALIDATION, message="title 必填")
    eng = conn.execute("SELECT * FROM engagements WHERE id=?", (engagement_id,)).fetchone()
    if eng is None:
        raise CairnError(
            ErrorCode.NOT_FOUND,
            message="engagement 不存在",
            detail={"engagement_id": engagement_id},
        )
    if eng["status"] != _ACTIVE:
        raise CairnError(
            ErrorCode.ENGAGEMENT_INVALID_STATE,
            message="engagement 非 active，不能创建项目",
            detail={"engagement_id": engagement_id, "status": eng["status"]},
        )
    pid = next_project_id(conn)
    now = utcnow()
    conn.execute(
        "INSERT INTO projects (id, engagement_id, title, status, bootstrap_enabled, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (pid, engagement_id, title, _ACTIVE, 1 if bootstrap_enabled else 0, now),
    )
    # 播种 origin/goal（只增不改；f001=origin, f002=goal）
    create_fact(conn, pid, description="origin")
    create_fact(conn, pid, description="goal")
    return _require_project(conn, pid)


def get_project(conn: sqlite3.Connection, pid: str) -> dict[str, Any] | None:
    """读项目；不存在返回 None。调用方（路由）读前先跑超时清理。"""
    return _project_row(conn, pid)


def list_projects(conn: sqlite3.Connection, *, engagement_id: str | None = None,
                  status: str | None = None) -> list[dict[str, Any]]:
    """项目列表（可按 engagement / status 过滤）。读前先跑超时清理（路由层）。"""
    where: list[str] = []
    params: list[Any] = []
    if engagement_id is not None:
        where.append("engagement_id=?")
        params.append(engagement_id)
    if status is not None:
        where.append("status=?")
        params.append(status)
    q = "SELECT * FROM projects"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY created_at"
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def delete_project(conn: sqlite3.Connection, pid: str) -> None:
    """物理级联删除（facts/intents/intent_sources/hints/scoped_counters 经 FK CASCADE）。"""
    _require_project(conn, pid)
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))


def set_project_title(conn: sqlite3.Connection, pid: str, title: str) -> dict[str, Any]:
    _require_project(conn, pid)
    title = (title or "").strip()
    if not title:
        raise CairnError(ErrorCode.VALIDATION, message="title 不能为空")
    conn.execute("UPDATE projects SET title=? WHERE id=?", (title, pid))
    return _require_project(conn, pid)


def set_project_status(conn: sqlite3.Connection, pid: str, status: str) -> dict[str, Any]:
    """置 active|stopped（A2 无 completed）。stopped = paused 语义（B5）：立即冻结租约。"""
    _require_project(conn, pid)
    if status not in (_ACTIVE, _STOPPED):
        raise CairnError(
            ErrorCode.VALIDATION,
            message="project status 非法",
            detail={"status": status, "allowed": [_ACTIVE, _STOPPED]},
        )
    conn.execute("UPDATE projects SET status=? WHERE id=?", (status, pid))
    if status == _STOPPED:
        freeze_project_leases(conn, pid)
    return _require_project(conn, pid)


# ---------------------------------------------------------------------------
# Fact（spec §2.2：只增不改；重复 description 幂等跳过）
# ---------------------------------------------------------------------------


def create_fact(conn: sqlite3.Connection, pid: str, *, description: str) -> dict[str, Any]:
    """创建事实（只增不改）。同 project 内重复 description 幂等跳过并返回已有事实。"""
    description = (description or "").strip()
    if not description:
        raise CairnError(ErrorCode.VALIDATION, message="fact description 不能为空")
    _require_project(conn, pid)
    existing = conn.execute(
        "SELECT * FROM facts WHERE project_id=? AND description=? ORDER BY created_at LIMIT 1",
        (pid, description),
    ).fetchone()
    if existing is not None:
        return dict(existing)
    fid = next_scoped_id(conn, pid, "fact")
    now = utcnow()
    conn.execute(
        "INSERT INTO facts (id, project_id, description, created_at) VALUES (?,?,?,?)",
        (fid, pid, description, now),
    )
    return {"id": fid, "project_id": pid, "description": description, "created_at": now}


def list_facts(conn: sqlite3.Connection, pid: str, *, after_ts: str | None = None,
               limit: int = 200) -> list[dict[str, Any]]:
    """事实列表；``after_ts`` 供 D3 时间线增量（created_at > after_ts）。"""
    where = "project_id=?"
    params: list[Any] = [pid]
    if after_ts is not None:
        where += " AND created_at > ?"
        params.append(after_ts)
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM facts WHERE {where} ORDER BY created_at LIMIT ?", params
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Intent（spec §2.3：worker 状态机 / 校验 / 租约）
# ---------------------------------------------------------------------------


def create_intent(conn: sqlite3.Connection, pid: str, *, description: str, creator: str,
                  from_fact_ids: list[str], to_fact_id: str | None = None,
                  worker: str | None = None) -> dict[str, Any]:
    """创建 intent 超边（spec §4-1/2）。

    校验：from_fact_ids 全部存在且**不含 goal**（400）；to_fact_id 存在且**非 goal**（400）；
    ``worker`` 只能为 null 或 ``== creator``（400，spec §4-2）。creator 不可变。
    项目非 active → 403（spec §4-7：reason intent 创建遇 403 视作成功收场）。
    """
    _require_active(conn, pid)
    description = (description or "").strip()
    creator = (creator or "").strip()
    if not description:
        raise CairnError(ErrorCode.VALIDATION, message="intent description 不能为空")
    if not creator:
        raise CairnError(ErrorCode.VALIDATION, message="intent creator 不能为空")
    if worker is not None and worker != creator:
        raise CairnError(
            ErrorCode.VALIDATION,
            message="创建时 worker 只能为 null 或等于 creator（spec §4-2）",
            detail={"worker": worker, "creator": creator},
        )

    goal_id = _goal_fact_id(conn, pid)
    from_ids = list(from_fact_ids or [])
    for fid in from_ids:
        fact = conn.execute(
            "SELECT * FROM facts WHERE id=? AND project_id=?", (fid, pid)
        ).fetchone()
        if fact is None:
            raise CairnError(
                ErrorCode.NOT_FOUND,
                message=f"from 事实不存在: {fid}",
                detail={"fact_id": fid, "project_id": pid},
            )
        if goal_id is not None and fid == goal_id:
            raise CairnError(
                ErrorCode.VALIDATION,
                message="goal 不能作为 intent 的 from 源（spec §4-1）",
                detail={"fact_id": fid},
            )
    if to_fact_id is not None:
        to = conn.execute(
            "SELECT * FROM facts WHERE id=? AND project_id=?", (to_fact_id, pid)
        ).fetchone()
        if to is None:
            raise CairnError(
                ErrorCode.NOT_FOUND,
                message=f"to 事实不存在: {to_fact_id}",
                detail={"fact_id": to_fact_id, "project_id": pid},
            )
        if goal_id is not None and to_fact_id == goal_id:
            raise CairnError(
                ErrorCode.VALIDATION,
                message="to_fact_id 不能指向 goal（无 to='goal' 完成边，A2）",
                detail={"fact_id": to_fact_id},
            )

    iid = next_scoped_id(conn, pid, "intent")
    now = utcnow()
    conn.execute(
        "INSERT INTO intents "
        "(id, project_id, to_fact_id, description, creator, worker, last_heartbeat_at, created_at, concluded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (iid, pid, to_fact_id, description, creator, worker, now if worker else None, now, None),
    )
    for fid in from_ids:
        conn.execute(
            "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES (?,?,?)",
            (iid, pid, fid),
        )
    return _require_intent(conn, pid, iid)


def claim_intent(conn: sqlite3.Connection, pid: str, iid: str, *, worker: str) -> dict[str, Any]:
    """认领 intent（403 非 active；409 他人持有/已 conclude）；刷新 last_heartbeat_at。"""
    _require_active(conn, pid)
    intent = _require_intent(conn, pid, iid)
    _assert_leaseable(intent, worker)
    now = utcnow()
    conn.execute(
        "UPDATE intents SET worker=?, last_heartbeat_at=? WHERE id=? AND project_id=?",
        (worker, now, iid, pid),
    )
    return _require_intent(conn, pid, iid)


def heartbeat_intent(conn: sqlite3.Connection, pid: str, iid: str, *, worker: str) -> dict[str, Any]:
    """intent 心跳（403/409 同上）；**首次心跳即认领**（worker=NULL → 置为请求者）。"""
    _require_active(conn, pid)
    intent = _require_intent(conn, pid, iid)
    _assert_leaseable(intent, worker)
    now = utcnow()
    conn.execute(
        "UPDATE intents SET worker=?, last_heartbeat_at=? WHERE id=? AND project_id=?",
        (worker, now, iid, pid),
    )
    return _require_intent(conn, pid, iid)


def release_intent(conn: sqlite3.Connection, pid: str, iid: str, *, worker: str) -> None:
    """释放 intent 租约（仅持有者可释放；403/409 抛错，Dispatcher 侧静默处理，spec §4-7）。"""
    _require_active(conn, pid)
    intent = _require_intent(conn, pid, iid)
    _assert_leaseable(intent, worker)
    conn.execute(
        "UPDATE intents SET worker=NULL, last_heartbeat_at=NULL WHERE id=? AND project_id=?",
        (iid, pid),
    )


def conclude_intent(conn: sqlite3.Connection, pid: str, iid: str, *, worker: str,
                    facts: list[Any] | None = None) -> dict[str, Any]:
    """conclude 双阶段收尾（spec §3/§5）：写 facts（只增，重复幂等）→ concluded_at + 释放租约。

    返回的 dict 额外带 ``fact_ids``（本次写出的事实 id，供路由编排 coverage/findings 溯源）。
    coverage_result/findings 由**路由层**编排 21/22（本函数不越界）。
    """
    _require_active(conn, pid)
    intent = _require_intent(conn, pid, iid)
    _assert_leaseable(intent, worker)

    fact_ids: list[str] = []
    for item in facts or []:
        description = _fact_description(item)
        if not description:
            continue
        created = create_fact(conn, pid, description=description)
        fact_ids.append(created["id"])

    now = utcnow()
    conn.execute(
        "UPDATE intents SET concluded_at=?, worker=NULL, last_heartbeat_at=NULL "
        "WHERE id=? AND project_id=?",
        (now, iid, pid),
    )
    result = _require_intent(conn, pid, iid)
    result["fact_ids"] = fact_ids
    return result


def _assert_leaseable(intent: dict[str, Any], worker: str) -> None:
    """租约仲裁：已 conclude → 409；他人持有 → 409。"""
    if intent["concluded_at"] is not None:
        raise CairnError(
            ErrorCode.LEASE_CONFLICT,
            message="intent 已 conclude，不可再操作（spec §4-8）",
            detail={"intent_id": intent["id"]},
        )
    if intent["worker"] is not None and intent["worker"] != worker:
        raise CairnError(
            ErrorCode.LEASE_CONFLICT,
            message="intent 已被他人认领",
            detail={"intent_id": intent["id"], "holder": intent["worker"]},
        )


def list_intents(conn: sqlite3.Connection, pid: str, *, open_only: bool = True) -> list[dict[str, Any]]:
    """intent 列表；``open_only=True`` 过滤未 conclude。"""
    rows = conn.execute(
        "SELECT * FROM intents WHERE project_id=? ORDER BY created_at", (pid,)
    ).fetchall()
    result = []
    for r in rows:
        d = _intent_to_dict(conn, r)
        if open_only and d["concluded_at"] is not None:
            continue
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Hint（spec §2.2：最宽松写权限，active/stopped 皆可）
# ---------------------------------------------------------------------------


def create_hint(conn: sqlite3.Connection, pid: str, *, content: str, creator: str) -> dict[str, Any]:
    """写入 hint（图外输入）。不触发除 reason 重触发外的特殊行为（spec §4-19）。"""
    _require_project(conn, pid)
    content = (content or "").strip()
    creator = (creator or "").strip()
    if not content:
        raise CairnError(ErrorCode.VALIDATION, message="hint content 不能为空")
    if not creator:
        raise CairnError(ErrorCode.VALIDATION, message="hint creator 不能为空")
    hid = next_scoped_id(conn, pid, "hint")
    now = utcnow()
    conn.execute(
        "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?,?,?,?,?)",
        (hid, pid, content, creator, now),
    )
    return {"id": hid, "project_id": pid, "content": content, "creator": creator, "created_at": now}


def list_hints(conn: sqlite3.Connection, pid: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM hints WHERE project_id=? ORDER BY created_at", (pid,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 项目级 reason 租约（projects.reason_* 列）
# ---------------------------------------------------------------------------


def claim_reason(conn: sqlite3.Connection, pid: str, *, worker: str,
                 trigger: str | None = None) -> None:
    """项目级 reason 租约认领：403 非 active；409 他人持有；写 reason_worker/started_at/trigger。"""
    _require_active(conn, pid)
    project = _require_project(conn, pid)
    if project["reason_worker"] is not None and project["reason_worker"] != worker:
        raise CairnError(
            ErrorCode.LEASE_CONFLICT,
            message="reason 租约已被他人持有",
            detail={"project_id": pid, "holder": project["reason_worker"]},
        )
    now = utcnow()
    conn.execute(
        "UPDATE projects SET reason_worker=?, reason_trigger=?, reason_started_at=?, "
        "reason_last_heartbeat_at=? WHERE id=?",
        (worker, trigger, now, now, pid),
    )


def heartbeat_reason(conn: sqlite3.Connection, pid: str, *, worker: str) -> None:
    """reason 心跳：409 他人持有/未被持有；刷新 reason_last_heartbeat_at。"""
    project = _require_project(conn, pid)
    if project["reason_worker"] is None or project["reason_worker"] != worker:
        raise CairnError(
            ErrorCode.LEASE_CONFLICT,
            message="reason 租约未被该 worker 持有",
            detail={"project_id": pid, "holder": project["reason_worker"]},
        )
    now = utcnow()
    conn.execute("UPDATE projects SET reason_last_heartbeat_at=? WHERE id=?", (now, pid))


def release_reason(conn: sqlite3.Connection, pid: str, *, worker: str) -> None:
    """释放 reason 租约（仅持有者可释放；清 reason_worker/started_at/trigger/last_heartbeat_at）。"""
    project = _require_project(conn, pid)
    if project["reason_worker"] is not None and project["reason_worker"] != worker:
        raise CairnError(
            ErrorCode.LEASE_CONFLICT,
            message="reason 租约被他人持有，无法释放",
            detail={"project_id": pid, "holder": project["reason_worker"]},
        )
    conn.execute(
        "UPDATE projects SET reason_worker=NULL, reason_trigger=NULL, "
        "reason_started_at=NULL, reason_last_heartbeat_at=NULL WHERE id=?",
        (pid,),
    )


# ---------------------------------------------------------------------------
# 冻结 / 超时清理（B5；spec §4-17）
# ---------------------------------------------------------------------------


def freeze_project_leases(conn: sqlite3.Connection, pid: str) -> None:
    """B5：冻结项目全部租约——open intent 的 worker 清空 + reason 租约清空。

    ``services/scope.py#_freeze_engagement_leases``（20）在 engagement 置 paused /
    expire 时对本 engagement 下每个 project 调用本函数。
    """
    conn.execute(
        "UPDATE intents SET worker=NULL, last_heartbeat_at=NULL "
        "WHERE project_id=? AND concluded_at IS NULL AND worker IS NOT NULL",
        (pid,),
    )
    conn.execute(
        "UPDATE projects SET reason_worker=NULL, reason_trigger=NULL, "
        "reason_started_at=NULL, reason_last_heartbeat_at=NULL WHERE id=?",
        (pid,),
    )


def intent_timeout_cleanup(conn: sqlite3.Connection, pid: str | None = None) -> list[str]:
    """读时清理：open intent 心跳超 ``settings.intent_timeout`` → worker=NULL（重新可认领）。

    已 conclude 不参与。返回被清理的 intent id 列表。
    """
    timeout = _settings_timeout(conn, "intent_timeout")
    where = "concluded_at IS NULL AND worker IS NOT NULL"
    params: list[Any] = []
    if pid is not None:
        where += " AND project_id=?"
        params.append(pid)
    rows = conn.execute(
        f"SELECT id, project_id, last_heartbeat_at FROM intents WHERE {where}", params
    ).fetchall()
    now = datetime.now(timezone.utc)
    cleaned: list[str] = []
    for r in rows:
        ts = _parse_ts(r["last_heartbeat_at"])
        if ts is None or (now - ts).total_seconds() > timeout:
            conn.execute(
                "UPDATE intents SET worker=NULL, last_heartbeat_at=NULL WHERE id=? AND project_id=?",
                (r["id"], r["project_id"]),
            )
            cleaned.append(r["id"])
    if cleaned:
        logger.info("intent_timeout_cleanup: 清理 %d 条过期租约（pid=%s）", len(cleaned), pid)
    return cleaned


def reason_timeout_cleanup(conn: sqlite3.Connection, pid: str | None = None) -> None:
    """读时清理：reason 心跳超 ``settings.reason_timeout`` → 清租约。"""
    timeout = _settings_timeout(conn, "reason_timeout")
    where = "reason_worker IS NOT NULL"
    params: list[Any] = []
    if pid is not None:
        where += " AND id=?"
        params.append(pid)
    rows = conn.execute(
        f"SELECT id, reason_last_heartbeat_at FROM projects WHERE {where}", params
    ).fetchall()
    now = datetime.now(timezone.utc)
    for r in rows:
        ts = _parse_ts(r["reason_last_heartbeat_at"])
        if ts is None or (now - ts).total_seconds() > timeout:
            conn.execute(
                "UPDATE projects SET reason_worker=NULL, reason_trigger=NULL, "
                "reason_started_at=NULL, reason_last_heartbeat_at=NULL WHERE id=?",
                (r["id"],),
            )
            logger.info("reason_timeout_cleanup: 清理过期 reason 租约 pid=%s", r["id"])


# ---------------------------------------------------------------------------
# 导出（spec §3 / §5：图快照 YAML，可被 13/30 图快照逻辑消费）
# ---------------------------------------------------------------------------


def export_graph_yaml(conn: sqlite3.Connection, pid: str) -> str:
    """图快照 YAML：含 origin/goal 特殊事实 + 全部 fact/intent/hint。"""
    project = _require_project(conn, pid)
    facts = list_facts(conn, pid)
    intents = list_intents(conn, pid, open_only=False)
    hints = list_hints(conn, pid)
    doc: dict[str, Any] = {
        "project": {
            "id": project["id"],
            "engagement_id": project["engagement_id"],
            "title": project["title"],
            "status": project["status"],
            "bootstrap_enabled": bool(project["bootstrap_enabled"]),
            "created_at": project["created_at"],
        },
        "facts": [
            {"id": f["id"], "description": f["description"], "created_at": f["created_at"]}
            for f in facts
        ],
        "intents": [
            {
                "id": i["id"],
                "description": i["description"],
                "creator": i["creator"],
                "worker": i["worker"],
                "to_fact_id": i["to_fact_id"],
                "from_fact_ids": i["from_fact_ids"],
                "created_at": i["created_at"],
                "concluded_at": i["concluded_at"],
            }
            for i in intents
        ],
        "hints": [
            {"id": h["id"], "content": h["content"], "creator": h["creator"], "created_at": h["created_at"]}
            for h in hints
        ],
    }
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)
