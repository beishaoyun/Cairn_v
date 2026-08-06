"""``cairn`` CLI 入口（Agent 10 所有；控制台入口 ``cairn = cairn.cli:main``）。

子命令：
- ``serve``    —— 启动 Cairn Server（FastAPI + uvicorn）；
- ``dispatch`` —— 占位：懒加载 ``cairn.dispatcher.cli.main_dispatch`` 并透传 argv，
                 由 13-dispatcher-runtime 实现完整逻辑（本文件不实现 dispatch 逻辑）。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace

from . import __version__
from .config import get_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairn",
        description="Cairn v2 授权渗透测试平台（Cairn Server + Dispatcher）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 Cairn Server")
    serve.add_argument("--host", default=os.environ.get("CAIRN_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("CAIRN_PORT", "8000")))
    serve.add_argument("--no-access-log", action="store_true", help="关闭访问日志")
    serve.add_argument("--db", default=None, help="SQLite 数据库路径（覆盖 CAIRN_DB_PATH）")
    serve.add_argument("--reload", action="store_true", help="开发热重载")

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回进程退出码（入口点包装为 ``sys.exit(main())``）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    # ``dispatch`` 子命令整体透传（argc/argv 原样转交 13 的 main_dispatch），
    # 不经过本 parser，避免 argparse 吞掉 dispatch 自身选项（如 --config）。
    if argv and argv[0] == "dispatch":
        return _dispatch(argv[1:])
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    return 2  # pragma: no cover


def _serve(args) -> int:
    import uvicorn

    from .server.app import create_app

    config = get_config()
    if args.db:
        config = replace(config, db_path=args.db)
    app = create_app(config)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=not args.no_access_log,
        reload=args.reload,
        log_level="info",
    )
    return 0


def _dispatch(argv: list[str]) -> int:
    """占位：懒加载 13 的 ``main_dispatch`` 并透传 argv。

    13 并行开发中（模块尚不可导入）时打印清晰提示并返回非 0。
    契约：``main_dispatch(argv: list[str] | None = None) -> int``（13 已按此实现）。
    """
    try:
        from cairn.dispatcher.cli import main_dispatch
    except ImportError as exc:
        print(
            "cairn dispatch: Dispatcher 未就绪（cairn.dispatcher.cli 不可导入：%s）。"
            "完整实现由 13-dispatcher-runtime 负责，见 dev-agents/13-dispatcher-runtime.md。"
            % exc,
            file=sys.stderr,
        )
        return 1
    return main_dispatch(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
