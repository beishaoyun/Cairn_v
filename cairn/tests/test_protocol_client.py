"""12-dispatcher-config 验收测试：CairnClient（Bearer + 错误码解析 + 参数透传）。

覆盖 dev-agents/12-dispatcher-config.md §3.2：
- 用 FastAPI TestClient 起一个 stub server（只实现错误码/健康/回显）；
- 401 抛 AUTH（error_code=AUTH_INVALID）；业务 409 抛对应 error_code；
- Bearer 头正确传递；
- digest（for_model）与 export（format=yaml）参数透传。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from cairn.dispatcher.errors import AuthError, CairnClientError, ScopeDeniedError
from cairn.dispatcher.protocol.client import CairnClient

TOKEN = "test-token"
EXPECTED_AUTH = f"Bearer {TOKEN}"


def build_stub() -> FastAPI:
    """stub server：健康 / 回显 / 401 AUTH / 409 业务错误 / 403 SCOPE_DENIED。"""
    app = FastAPI()

    def _auth_ok(authorization: str) -> bool:
        return authorization == EXPECTED_AUTH

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/engagements/{eid}")
    def get_engagement(eid: str, authorization: str = Header(default="")):
        if not _auth_ok(authorization):
            return JSONResponse(
                status_code=401,
                content={"error_code": "AUTH_INVALID", "message": "bad token", "detail": None},
            )
        return {"id": eid, "status": "active"}

    @app.put("/engagements/{eid}/status")
    def put_status(eid: str, authorization: str = Header(default="")):
        if not _auth_ok(authorization):
            return JSONResponse(
                status_code=401,
                content={"error_code": "AUTH_REQUIRED", "message": "missing token", "detail": None},
            )
        # 业务 409：非法的 engagement 状态转换
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "ENGAGEMENT_INVALID_STATE",
                "message": "illegal transition active->completed",
                "detail": {"from": "active", "to": "completed"},
            },
        )

    @app.post("/projects/{pid}/intents/{iid}/claim")
    def claim_intent(pid: str, iid: str, authorization: str = Header(default="")):
        if not _auth_ok(authorization):
            return JSONResponse(
                status_code=401,
                content={"error_code": "AUTH_INVALID", "message": "bad token", "detail": None},
            )
        return JSONResponse(
            status_code=409,
            content={"error_code": "LEASE_CONFLICT", "message": "held by another worker", "detail": None},
        )

    @app.get("/engagements/{eid}/scope/check")
    def scope_check(eid: str, value: str, authorization: str = Header(default="")):
        if not _auth_ok(authorization):
            return JSONResponse(
                status_code=401,
                content={"error_code": "AUTH_INVALID", "message": "bad token", "detail": None},
            )
        # value 含 "forbidden" → 403 SCOPE_DENIED
        if "forbidden" in value:
            return JSONResponse(
                status_code=403,
                content={"error_code": "SCOPE_DENIED", "message": f"not in scope: {value}", "detail": None},
            )
        return {"allowed": True, "target": {"value": value}}

    @app.get("/engagements/{eid}/traffic/{tid}")
    def resolve_traffic(eid: str, tid: str, for_model: str = "false"):
        # 回显：用于断言 for_model 参数透传
        return {"traffic_id": tid, "for_model": for_model}

    @app.get("/projects/{pid}/export")
    def export_graph(pid: str, format: str = "yaml"):
        # 回显：用于断言 format=yaml 参数透传
        return PlainTextResponse(f"format={format}")

    return app


@pytest.fixture
def client() -> CairnClient:
    with TestClient(build_stub()) as tc:
        yield CairnClient("http://testserver", TOKEN, client=tc)


def test_health(client: CairnClient):
    assert client.health() == {"status": "ok"}


def test_bearer_header_sent_and_valid_auth_ok(client: CairnClient):
    data = client.get("e-001")
    assert data == {"id": "e-001", "status": "active"}


def test_401_raises_auth_error():
    with TestClient(build_stub()) as tc:
        bad = CairnClient("http://testserver", "wrong-token", client=tc)
        with pytest.raises(AuthError) as exc:
            bad.get("e-001")
        assert exc.value.error_code == "AUTH_INVALID"
        assert exc.value.http_status == 401


def test_401_with_wrong_token_not_auth_ok():
    with TestClient(build_stub()) as tc:
        bad = CairnClient("http://testserver", "wrong-token", client=tc)
        with pytest.raises(CairnClientError) as exc:
            bad.get("e-001")
        assert exc.value.error_code in ("AUTH_REQUIRED", "AUTH_INVALID")
        assert exc.value.http_status == 401


def test_business_409_raises_corresponding_error_code(client: CairnClient):
    with pytest.raises(CairnClientError) as exc:
        client.set_status("e-001", "completed")
    assert exc.value.error_code == "ENGAGEMENT_INVALID_STATE"
    assert exc.value.http_status == 409
    assert exc.value.detail == {"from": "active", "to": "completed"}


def test_lease_conflict_409(client: CairnClient):
    with pytest.raises(CairnClientError) as exc:
        client.claim_intent("p-1", "i-1", worker="w1")
    assert exc.value.error_code == "LEASE_CONFLICT"


def test_scope_denied_403(client: CairnClient):
    with pytest.raises(ScopeDeniedError) as exc:
        client.check_scope("e-001", "forbidden.example.com")
    assert exc.value.error_code == "SCOPE_DENIED"
    assert exc.value.http_status == 403


def test_digest_param_passthrough(client: CairnClient):
    # 默认 for_model=True → 发送 for_model=true
    data = client.resolve_traffic("e-001", "t-001")
    assert data["for_model"] == "true"
    data = client.resolve_traffic("e-001", "t-001", for_model=False)
    assert data["for_model"] == "false"


def test_export_param_passthrough(client: CairnClient):
    text = client.export_yaml("p-1")
    assert text == "format=yaml"
