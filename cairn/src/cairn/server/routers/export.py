"""探索图导出路由（skeleton §2.4 / exploration-graph-spec §5；Agent 25）。

- ``GET /projects/{pid}/export?format=yaml``：图快照 YAML（含 origin/goal/全部 fact/intent/hint），
  可被 13/30 的图快照逻辑消费（spec §4-14：prompt 只给文件引用路径）。
- ``GET /projects/{pid}/export?format=timeline``：事实增量 JSON（``?after_ts=`` 增量，D3 时间线源）。

读前先跑超时清理（spec §3 注释：读到的即清理后状态）。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from ..db import get_db
from ..errors import CairnError, ErrorCode
from ..services import graph as svc

router = APIRouter(prefix="/projects/{pid}/export", tags=["export"])

_YAML_MEDIA = "application/yaml; charset=utf-8"


@router.get("")
def export_graph(
    pid: str,
    format: str = "yaml",
    after_ts: str | None = None,
    db: sqlite3.Connection = Depends(get_db),
) -> Response:
    """图快照导出。``format=yaml``（默认）返回 YAML 文本；``format=timeline`` 返回事实增量 JSON。"""
    svc.intent_timeout_cleanup(db, pid=pid)
    svc.reason_timeout_cleanup(db, pid=pid)
    db.commit()
    if format == "timeline":
        facts = svc.list_facts(db, pid, after_ts=after_ts)
        return JSONResponse({"project_id": pid, "facts": facts})
    if format != "yaml":
        raise CairnError(ErrorCode.VALIDATION, message="format 仅支持 yaml|timeline",
                         detail={"format": format})
    text = svc.export_graph_yaml(db, pid)
    return Response(content=text, media_type=_YAML_MEDIA)
