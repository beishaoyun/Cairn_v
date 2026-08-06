"""Cairn Server 服务端配置（Agent 10 所有，skeleton §1）。

DB 路径 / token / 证据目录 / 分页默认全部可用环境变量覆盖；`get_config()` 每次从
环境变量重建，便于测试用 monkeypatch 覆盖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# 分页默认（v2 §7.2：列表接口统一 offset/limit）
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 200

DEFAULT_DB_PATH = "data/cairn.db"
DEFAULT_EVIDENCE_ROOT = "data/evidence"
DEFAULT_TRAFFIC_ROOT = "data/traffic"
DEFAULT_ARCHIVE_ROOT = "data/archive"
DEFAULT_LOGS_ROOT = "data/logs"


@dataclass(frozen=True)
class ServerConfig:
    """服务端配置。字段语义：

    - ``db_path``         SQLite 数据库文件路径（env CAIRN_DB_PATH）
    - ``api_token``       共享 Bearer Token（D2：T/H 同一 token，env CAIRN_API_TOKEN）
    - ``evidence_root``   证据全量文件根（evidence_root/{engagement_id}/<rel>，B7）
    - ``traffic_root``    捕获流量文件根
    - ``archive_root``    归档根（C4）
    - ``logs_root``       任务原始流分片文件根（progress/task_events.raw_path，懒加载）
    - ``page_size``/``max_page_size`` 列表分页默认/上限
    """

    db_path: str = DEFAULT_DB_PATH
    api_token: str | None = None
    evidence_root: str = DEFAULT_EVIDENCE_ROOT
    traffic_root: str = DEFAULT_TRAFFIC_ROOT
    archive_root: str = DEFAULT_ARCHIVE_ROOT
    logs_root: str = DEFAULT_LOGS_ROOT
    page_size: int = DEFAULT_PAGE_SIZE
    max_page_size: int = MAX_PAGE_SIZE
    host: str = "127.0.0.1"
    port: int = 8000

    def token(self) -> str | None:
        """鉴权 token：显式传入优先，否则回退环境变量 CAIRN_API_TOKEN。"""
        if self.api_token is not None:
            return self.api_token
        return os.environ.get("CAIRN_API_TOKEN")


def get_config() -> ServerConfig:
    """从环境变量构造服务端配置（每次读取，便于测试覆盖）。"""
    return ServerConfig(
        db_path=os.environ.get("CAIRN_DB_PATH", DEFAULT_DB_PATH),
        api_token=os.environ.get("CAIRN_API_TOKEN"),
        evidence_root=os.environ.get("CAIRN_EVIDENCE_ROOT", DEFAULT_EVIDENCE_ROOT),
        traffic_root=os.environ.get("CAIRN_TRAFFIC_ROOT", DEFAULT_TRAFFIC_ROOT),
        archive_root=os.environ.get("CAIRN_ARCHIVE_ROOT", DEFAULT_ARCHIVE_ROOT),
        logs_root=os.environ.get("CAIRN_LOGS_ROOT", DEFAULT_LOGS_ROOT),
    )
