"""Cairn 全局错误码（对齐 architecture-research-report-pentest-v2.md §7.3）。

v2 §7.3 全部错误码均已覆盖；另增 ``INTERNAL`` 兜底 500（未在文档中，但「含」语义允许
扩展，用于统一错误响应不破坏 ``{"error_code","message","detail"}`` 形状）。
"""

from __future__ import annotations

import enum
from typing import Any


class ErrorCode(str, enum.Enum):
    """全局错误码。

    ``.value``      = error_code 字符串（响应体字段）
    ``.http_status`` = HTTP 状态码
    ``.message``     = 默认 message
    """

    def __new__(cls, code: str, http_status: int, message: str) -> "ErrorCode":
        obj = str.__new__(cls, code)
        obj._value_ = code
        obj.http_status = http_status
        obj.message = message
        return obj

    AUTH_REQUIRED = ("AUTH_REQUIRED", 401, "缺少访问令牌")
    AUTH_INVALID = ("AUTH_INVALID", 401, "访问令牌无效")
    SCOPE_DENIED = ("SCOPE_DENIED", 403, "目标不在授权范围")
    KILL_SWITCH_ON = ("KILL_SWITCH_ON", 423, "全局或项目熔断已开启")
    OUT_OF_AUTHORIZATION_WINDOW = ("OUT_OF_AUTHORIZATION_WINDOW", 403, "授权时间窗外")
    PROJECT_INACTIVE = ("PROJECT_INACTIVE", 403, "项目非 active 状态")
    ENGAGEMENT_INVALID_STATE = ("ENGAGEMENT_INVALID_STATE", 409, "Engagement 状态转换非法")
    LEASE_CONFLICT = ("LEASE_CONFLICT", 409, "租约被他人持有")
    FINDING_DUP = ("FINDING_DUP", 409, "漏洞去重冲突（已存在）")
    COVERAGE_DUP = ("COVERAGE_DUP", 409, "覆盖项重复（同 target+test_type）")
    COVERAGE_NOT_APPLICABLE = ("COVERAGE_NOT_APPLICABLE", 422, "intent 引用了非本 engagement 覆盖项")
    COVERAGE_ALREADY_COVERED = ("COVERAGE_ALREADY_COVERED", 409, "覆盖项已被测")
    COVERAGE_POLICY_UNMET = ("COVERAGE_POLICY_UNMET", 409, "finalize 时覆盖策略未达标")
    NOT_FOUND = ("NOT_FOUND", 404, "资源不存在")
    VALIDATION = ("VALIDATION", 422, "请求结构/语义校验失败")
    INTERNAL = ("INTERNAL", 500, "服务器内部错误")


class CairnError(Exception):
    """业务异常：路由层 raise，由 app.py 全局 handler 转成统一错误响应。"""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str | None = None,
        detail: Any = None,
    ) -> None:
        self.error_code = error_code
        self.message = message or error_code.message
        self.detail = detail
        super().__init__(self.message)


def error_payload(
    error_code: ErrorCode,
    message: str | None = None,
    detail: Any = None,
) -> dict:
    """统一错误响应体 ``{"error_code","message","detail"}``（v2 §7.2）。"""
    return {
        "error_code": error_code.value,
        "message": message or error_code.message,
        "detail": detail,
    }


def code_for_http_status(status: int) -> ErrorCode:
    """把裸 HTTP 状态码映射到默认错误码（供通用 HTTPException handler 使用）。"""
    if status == 400:
        return ErrorCode.VALIDATION
    if status == 401:
        return ErrorCode.AUTH_REQUIRED
    if status == 403:
        return ErrorCode.SCOPE_DENIED
    if status == 404:
        return ErrorCode.NOT_FOUND
    if status == 405:
        return ErrorCode.VALIDATION
    if status == 409:
        return ErrorCode.LEASE_CONFLICT
    if status == 422:
        return ErrorCode.VALIDATION
    if status == 423:
        return ErrorCode.KILL_SWITCH_ON
    if status >= 500:
        return ErrorCode.INTERNAL
    return ErrorCode.VALIDATION
