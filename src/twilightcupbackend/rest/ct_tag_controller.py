"""词条库管理控制器（管理员）：维护 CT 类别图池可选的全局词条。

词条库与 `Category.ct_tags` 的关系：
- 本控制器管理“词条库”本身（可新增/删除）；
- 图池编辑器在 CT 类别上选择当前图池支持的词条，引用这里的 `name`；
- 删除词条不会改动已引用它的图池/比赛快照（与关卡库同策略）。
"""

from __future__ import annotations

from classy_fastapi import Routable, delete, get, post
from fastapi import Depends, HTTPException, status
from pymongo.errors import DuplicateKeyError

from ..auth import require_admin
from ..controllers import DBController
from ..datatypes import Account, CtTag
from .schemas import CtTagCreate, CtTagOut


class CtTagController(Routable):
    def __init__(self, db: DBController) -> None:
        super().__init__(prefix="/admin/ct-tags", tags=["ct-tags"])
        self.db = db

    @post(
        "",
        response_model=CtTagOut,
        status_code=status.HTTP_201_CREATED,
        summary="创建词条",
        description="管理员新增一个可被图池 CT 类别选择的自定义词条。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            409: {"description": "词条已存在"},
        },
    )
    def create(
        self,
        body: CtTagCreate,
        _: Account = Depends(require_admin),
    ) -> CtTagOut:
        name = body.name.strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "词条不能为空")
        tag = CtTag(name=name)
        try:
            self.db.ct_tags.insert(tag)
        except DuplicateKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "词条已存在") from exc
        return CtTagOut.from_doc(tag)

    @get(
        "",
        response_model=list[CtTagOut],
        summary="词条列表",
        description="返回全部词条（按创建时间倒序）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
        },
    )
    def list(
        self,
        _: Account = Depends(require_admin),
    ) -> list[CtTagOut]:
        tags = self.db.ct_tags.find()
        tags.sort(key=lambda x: x.created_at, reverse=True)
        return [CtTagOut.from_doc(x) for x in tags]

    @delete(
        "/{tag_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="删除词条",
        description="删除词条库条目（不改动已引用它的图池/比赛；引用处名称仍可继续使用）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "词条不存在"},
        },
    )
    def remove(
        self,
        tag_id: str,
        _: Account = Depends(require_admin),
    ) -> None:
        if self.db.ct_tags.get(tag_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "词条不存在")
        self.db.ct_tags.delete(tag_id)
