"""CairnClient —— Dispatcher 调 Cairn Server 的唯一 HTTP 客户端。

契约：
- 单 ``CairnClient(base_url, token)``，所有请求带 ``Authorization: Bearer <token>``。
- 非 2xx 按 v2 §7.2/§7.3 解析，抛 ``cairn.dispatcher.errors.CairnClientError``
  （``error_code`` 与服务端顶层 error_code 一致）。
- 方法面映射 `backend-module-skeleton.md` §2/§3（服务端子域 20-24 实现端点；
  本层先按契约写，联调在阶段 2）。
- **不缓存任何 Server 数据**：每次调用独立走 HTTP。

方法 -> 端点 / 服务签名映射表见 `dev-agents/notes/12-dispatcher-config.md`。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

import httpx

from ..errors import raise_for_error

#: 携带 idempotency key 的 HTTP 头（coverage writeback 幂等）
_IDEMPOTENCY_HEADER = "Idempotency-Key"


def _q(**kw: Any) -> dict[str, str]:
    """过滤 None 的 query 参数。"""
    return {k: str(v) for k, v in kw.items() if v is not None}


class CairnClient:
    """Cairn Server 客户端。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        if client is not None:
            # 注入已有 httpx.Client（测试可传 FastAPI TestClient 驱动 ASGI stub）。
            self._client = client
            self._client.headers["Authorization"] = f"Bearer {token}"
            self._owns_client = False
        else:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------
    # 低层
    # ------------------------------------------------------------------
    def _headers(self, headers: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        """每请求显式携带 Bearer 头（不依赖底层 client 的默认 headers）。"""
        merged = {"Authorization": f"Bearer {self.token}"}
        if headers:
            merged.update(headers)
        return merged

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        files: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        resp = self._client.request(
            method, path, params=params, json=json, data=data, files=files, headers=self._headers(headers)
        )
        if resp.status_code >= 400:
            raise_for_error(resp)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _request_text(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
    ) -> str:
        resp = self._client.request(method, path, params=params, headers=self._headers())
        if resp.status_code >= 400:
            raise_for_error(resp)
        return resp.text

    # ------------------------------------------------------------------
    # engagement（skeleton §2.2 / services.scope）
    # ------------------------------------------------------------------
    def list_active(self) -> list[dict]:
        """活跃 engagement 列表（含 scope/kill 状态）。GET /engagements?status=active"""
        return self._request("GET", "/engagements", params={"status": "active"})

    def get(self, eid: str) -> dict:
        """单个 engagement 详情。GET /engagements/{eid}"""
        return self._request("GET", f"/engagements/{eid}")

    def set_status(self, eid: str, status: str, *, retest: bool = False) -> dict:
        """状态机流转（planning/active/paused/completed/archived）。PUT /engagements/{eid}/status"""
        return self._request("PUT", f"/engagements/{eid}/status", json={"status": status, "retest": retest})

    def kill(self, eid: str) -> dict:
        """熔断开关。POST /engagements/{eid}/kill"""
        return self._request("POST", f"/engagements/{eid}/kill", json={})

    # ------------------------------------------------------------------
    # scope / targets（skeleton §2.2 / services.scope）
    # ------------------------------------------------------------------
    def list_targets(self, eid: str) -> list[dict]:
        """范围目标列表。GET /engagements/{eid}/targets"""
        return self._request("GET", f"/engagements/{eid}/targets")

    def create_target(self, eid: str, value: str, **extra: Any) -> dict:
        """登记范围目标。POST /engagements/{eid}/targets"""
        payload: dict[str, Any] = {"value": value}
        if "scope" in extra:
            payload["scope"] = extra.pop("scope")
        payload.update(extra)
        return self._request("POST", f"/engagements/{eid}/targets", json=payload)

    def check_scope(self, eid: str, value: str) -> dict:
        """运行时 scope 守卫：目标不在授权范围 → 403 SCOPE_DENIED（抛 ScopeDeniedError）。

        对应 services.scope.check_scope_allowed。端点为进程内守卫查询：
        GET /engagements/{eid}/scope/check?value=...
        """
        return self._request("GET", f"/engagements/{eid}/scope/check", params={"value": value})

    # ------------------------------------------------------------------
    # coverage（skeleton §2.3 / services.coverage）
    # ------------------------------------------------------------------
    def get_coverage(self, eid: str) -> dict:
        """矩阵+热力图数据。GET /engagements/{eid}/coverage"""
        return self._request("GET", f"/engagements/{eid}/coverage")

    def get_gaps(
        self,
        eid: str,
        *,
        exclude_in_progress: bool = False,
        threshold: Optional[Any] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """缺口清单（reason 输入）。GET /engagements/{eid}/coverage/gaps"""
        params = {"exclude_in_progress": str(exclude_in_progress).lower()}
        if threshold is not None:
            params["threshold"] = str(threshold)
        if limit is not None:
            params["limit"] = str(limit)
        return self._request("GET", f"/engagements/{eid}/coverage/gaps", params=params)

    def list_items(self, eid: str) -> list[dict]:
        """覆盖项列表。GET /engagements/{eid}/coverage/items"""
        return self._request("GET", f"/engagements/{eid}/coverage/items")

    def waive(self, eid: str, item_id: str, kind: str, reason: str, *, by: Optional[str] = None) -> dict:
        """人工豁免（kind+reason）。POST /engagements/{eid}/coverage/items/{cid}/waive"""
        payload: dict[str, Any] = {"kind": kind, "reason": reason}
        if by is not None:
            payload["by"] = by
        return self._request("POST", f"/engagements/{eid}/coverage/items/{item_id}/waive", json=payload)

    def write_coverage_result(
        self,
        eid: str,
        *,
        item_ids: Iterable[str],
        depth_achieved: str,
        outcome: str,
        fact_id: str,
        intent_id: str,
        evidence_refs: Optional[Iterable[str]] = None,
        tested_scope: Optional[Any] = None,
        partial: bool = False,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """覆盖写回（= 服务端 coverage 写回）。

        POST /engagements/{eid}/coverage/result。携带 ``Idempotency-Key`` 头防重放
        （coverage_records 以 (item_id, intent_id) + 幂等键去重，spec coverage §3）。
        """
        payload: dict[str, Any] = {
            "item_ids": list(item_ids),
            "depth_achieved": depth_achieved,
            "outcome": outcome,
            "fact_id": fact_id,
            "intent_id": intent_id,
            "evidence_refs": list(evidence_refs or []),
            "tested_scope": tested_scope,
            "partial": partial,
        }
        headers = {_IDEMPOTENCY_HEADER: idempotency_key} if idempotency_key else None
        return self._request("POST", f"/engagements/{eid}/coverage/result", json=payload, headers=headers)

    # ------------------------------------------------------------------
    # graph（skeleton §2.4 / services.graph）
    # ------------------------------------------------------------------
    def export_yaml(self, pid: str) -> str:
        """图 YAML 快照（纯文本）。GET /projects/{pid}/export?format=yaml"""
        return self._request_text("GET", f"/projects/{pid}/export", params={"format": "yaml"})

    def claim_intent(self, pid: str, iid: str, *, worker: str) -> dict:
        """认领 intent（他人持有 → 409 LEASE_CONFLICT）。POST /projects/{pid}/intents/{iid}/claim"""
        return self._request("POST", f"/projects/{pid}/intents/{iid}/claim", json={"worker": worker})

    def heartbeat_intent(self, pid: str, iid: str, *, worker: str) -> dict:
        """intent 心跳保活。POST /projects/{pid}/intents/{iid}/heartbeat"""
        return self._request("POST", f"/projects/{pid}/intents/{iid}/heartbeat", json={"worker": worker})

    def release_intent(self, pid: str, iid: str, *, worker: str) -> dict:
        """释放 intent。POST /projects/{pid}/intents/{iid}/release"""
        return self._request("POST", f"/projects/{pid}/intents/{iid}/release", json={"worker": worker})

    def conclude_intent(
        self,
        pid: str,
        iid: str,
        *,
        worker: str,
        facts: Optional[list[str]] = None,
    ) -> dict:
        """intent 结论（写事实 + 释放）。POST /projects/{pid}/intents/{iid}/conclude"""
        payload: dict[str, Any] = {"worker": worker}
        if facts is not None:
            payload["facts"] = facts
        return self._request("POST", f"/projects/{pid}/intents/{iid}/conclude", json=payload)

    # ------------------------------------------------------------------
    # findings（skeleton §2.5 / services.findings）
    # ------------------------------------------------------------------
    def create_finding(
        self,
        eid: str,
        payload: dict,
        *,
        detected_by: Optional[str] = None,
        actor: str = "agent",
    ) -> dict:
        """登记 finding（agent 只能 open）。POST /engagements/{eid}/findings"""
        body = dict(payload)
        if detected_by:
            body["detected_by"] = detected_by
        body["actor"] = actor
        return self._request("POST", f"/engagements/{eid}/findings", json=body)

    def upload_evidence(self, eid: str, fid: str, file: Any, kind: str) -> dict:
        """上传证据文件（multipart）。POST /engagements/{eid}/findings/{fid}/evidence"""
        filename = getattr(file, "name", "evidence")
        content_type = getattr(file, "content_type", "application/octet-stream")
        files = {"file": (filename, file, content_type)}
        return self._request(
            "POST", f"/engagements/{eid}/findings/{fid}/evidence", files=files, data={"kind": kind}
        )

    def add_http_evidence(self, eid: str, fid: str, http_obj: dict) -> dict:
        """请求/响应包证据登记。POST /engagements/{eid}/findings/{fid}/http"""
        return self._request("POST", f"/engagements/{eid}/findings/{fid}/http", json=http_obj)

    def add_command_evidence(self, eid: str, fid: str, cmd: dict) -> dict:
        """命令回显证据登记。POST /engagements/{eid}/findings/{fid}/commands"""
        return self._request("POST", f"/engagements/{eid}/findings/{fid}/commands", json=cmd)

    def link_traffic(
        self,
        eid: str,
        fid: str,
        traffic_ids: Iterable[str],
        role: str,
        *,
        source: Optional[str] = None,
    ) -> dict:
        """关联流量（role=trigger/verification/replay）。POST /engagements/{eid}/findings/{fid}/traffic"""
        payload: dict[str, Any] = {"traffic_ids": list(traffic_ids), "role": role}
        if source is not None:
            payload["source"] = source
        return self._request("POST", f"/engagements/{eid}/findings/{fid}/traffic", json=payload)

    # ------------------------------------------------------------------
    # traffic（skeleton §2.5 / services.capture）
    # ------------------------------------------------------------------
    def list_traffic(self, eid: str, *, client: Optional[str] = None, since: Optional[str] = None) -> list[dict]:
        """捕获流量索引/检索。GET /engagements/{eid}/traffic"""
        return self._request("GET", f"/engagements/{eid}/traffic", params=_q(client=client, since=since))

    def resolve_traffic(self, eid: str, tid: str, *, for_model: bool = True) -> dict:
        """还原流量：for_model=true → digest（≤digest_budget）；false → 全量。

        GET /engagements/{eid}/traffic/{tid}?for_model=...
        （服务端 services.capture.resolve_traffic 默认 for_model=False；客户端面向
        LLM 消费默认 True，见 12-dispatcher-config 交接物。）
        """
        return self._request(
            "GET", f"/engagements/{eid}/traffic/{tid}", params={"for_model": str(for_model).lower()}
        )

    # ------------------------------------------------------------------
    # progress（skeleton §2.5 / services.progress）
    # ------------------------------------------------------------------
    def open_task_run(
        self,
        eid: str,
        *,
        task_type: str,
        worker: str,
        project_id: Optional[str] = None,
        **extra: Any,
    ) -> dict:
        """开启一个 task_run。POST /engagements/{eid}/task_runs

        （services.progress.open_task_run(engagement_id, project_id, task_type, worker)；
        project_id 可空，verify/audit/replay 为 engagement 级。）
        """
        payload: dict[str, Any] = {"task_type": task_type, "worker": worker}
        if project_id is not None:
            payload["project_id"] = project_id
        payload.update(extra)
        return self._request("POST", f"/engagements/{eid}/task_runs", json=payload)

    def append_event(
        self,
        run_id: str,
        *,
        kind: str,
        level: str,
        message: str,
        raw_path: Optional[str] = None,
    ) -> dict:
        """追加 task 事件。POST /tasks/{run_id}/events"""
        payload: dict[str, Any] = {"kind": kind, "level": level, "message": message}
        if raw_path is not None:
            payload["raw_path"] = raw_path
        return self._request("POST", f"/tasks/{run_id}/events", json=payload)

    def finish_task_run(self, run_id: str, status: str, *, outcome_note: Optional[str] = None) -> dict:
        """结束 task_run（success/failed/cancelled...）。POST /tasks/{run_id}/finish"""
        payload: dict[str, Any] = {"status": status}
        if outcome_note is not None:
            payload["outcome_note"] = outcome_note
        return self._request("POST", f"/tasks/{run_id}/finish", json=payload)

    # ------------------------------------------------------------------
    # report / audit（skeleton §2.3/§2.6）
    # ------------------------------------------------------------------
    def trigger_audit(self, eid: str, item_id: str) -> dict:
        """触发/确认覆盖抽样复核。POST /engagements/{eid}/coverage/items/{cid}/audit"""
        return self._request("POST", f"/engagements/{eid}/coverage/items/{item_id}/audit", json={})

    def get_report(self, eid: str) -> dict:
        """获取最新报告。GET /engagements/{eid}/report"""
        return self._request("GET", f"/engagements/{eid}/report")

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------
    def health(self) -> dict:
        """连通性/健康检查。GET /health"""
        return self._request("GET", "/health")
