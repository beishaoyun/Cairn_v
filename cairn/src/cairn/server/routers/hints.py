"""探索图 hint 路由（skeleton §2.4 / exploration-graph-spec §5；Agent 25）。

Hint 是图外输入，写权限最宽松：active/stopped 皆可写（spec §4-19），
不触发除 reason 重触发外的特殊行为。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from ..db import get_db
from ..services import graph as svc

router = APIRouter(prefix="/projects/{pid}/hints", tags=["hints"])


class HintIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    creator: str


@router.post("", status_code=201)
def create_hint(
    pid: str,
    payload: HintIn,
    db: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    hint = svc.create_hint(db, pid, content=payload.content, creator=payload.creator)
    db.commit()
    return hint
