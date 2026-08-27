"""FastAPI 应用入口：构建 app、配置 lifespan、挂载路由、uvicorn 启动。

参考 AShareGateway main.py 的 lifespan + 模块级 app 风格。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .commands import CommandHandler
from .config import settings
from .connection_manager import ConnectionManager
from .controllers import DBController
from .i18n import catalog
from .logging_setup import configure_logging
from .match_fsm import MatchEngine
from .rest import register_routes
from .storage import Storage
from .stores import MatchRegistry
from .tournament_engine import TournamentEngine
from .ws import register_ws

logger = configure_logging(settings)

_TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "健康检查"},
    {"name": "auth", "description": "账号鉴权：登录签发令牌、查看当前账号"},
    {
        "name": "accounts",
        "description": "账号管理（管理员）：账号的创建/查询/修改/删除",
    },
    {
        "name": "matches",
        "description": "比赛比赛管理（管理员）：创建/查询比赛、配置赛制与图池",
    },
    {
        "name": "logs",
        "description": "日志查询（管理员/裁判/导播）：比赛日志、聊天日志、回合明细",
    },
    {
        "name": "bilibili",
        "description": "B站直播流同源代理（导播选手画面）：以 B站要求的请求头拉取 "
        "HTTP-FLV 并转发给前端 mpegts.js",
    },
    {
        "name": "speedrun",
        "description": "speedrun.com 只读代理（裁判/导播/管理员）：图池映射选择器与"
        "项目信息场景的选项/榜单数据源；响应持久化到 Mongo（按速通项目区分），"
        "支持 auto/cached/refresh 三模式（前端 SWR）",
    },
]


def create_app(db: DBController | None = None) -> FastAPI:
    """构建 FastAPI 应用。

    传入 ``db``（如 mongomock 支撑的控制器）用于测试；否则连真实 Mongo。
    """
    ctl = db if db is not None else DBController(settings)
    own_db = db is None  # 是否由本次创建（决定是否在关闭时释放）
    registry = MatchRegistry()
    storage = Storage(settings)
    connection_manager = ConnectionManager(ctl, registry, settings)
    connection_manager.command_router = CommandHandler(connection_manager)
    # storage 传入引擎：WS 下发的回合 pick 签展示图公开 URL（REST 输出层同口径）
    connection_manager.match_engine = MatchEngine(connection_manager, ctl, storage)
    connection_manager.tournament_engine = TournamentEngine(ctl, connection_manager)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("TwilightCup backend starting (v%s).", __version__)
        catalog.load_dir(Path(settings.locales_dir), settings.default_locale)
        if own_db:
            try:
                ctl.ensure_indexes()
            except Exception:
                logger.warning(
                    "ensure_indexes 失败（Mongo 可能未就绪）。", exc_info=True
                )
            try:
                ctl.ensure_default_tournament()
            except Exception:
                logger.warning(
                    "默认赛事 seed 失败（Mongo 可能未就绪）。", exc_info=True
                )
            try:
                storage.ensure_bucket()
            except Exception:
                logger.warning(
                    "对象存储 ensure_bucket 失败（MinIO 可能未就绪）。",
                    exc_info=True,
                )
        app.state.db = ctl
        app.state.settings = settings
        app.state.registry = registry
        app.state.connection_manager = connection_manager
        app.state.storage = storage
        yield
        if own_db:
            ctl.close()
        logger.info("TwilightCup backend shutting down.")

    app = FastAPI(
        title="TwilightCup Backend",
        version=__version__,
        description="黄昏杯（Twilight Cup）《人类一败涂地》1v1 速通比赛中转服务端。"
        "REST 接口用于账号/比赛管理与日志查询；选手·裁判·导播三端的实时通信"
        "走 WebSocket（协议见 docs/ws-protocol.md）。",
        lifespan=lifespan,
        openapi_tags=_TAGS,
    )
    app.state.db = ctl
    app.state.settings = settings
    app.state.registry = registry
    app.state.connection_manager = connection_manager
    app.state.storage = storage

    # 开发期允许前端跨域调用 REST（生产环境应收敛 allow_origins）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["health"], summary="健康检查")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        # 错误体约定 {"msg": ...}：前端 extractMsg 读 msg 最稳（亦兼容旧 detail）。
        # CodedHTTPException 额外携带稳定字符串 code（如登录的
        # ENDPOINT_FORBIDDEN），前端可按 code 分支/i18n；普通异常无 code 字段。
        detail = exc.detail
        msg = detail if isinstance(detail, str) else "请求有误"
        body: dict[str, str] = {"msg": msg}
        code = getattr(exc, "code", None)
        if isinstance(code, str):
            body["code"] = code
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常 (%s %s)", request.method, request.url.path)
        return JSONResponse(
            status_code=500, content={"code": 500, "msg": "内部服务器错误"}
        )

    register_routes(app, ctl, settings)
    register_ws(app)
    return app


app = create_app()


def main() -> None:
    """uvicorn 启动入口（macOS/Linux 自动使用 uvloop）。"""
    uvicorn.run(
        "twilightcupbackend.main:app",
        host=settings.host,
        port=settings.port,
        loop="uvloop",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
