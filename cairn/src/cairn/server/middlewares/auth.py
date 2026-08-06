"""Bearer Token 鉴权中间件（v2 §6.3 / §7.3）。

- 读取 ``CAIRN_API_TOKEN``（或 ``ServerConfig.token``），缺/错 → 401
  ``AUTH_REQUIRED`` / ``AUTH_INVALID``；
- 健康检查豁免：``GET /health`` 与 ``GET /projects``（v2 文档「可豁免」选项）。
  注：``/projects`` 为 25-graph-subdomain 的占位豁免，业务路由接管后由编排者
  决定是否收窄（该豁免使无 token 也能列出项目）。
- D2：T/H 同一 Bearer Token，服务端不做调用方区分。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from ..errors import ErrorCode


def default_token_provider() -> str | None:
    return os.environ.get("CAIRN_API_TOKEN")


def default_exempt_paths(method: str, path: str) -> bool:
    """鉴权豁免路径（Agent 23 F8 扩展，2026-08-06）：

    - ``GET /health`` 与 ``GET /projects``：健康冒烟（10 原有）；
    - ``POST /engagements/{id}/traffic``：捕获代理受限写 token 端点（F8/C5）——主 token
      中间件放行，鉴权由 ``routers/traffic.py#require_capture_token`` 校验
      ``CAIRN_CAPTURE_TOKEN``（代理持受限写 token，非 Bearer 主 token）。精确匹配 4 段路径，
      不影响 ``/engagements/{id}/findings/{fid}/traffic``（仍走主 token）；
    - ``GET /tasks/{id}/events``：进度事件流端点（Agent 24 扩展）——EventSource 无法携带
      Authorization 头，SSE 用一次性 ticket 鉴权（``routers/progress.py#require_events_auth``
      手动校验 Bearer token 或 ticket）；JSON/长轮询模式同样走该手动鉴权。仅精确匹配 4 段
      路径，``POST .../events/ticket`` 与 ``GET .../events/{seq}/raw`` 仍走主 token。
    """
    if path == "/health":
        return True
    if method == "GET" and path == "/projects":
        return True
    if method == "POST":
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "engagements" and parts[3] == "traffic":
            return True
    if method == "GET":
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "tasks" and parts[3] == "events":
            return True
    return False


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value
    return None


class BearerAuthMiddleware:
    """纯 ASGI 中间件：在路由前校验 ``Authorization: Bearer <token>``。"""

    def __init__(
        self,
        app: ASGIApp,
        token_provider: Callable[[], str | None] | None = None,
        exempt: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.app = app
        self.token_provider = token_provider or default_token_provider
        self.exempt = exempt or default_exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        if self.exempt(method, path):
            await self.app(scope, receive, send)
            return

        expected = self.token_provider()
        if not expected:
            await self._reject(scope, send, ErrorCode.AUTH_REQUIRED, "缺少访问令牌：未配置 CAIRN_API_TOKEN")
            return

        auth = _header(scope, b"authorization")
        if auth is None or not auth.lower().startswith(b"bearer "):
            await self._reject(scope, send, ErrorCode.AUTH_REQUIRED, "缺少 Bearer Authorization 头")
            return

        token = auth.split(b" ", 1)[1].strip().decode("utf-8", "replace")
        if not token or token != expected:
            await self._reject(scope, send, ErrorCode.AUTH_INVALID, "访问令牌无效")
            return

        await self.app(scope, receive, send)

    async def _reject(
        self,
        scope: Scope,
        send: Send,
        error_code: ErrorCode,
        message: str,
        detail=None,
    ) -> None:
        body = json.dumps(
            {"error_code": error_code.value, "message": message, "detail": detail},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"www-authenticate", b"Bearer"),
        ]
        await send({"type": "http.response.start", "status": error_code.http_status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
