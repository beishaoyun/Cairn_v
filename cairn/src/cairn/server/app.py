"""FastAPI 装配：全局异常 handler + Bearer 鉴权中间件 + 业务路由注册点 + 静态/前端托管。

装配顺序（创建时执行）：
1. ``init_db``（幂等建库 + v1→v2 迁移）；
2. 挂 BearerAuthMiddleware（AUTH_REQUIRED/AUTH_INVALID，/health 与 GET /projects 豁免）；
3. 注册全局异常 handler（统一 ``{"error_code","message","detail"}``，422 包 VALIDATION）；
4. 挂 settings 路由（skeleton §2.1）；
5. ``register_business_routers`` —— **业务路由注册点**：20-25 等 Agent 各自新增
   ``cairn/server/routers/<模块>.py`` 并暴露 ``router`` 属性，本函数自动发现挂载；
6. 健康检查 ``/health`` 与 ``/projects`` 占位（25-graph-subdomain 接管后替换）；
7. 前端静态目录（Vite dist 存在 index.html 才挂载，避免空目录吞 API 404）。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from cairn import __version__

from ..config import ServerConfig, get_config
from .db import init_db
from .errors import CairnError, ErrorCode, code_for_http_status, error_payload
from .middlewares.auth import BearerAuthMiddleware, default_exempt_paths
from .routers import settings as settings_router

logger = logging.getLogger("cairn.server.app")


# ---------------------------------------------------------------------------
# 全局异常 handler（统一错误响应 v2 §7.2/§7.3）
# ---------------------------------------------------------------------------


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        # 422 校验错误包 error_code=VALIDATION，但保留 FastAPI detail
        return JSONResponse(
            status_code=422,
            content=error_payload(ErrorCode.VALIDATION, detail=exc.errors()),
        )

    @app.exception_handler(CairnError)
    async def _cairn_handler(request: Request, exc: CairnError):
        return JSONResponse(
            status_code=exc.error_code.http_status,
            content=error_payload(exc.error_code, exc.message, exc.detail),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException):
        code = code_for_http_status(exc.status_code)
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(code, detail=exc.detail),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception):
        logger.exception(
            "unhandled exception: %s %s", request.method, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(ErrorCode.INTERNAL, detail=str(exc)),
        )


# ---------------------------------------------------------------------------
# 业务路由注册点
# ---------------------------------------------------------------------------

#: 期望由业务 Agent 提供的 router 模块名（仅文档用途，自动发现不依赖本列表）
BUSINESS_ROUTER_MODULES = (
    "engagements", "targets", "projects", "intents", "hints", "findings",
    "coverage", "report", "export", "capture", "progress", "timeline", "replay",
)


def register_business_routers(app: FastAPI) -> None:
    """自动发现并挂载 ``cairn.server.routers`` 下各业务模块的 ``router``。

    下游 Agent（20-25 等）新建 ``cairn/server/routers/<模块>.py``，模块内定义并导出
    ``router = APIRouter(...)`` 即可，无需修改本文件（避免 app.py 写冲突）。
    模块尚不存在（并行开发中）或导入失败时跳过并告警。
    """
    pkg = importlib.import_module("cairn.server.routers")
    for mod in pkgutil.iter_modules(pkg.__path__):
        name = mod.name
        if name == "settings":
            continue  # 已由 create_app 显式挂载
        try:
            module = importlib.import_module(f"cairn.server.routers.{name}")
        except Exception as exc:  # noqa: BLE001 —— 并行开发：下游路由未就绪时跳过
            logger.warning("跳过业务路由模块 %s：%s", name, exc)
            continue
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)
            logger.info("挂载业务路由：%s", name)


# ---------------------------------------------------------------------------
# 静态 / 前端托管
# ---------------------------------------------------------------------------


def _mount_static(app: FastAPI) -> None:
    static_dir = Path(__file__).parent / "static"
    # 阶段 3 前端产物（Vite dist）：存在 index.html 才挂载，空目录/占位文件不吞 API 404
    if static_dir.is_dir() and (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="frontend")


# ---------------------------------------------------------------------------
# App 工厂
# ---------------------------------------------------------------------------


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """构建 Cairn Server 应用。``config`` 缺省时从环境变量读取。"""
    config = config or get_config()
    init_db(config.db_path)  # 建库 + 迁移（幂等；serve 启动即就绪）

    app = FastAPI(
        title="Cairn Server",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.config = config

    app.add_middleware(
        BearerAuthMiddleware,
        token_provider=config.token,
        exempt=default_exempt_paths,
    )
    _register_exception_handlers(app)

    app.include_router(settings_router.router)
    register_business_routers(app)

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/projects", tags=["placeholder"])
    def projects_placeholder(engagement_id: str | None = None):
        """占位端点：由 25-graph-subdomain 接管（skeleton §2.4）。

        此处返回空列表作为兜底；auth 中间件**不再豁免** GET /projects（P1-4 收窄，
        50 审计）——无 token 访问返回 401，不得枚举项目元数据。
        """
        return []

    _mount_static(app)
    return app
