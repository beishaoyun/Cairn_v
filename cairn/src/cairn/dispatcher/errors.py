"""派发侧错误码 —— 映射 Cairn Server 端 error_code（architecture v2 §7.3）。

CairnClient 对所有非 2xx 响应抛 ``CairnClientError``，其 ``error_code`` 与
服务端返回的 ``{"error_code": ..., "message": ..., "detail": ...}`` 顶层字段一致。
"""

from __future__ import annotations

from typing import Any

import httpx

# ---- 服务端 error_code 常量（v2 §7.3）----
AUTH_REQUIRED = "AUTH_REQUIRED"
AUTH_INVALID = "AUTH_INVALID"
SCOPE_DENIED = "SCOPE_DENIED"
KILL_SWITCH_ON = "KILL_SWITCH_ON"
OUT_OF_AUTHORIZATION_WINDOW = "OUT_OF_AUTHORIZATION_WINDOW"
PROJECT_INACTIVE = "PROJECT_INACTIVE"
ENGAGEMENT_INVALID_STATE = "ENGAGEMENT_INVALID_STATE"
LEASE_CONFLICT = "LEASE_CONFLICT"
FINDING_DUP = "FINDING_DUP"
COVERAGE_DUP = "COVERAGE_DUP"
COVERAGE_NOT_APPLICABLE = "COVERAGE_NOT_APPLICABLE"
COVERAGE_ALREADY_COVERED = "COVERAGE_ALREADY_COVERED"
COVERAGE_POLICY_UNMET = "COVERAGE_POLICY_UNMET"
NOT_FOUND = "NOT_FOUND"
VALIDATION = "VALIDATION"

#: HTTP 状态码 → 无错误体/无法解析时的兜底 error_code（v2 §7.3 对应关系）
STATUS_FALLBACK_CODES: dict[int, str] = {
    401: AUTH_REQUIRED,
    403: SCOPE_DENIED,
    404: NOT_FOUND,
    409: LEASE_CONFLICT,
    422: VALIDATION,
    423: KILL_SWITCH_ON,
}


class CairnClientError(Exception):
    """非 2xx 服务端响应对应的派发侧异常。

    Attributes:
        error_code:  服务端 error_code（如 ``SCOPE_DENIED``、``LEASE_CONFLICT``）。
        http_status: 服务端 HTTP 状态码。
        detail:      服务端 ``detail`` 字段（可空）。
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        http_status: int = 0,
        detail: Any = None,
    ) -> None:
        self.error_code = error_code
        self.http_status = http_status
        self.detail = detail
        super().__init__(f"[{error_code}] {message}")


class AuthError(CairnClientError):
    """401：缺/错 token（AUTH_REQUIRED / AUTH_INVALID）。"""


class ScopeDeniedError(CairnClientError):
    """403：目标不在授权范围（SCOPE_DENIED）。"""


def raise_for_error(resp: httpx.Response) -> "None":
    """按 v2 §7.2/§7.3 解析非 2xx 响应并抛出对应 CairnClientError。

    - 顶层 ``error_code`` 优先；无法解析时按 HTTP 状态码兜底。
    - ``AUTH_*`` / ``SCOPE_DENIED`` 分别提升为语义子类，便于派发逻辑按类型处理。
    """
    body: Any = None
    try:
        body = resp.json()
    except Exception:
        body = None

    error_code = body.get("error_code") if isinstance(body, dict) else None
    message = body.get("message") if isinstance(body, dict) else None
    detail = body.get("detail") if isinstance(body, dict) else None

    if not error_code:
        error_code = STATUS_FALLBACK_CODES.get(resp.status_code, f"HTTP_{resp.status_code}")

    message = message or resp.text[:500]
    cls = CairnClientError
    if error_code in (AUTH_REQUIRED, AUTH_INVALID):
        cls = AuthError
    elif error_code == SCOPE_DENIED:
        cls = ScopeDeniedError
    raise cls(error_code=error_code, message=message, http_status=resp.status_code, detail=detail)
