"""Finding 写回（Agent 30）—— 落库 + 去重 + 证据挂载（http/commands/traffic），带重试。

契约：
- 所有写回经 12 的 ``CairnClient``（C5：Agent 容器不持 token）；``detected_by`` 传
  worker 名、``actor='agent'``（仅 open，黄金不变量 3）。
- 去重（B3）：服务端 ``FINDING_DUP``（409）→ 命中已有，跳过创建并**追加证据**到已有
  finding（22 §2 验收 2 的「命中已有→追加证据」路径）。
- 证据挂载：``http[]``（agent_typed 语义注释）走 ``add_http_evidence``；``commands[]`` 走
  ``add_command_evidence``；``traffic_ids``（role='trigger'，source='captured'）走
  ``link_traffic``（C2：traffic 为真相引用，http[] 仅为语义注释）。
- G：写失败退避 1 次（tuning.writeback_retries），仍失败只记日志 + 抛 ``WritebackError``。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from ..errors import FINDING_DUP, CairnClientError
from ..tasks.common import validate_findings_payload, with_retry

logger = logging.getLogger("cairn.dispatcher.findings.writer")


class FindingsWriter:
    """Finding 写回器：创建 + 证据 + 流量关联（带重试）。"""

    def __init__(
        self,
        client: Any,
        *,
        retries: int = 1,
        backoff: float = 0.5,
        log: Optional[Any] = None,
    ) -> None:
        self.client = client
        self.retries = retries
        self.backoff = backoff
        self.log = log or (lambda _m: None)

    # ------------------------------------------------------------------
    # 单 finding 写回
    # ------------------------------------------------------------------

    def _attach_evidence(
        self,
        eid: str,
        fid: str,
        finding: dict,
        *,
        actor: str,
    ) -> None:
        """证据挂载：http[]（agent_typed）+ commands[] + traffic_ids（role=trigger）。"""
        for h in finding.get("http") or []:
            entry = dict(h)
            entry.setdefault("source", "agent_typed")
            entry["actor"] = actor
            try:
                self.client.add_http_evidence(eid, fid, entry)
            except CairnClientError as exc:  # 单条证据失败不阻断
                self.log(f"add_http_evidence 失败（忽略）: {exc}")
        for c in finding.get("commands") or []:
            try:
                self.client.add_command_evidence(eid, fid, c)
            except CairnClientError as exc:
                self.log(f"add_command_evidence 失败（忽略）: {exc}")
        tids = finding.get("traffic_ids") or []
        if tids:
            try:
                self.client.link_traffic(eid, fid, tids, role="trigger", source="captured")
            except CairnClientError as exc:
                self.log(f"link_traffic(trigger) 失败（忽略）: {exc}")

    def _write_one(
        self,
        eid: str,
        finding: dict,
        *,
        detected_by: str,
        actor: str,
    ) -> dict:
        """创建单个 finding（含重试），返回 created finding dict。

        ``FINDING_DUP``：命中已有（22 detail.finding_id）→ 不重复建单，追加证据到已有。
        """
        payload = dict(finding)
        for key in ("http", "commands", "traffic_ids"):
            payload.pop(key, None)

        def _do() -> dict:
            return self.client.create_finding(
                eid, payload, detected_by=detected_by, actor=actor
            )

        try:
            created = with_retry(_do, retries=self.retries, backoff=self.backoff, log=self.log)
        except CairnClientError as exc:
            if exc.error_code == FINDING_DUP:
                # B3：命中已有 → 追加证据（detail 携带已有 finding_id）
                existing_id = None
                if isinstance(exc.detail, dict):
                    existing_id = exc.detail.get("finding_id")
                self.log(f"FINDING_DUP 命中已有，追加证据: title={finding.get('title')!r} fid={existing_id}")
                if existing_id:
                    self._attach_evidence(eid, existing_id, finding, actor=actor)
                return {"id": existing_id, "duplicate": True, "title": finding.get("title")}
            raise
        # 证据挂载（以创建后的 fid）
        self._attach_evidence(eid, created.get("id"), finding, actor=actor)
        return created

    # ------------------------------------------------------------------
    # 批量写回
    # ------------------------------------------------------------------

    def write(
        self,
        eid: str,
        *,
        findings: Iterable[dict],
        detected_by: str,
        actor: str = "agent",
        source_fact_id: Optional[str] = None,
        coverage_item_id: Optional[str] = None,
    ) -> list[dict]:
        """批量写回 findings（先校验白名单，再逐个创建+证据）。

        - ``findings`` 先过 ``validate_findings_payload``（契约拒绝即抛，任务失败）；
        - ``source_fact_id``/``coverage_item_id`` 注入每个 payload（图/覆盖关联）。
        返回 created finding dict 列表（含 duplicate 标记）。
        """
        validated = validate_findings_payload(list(findings))
        created: list[dict] = []
        for f in validated:
            f = dict(f)
            if source_fact_id is not None:
                f.setdefault("source_fact_id", source_fact_id)
            if coverage_item_id is not None:
                f.setdefault("coverage_item_id", coverage_item_id)
            created.append(
                self._write_one(eid, f, detected_by=detected_by, actor=actor)
            )
        return created
