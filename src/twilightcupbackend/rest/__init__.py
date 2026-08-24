"""REST 控制器注册入口。"""

from __future__ import annotations

from fastapi import FastAPI

from ..config import Settings
from ..controllers import DBController
from .account_controller import AccountController
from .auth_controller import AuthController
from .level_controller import LevelController
from .log_controller import LogController
from .mappool_controller import MappoolController
from .match_controller import MatchController
from .me_controller import MeController
from .speedrun_proxy import SpeedrunProxyController
from .tournament_controller import TournamentController
from .upload_controller import UploadController

__all__ = ["register_routes"]


def register_routes(app: FastAPI, db: DBController, settings: Settings) -> None:
    """实例化各 classy-fastapi 控制器并挂载其 router。"""
    cm = getattr(app.state, "connection_manager", None)
    storage = getattr(app.state, "storage", None)
    app.include_router(AuthController(db, settings).router)
    app.include_router(AccountController(db).router)
    app.include_router(MatchController(db, cm, storage).router)
    app.include_router(MappoolController(db, storage).router)
    app.include_router(LevelController(db, storage).router)
    app.include_router(TournamentController(db, cm, storage).router)
    app.include_router(MeController(db, cm, storage).router)
    app.include_router(LogController(db).router)
    app.include_router(SpeedrunProxyController().router)
    if storage is not None:
        app.include_router(UploadController(db, storage).router)
