"""覆盖写回（Agent 30）—— B1 格子互斥 / C9 幂等写回 / A5 复测支持 / bootstrap 播种。

契约：
- 所有写回经 12 的 ``CairnClient``（C5：Agent 容器不持 token）。
- B1：``claim_item``/``release_item`` 调 21 的 ``POST /coverage/items/{cid}/claim|release``
  （路径假设，阶段 2 联调对齐 12 交接物 §3）。
- C9 幂等：``write_result`` 携带 ``Idempotency-Key`` 头，键 = ``{item_id}:{intent_id}``
  （服务端以 ``(item_id, intent_id)`` 去重；重发 no-op 成功而非 409）。
- ``COVERAGE_ALREADY_COVERED``（他人认领/已测）是**预期分支**：写回作废 + release，
  交由下轮 reason 重排，不是校验事故。
- G：写失败退避 1 次（tuning.writeback_retries），仍失败抛 ``WritebackError``。
- ``seed_from_discovery``：bootstrap 播种覆盖项（21 的 ``seed_from_discovery`` 未实现，
  本模块按 coverage spec §1.1 服务→测试项映射在 Dispatcher 侧最小播种，best-effort）。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from ..errors import (
    COVERAGE_ALREADY_COVERED,
    COVERAGE_NOT_APPLICABLE,
    CairnClientError,
)
from ..tasks.common import COVERAGE_OUTCOMES, DEPTHS, with_retry

logger = logging.getLogger("cairn.dispatcher.coverage.writer")

#: B1 认领/释放端点路径假设（12 交接物 §3「路径假设」；21 路由已实现）
_CLAIM_PATH = "/engagements/{eid}/coverage/items/{cid}/claim"
_RELEASE_PATH = "/engagements/{eid}/coverage/items/{cid}/release"

#: 播种：服务类型 → 适用测试项 slug（coverage spec §1.1；识别不到的只播种扫描基线项）
SERVICE_TO_TEST_TYPES: dict[str, tuple[str, ...]] = {
    "http": ("web_sqli", "web_xss", "web_auth_bypass", "web_weak_credentials", "web_info_disclosure", "vuln_scanning"),
    "https": ("web_sqli", "web_xss", "web_auth_bypass", "web_weak_credentials", "web_info_disclosure", "vuln_scanning"),
    "tomcat": ("web_sqli", "web_xss", "web_auth_bypass", "web_weak_credentials", "vuln_scanning"),
    "nginx": ("web_info_disclosure", "vuln_scanning", "tech_fingerprint"),
    "ssh": ("net_service_hardening", "net_ssh_brute", "net_default_creds"),
    "mysql": ("net_service_hardening", "net_default_creds"),
    "postgres": ("net_service_hardening", "net_default_creds"),
    "redis": ("net_service_hardening", "net_default_creds"),
    "ftp": ("net_default_creds",),
    "snmp": ("net_snmp", "net_default_creds"),
    "dns": ("service_identification",),
}
#: 默认（服务识别不到的）最低播种项
_DEFAULT_SEED_SLUGS: tuple[str, ...] = ("asset_discovery", "service_identification", "vuln_scanning", "tech_fingerprint")


class CoverageWriter:
    """覆盖写回器：认领/释放/写回/播种（B1/C9/G）。"""

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

    # ---- B1 格子互斥 ----

    def claim_item(self, eid: str, item_id: str, intent_id: str) -> bool:
        """B1 认领：untested 且未被认领 → True；已被他人认领/已测 → False（不派发）。"""
        resp = self.client._request(
            "POST", _CLAIM_PATH.format(eid=eid, cid=item_id), json={"intent_id": intent_id}
        )
        return bool(resp.get("claimed"))

    def release_item(self, eid: str, item_id: str, intent_id: str) -> None:
        """B1 释放：仅 current_intent_id == intent_id 回退 untested（NULL 不放行）。"""
        try:
            self.client._request(
                "POST", _RELEASE_PATH.format(eid=eid, cid=item_id), json={"intent_id": intent_id}
            )
        except CairnClientError as exc:  # 释放失败不阻断（下轮 reason 重排）
            self.log(f"release_item 失败（忽略）: item={item_id} exc={exc}")

    def claim_all(self, eid: str, item_ids: Iterable[str], intent_id: str) -> tuple[list[str], list[str]]:
        """逐一认领；返回 ``(claimed_ids, busy_ids)``。任一格子忙 → 该 intent 不派发（B1）。"""
        claimed: list[str] = []
        busy: list[str] = []
        for iid in item_ids:
            try:
                ok = self.claim_item(eid, iid, intent_id)
            except CairnClientError as exc:
                self.log(f"claim 失败 item={iid}: {exc}")
                ok = False
            if ok:
                claimed.append(iid)
            else:
                busy.append(iid)
        return claimed, busy

    # ---- C9 幂等写回 ----

    def write_result(
        self,
        eid: str,
        *,
        item_ids: Iterable[str],
        depth_achieved: str,
        outcome: str,
        intent_id: str,
        fact_id: Optional[str] = None,
        evidence_refs: Optional[Iterable[str]] = None,
        tested_scope: Any = None,
        partial: bool = False,
    ) -> dict:
        """explore 覆盖写回（带重试 + Idempotency-Key，键 = item_id:intent_id）。

        ``COVERAGE_ALREADY_COVERED`` 确定性拒绝不重试（预期分支，调用方作废+release）。
        """
        if depth_achieved not in DEPTHS:
            raise ValueError(f"非法 depth_achieved: {depth_achieved!r}")
        if outcome not in COVERAGE_OUTCOMES:
            raise ValueError(f"非法 outcome: {outcome!r}")
        item_ids = list(item_ids)
        idem_key = ";".join(f"{i}:{intent_id}" for i in item_ids)

        def _do() -> dict:
            return self.client.write_coverage_result(
                eid,
                item_ids=item_ids,
                depth_achieved=depth_achieved,
                outcome=outcome,
                fact_id=fact_id,
                intent_id=intent_id,
                evidence_refs=list(evidence_refs or []),
                tested_scope=tested_scope,
                partial=partial,
                idempotency_key=idem_key,
            )

        return with_retry(_do, retries=self.retries, backoff=self.backoff, log=self.log)

    # ---- A5 复测支持（best-effort；服务端 rebuild 由 41/25 编排） ----

    def rebuild_for_retest(
        self,
        eid: str,
        *,
        target_id: str,
        test_type_id: str,
        depth: Optional[str] = None,
    ) -> Optional[dict]:
        """A5：finding fixed 后复用原覆盖项行（retest_round+1）。服务端重建端点为 41/25
        编排；本层提供 best-effort 调用（经 ``PUT /coverage/items/{cid}`` 校准深度）。

        说明：21 的 ``rebuild_for_retest`` 是服务端服务函数（无路由），Dispatcher 侧无法
        直接触发完整重建；本函数返回 ``None`` 表示「等待 41/25 服务端编排」，仅保留接口
        供 40 接线。实际复测写回由 ``write_result`` 对已重建格子完成（retest 语义由
        ``(item_id, intent_id)`` 幂等键天然覆盖）。
        """
        # 寻找 (target, test_type) 覆盖项
        try:
            items = self.client.list_items(eid) or []
        except CairnClientError as exc:  # pragma: no cover
            self.log(f"list_items 失败: {exc}")
            return None
        for it in items:
            if it.get("target_id") == target_id and it.get("test_type_id") == test_type_id:
                if depth is not None and depth != it.get("depth_required"):
                    try:
                        self.client._request(
                            "PUT",
                            f"/engagements/{eid}/coverage/items/{it['id']}",
                            json={"depth_required": depth},
                        )
                    except CairnClientError as exc:  # pragma: no cover
                        self.log(f"rebuild 校准深度失败: {exc}")
                return it
        return None

    # ---- bootstrap 播种 ----

    def seed_from_discovery(
        self,
        eid: str,
        discoveries: Iterable[dict],
        *,
        scope: Optional[str] = None,
    ) -> dict:
        """bootstrap discoveries 播种：为每个发现资产建 target + 覆盖项。

        - 先 ``check_scope``（authorized 命中/auto_created 由服务端判定；prohibited 403 跳过）；
        - 目标不存在则 ``create_target``（auto_created 语义由服务端 scope guard 落实）；
        - 按服务→测试项映射建覆盖项（``POST /coverage/items``，幂等 upsert）。
        返回 ``{"targets_created": n, "items_created": n, "skipped": [...]}``。
        """
        result: dict[str, Any] = {"targets_created": 0, "items_created": 0, "skipped": []}
        try:
            coverage = self.client.get_coverage(eid) or {}
            test_types = {t["id"]: t for t in coverage.get("test_types", [])}
        except CairnClientError:
            test_types = {}
        for d in discoveries:
            value = (d.get("target") or "").strip()
            service = (d.get("service") or "").strip().lower()
            if not value:
                continue
            # scope guard：prohibited 命中即 403（服务端判定）；None 表示未命中，跳过
            try:
                checked = self.client.check_scope(eid, value)
            except CairnClientError as exc:
                result["skipped"].append({"target": value, "reason": f"scope: {exc}"})
                continue
            target = checked if checked else None
            if target is None:
                try:
                    target = self.client.create_target(
                        eid, value, note=f"bootstrap discovery service={service}" if service else None
                    )
                    result["targets_created"] += 1
                except CairnClientError as exc:
                    result["skipped"].append({"target": value, "reason": f"target: {exc}"})
                    continue
            # 播种覆盖项
            slugs = SERVICE_TO_TEST_TYPES.get(service, _DEFAULT_SEED_SLUGS)
            for slug in slugs:
                tt_id = f"tt_{slug}"
                if test_types and tt_id not in test_types:
                    continue  # 目录禁用/不存在 → 跳过（spec §1.1：只作用于 enabled 项）
                try:
                    self.client._request(
                        "POST",
                        f"/engagements/{eid}/coverage/items",
                        json={"target_id": target["id"], "test_type_id": tt_id, "seed_source": "auto"},
                    )
                    result["items_created"] += 1
                except CairnClientError as exc:
                    result["skipped"].append({"target": value, "tt": tt_id, "reason": f"item: {exc}"})
        return result
