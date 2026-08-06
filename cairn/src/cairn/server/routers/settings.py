"""配置子域路由：GET/PUT /settings（skeleton §2.1；DDL §1 单例行 rowid=1）。

字段：``intent_timeout`` / ``reason_timeout`` / ``global_kill_switch`` / ``coverage_policy``。
PUT 支持部分更新（缺省字段保留现值）。
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..db import get_db
from ..errors import CairnError, ErrorCode

router = APIRouter(prefix="/settings", tags=["settings"])

_SETTINGS_COLS = ("intent_timeout", "reason_timeout", "global_kill_switch", "coverage_policy")


class SettingsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_timeout: int
    reason_timeout: int
    global_kill_switch: int
    coverage_policy: dict


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_timeout: int | None = Field(default=None, ge=1)
    reason_timeout: int | None = Field(default=None, ge=1)
    global_kill_switch: int | None = Field(default=None, ge=0, le=1)
    coverage_policy: dict | None = None


def _ensure_settings_row(db: sqlite3.Connection) -> sqlite3.Row:
    db.execute(
        "INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout, global_kill_switch, coverage_policy) "
        "VALUES (1, 15, 15, 0, '{}')"
    )
    db.commit()
    row = db.execute(f"SELECT {', '.join(_SETTINGS_COLS)} FROM settings WHERE rowid = 1").fetchone()
    if row is None:  # pragma: no cover
        raise CairnError(ErrorCode.INTERNAL, message="settings 单例行初始化失败", detail={"rowid": 1})
    return row


def _to_out(row) -> SettingsOut:
    return SettingsOut(
        intent_timeout=row["intent_timeout"],
        reason_timeout=row["reason_timeout"],
        global_kill_switch=row["global_kill_switch"],
        coverage_policy=json.loads(row["coverage_policy"] or "{}"),
    )


@router.get("", response_model=SettingsOut)
def get_settings(db: sqlite3.Connection = Depends(get_db)) -> SettingsOut:
    return _to_out(_ensure_settings_row(db))


@router.put("", response_model=SettingsOut)
def put_settings(payload: SettingsUpdate, db: sqlite3.Connection = Depends(get_db)) -> SettingsOut:
    current = _ensure_settings_row(db)
    intent_timeout = payload.intent_timeout if payload.intent_timeout is not None else current["intent_timeout"]
    reason_timeout = payload.reason_timeout if payload.reason_timeout is not None else current["reason_timeout"]
    global_kill_switch = (
        payload.global_kill_switch if payload.global_kill_switch is not None else current["global_kill_switch"]
    )
    coverage_policy = (
        payload.coverage_policy if payload.coverage_policy is not None else json.loads(current["coverage_policy"] or "{}")
    )
    db.execute(
        "UPDATE settings SET intent_timeout=?, reason_timeout=?, global_kill_switch=?, coverage_policy=? WHERE rowid=1",
        (
            intent_timeout,
            reason_timeout,
            global_kill_switch,
            json.dumps(coverage_policy, ensure_ascii=False),
        ),
    )
    db.commit()
    return _to_out(
        db.execute(f"SELECT {', '.join(_SETTINGS_COLS)} FROM settings WHERE rowid = 1").fetchone()
    )
