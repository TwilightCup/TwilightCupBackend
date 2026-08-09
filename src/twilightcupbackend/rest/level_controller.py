"""关卡库管理控制器（管理员）：创建/查询/修改/删除可复用关卡。"""

from __future__ import annotations

from classy_fastapi import Routable, delete, get, patch, post
from fastapi import Depends, HTTPException, status
from pymongo.errors import DuplicateKeyError

from ..auth import require_admin
from ..controllers import DBController
from ..datatypes import Account, Level
from ..storage import Storage
from .schemas import LevelCreate, LevelOut, LevelUpdate


class LevelController(Routable):
    def __init__(self, db: DBController, storage: Storage | None = None) -> None:
        super().__init__(prefix="/admin/levels", tags=["levels"])
        self.db = db
        self.storage = storage

    @post(
        "",
        response_model=LevelOut,
        status_code=status.HTTP_201_CREATED,
        summary="创建关卡",
        description="管理员创建关卡（关卡库）；图池选图的 collection.raw 按 id 引用。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            409: {"description": "关卡名已存在"},
        },
    )
    def create(
        self,
        body: LevelCreate,
        _: Account = Depends(require_admin),
    ) -> LevelOut:
        level = Level(
            name=body.name,
            display_name=body.display_name or body.name,
            logo=body.logo,
        )
        try:
            self.db.levels.insert(level)
        except DuplicateKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "关卡名已存在") from exc
        return LevelOut.from_level(level, self.storage)

    @get(
        "",
        response_model=list[LevelOut],
        summary="关卡列表",
        description="返回全部关卡（按创建时间倒序）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
        },
    )
    def list(
        self,
        _: Account = Depends(require_admin),
    ) -> list[LevelOut]:
        levels = self.db.levels.find()
        levels.sort(key=lambda x: x.created_at, reverse=True)
        return [LevelOut.from_level(x, self.storage) for x in levels]

    @get(
        "/{level_id}",
        response_model=LevelOut,
        summary="查询单个关卡",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "关卡不存在"},
        },
    )
    def get_one(
        self,
        level_id: str,
        _: Account = Depends(require_admin),
    ) -> LevelOut:
        level = self.db.levels.get(level_id)
        if level is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关卡不存在")
        return LevelOut.from_level(level, self.storage)

    @patch(
        "/{level_id}",
        response_model=LevelOut,
        summary="修改关卡",
        description="按字段局部更新（display_name / logo）；name 不可改。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "关卡不存在"},
        },
    )
    def update(
        self,
        level_id: str,
        body: LevelUpdate,
        _: Account = Depends(require_admin),
    ) -> LevelOut:
        level = self.db.levels.get(level_id)
        if level is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关卡不存在")
        if body.display_name is not None:
            level.display_name = body.display_name
        if body.logo is not None:
            level.logo = body.logo
        self.db.levels.replace(level)
        return LevelOut.from_level(level, self.storage)

    @delete(
        "/{level_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="删除关卡",
        description="删除关卡库条目（不改动已引用它的图池/比赛数据；引用处下发时名字原样保留）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "关卡不存在"},
        },
    )
    def remove(
        self,
        level_id: str,
        _: Account = Depends(require_admin),
    ) -> None:
        if self.db.levels.get(level_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "关卡不存在")
        self.db.levels.delete(level_id)
