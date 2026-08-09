"""图池库管理控制器（管理员）：创建/查询/修改/删除可复用图池。"""

from __future__ import annotations

from classy_fastapi import Routable, delete, get, patch, post
from fastapi import Depends, HTTPException, status
from pymongo.errors import DuplicateKeyError

from ..auth import require_admin
from ..controllers import DBController
from ..datatypes import Account, MappoolDoc
from ..storage import Storage
from .schemas import MappoolCreate, MappoolOut, MappoolUpdate


class MappoolController(Routable):
    def __init__(self, db: DBController, storage: Storage | None = None) -> None:
        super().__init__(prefix="/admin/mappools", tags=["mappools"])
        self.db = db
        self.storage = storage

    @post(
        "",
        response_model=MappoolOut,
        status_code=status.HTTP_201_CREATED,
        summary="创建图池",
        description="管理员创建一个可复用图池（入库），创建比赛时按 id 引用。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            409: {"description": "图池名已存在"},
        },
    )
    def create(
        self,
        body: MappoolCreate,
        admin: Account = Depends(require_admin),
    ) -> MappoolOut:
        doc = MappoolDoc(name=body.name, mappool=body.mappool, created_by=admin.id)
        try:
            self.db.mappools.insert(doc)
        except DuplicateKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "图池名已存在") from exc
        return MappoolOut.from_doc(doc, self.storage)

    @get(
        "",
        response_model=list[MappoolOut],
        summary="图池列表",
        description="返回全部图池库（按创建时间倒序）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
        },
    )
    def list(
        self,
        _: Account = Depends(require_admin),
    ) -> list[MappoolOut]:
        docs = self.db.mappools.find()
        docs.sort(key=lambda d: d.created_at, reverse=True)
        return [MappoolOut.from_doc(d, self.storage) for d in docs]

    @get(
        "/{mappool_id}",
        response_model=MappoolOut,
        summary="查询单个图池",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "图池不存在"},
        },
    )
    def get_one(
        self,
        mappool_id: str,
        _: Account = Depends(require_admin),
    ) -> MappoolOut:
        doc = self.db.mappools.get(mappool_id)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "图池不存在")
        return MappoolOut.from_doc(doc, self.storage)

    @patch(
        "/{mappool_id}",
        response_model=MappoolOut,
        summary="修改图池",
        description="按字段局部更新（name / mappool，传哪个改哪个）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "图池不存在"},
            409: {"description": "图池名冲突"},
        },
    )
    def update(
        self,
        mappool_id: str,
        body: MappoolUpdate,
        _: Account = Depends(require_admin),
    ) -> MappoolOut:
        doc = self.db.mappools.get(mappool_id)
        if doc is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "图池不存在")
        if body.name is not None:
            doc.name = body.name
        if body.mappool is not None:
            doc.mappool = body.mappool
        try:
            self.db.mappools.replace(doc)
        except DuplicateKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "图池名已存在") from exc
        return MappoolOut.from_doc(doc, self.storage)

    @delete(
        "/{mappool_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="删除图池",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "图池不存在"},
        },
    )
    def remove(
        self,
        mappool_id: str,
        _: Account = Depends(require_admin),
    ) -> None:
        if self.db.mappools.get(mappool_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "图池不存在")
        self.db.mappools.delete(mappool_id)
