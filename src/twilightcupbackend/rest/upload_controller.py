"""静态资源上传控制器（管理员）：上传 logo 等到 MinIO，返回 key + 预签名 URL。"""

from __future__ import annotations

from classy_fastapi import Routable, get, post
from fastapi import Depends, HTTPException, UploadFile, status
from pydantic import BaseModel

from ..auth import get_current_account, require_admin
from ..controllers import DBController
from ..datatypes import Account
from ..storage import Storage

# 允许的图片 MIME 类型 + 扩展名
_ALLOWED = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_MAX_BYTES = 5 * 1024 * 1024  # 5MB


class UploadResult(BaseModel):
    key: str
    url: str | None


class UploadController(Routable):
    def __init__(self, db: DBController, storage: Storage) -> None:
        super().__init__(prefix="/admin/uploads", tags=["uploads"])
        self.db = db
        self.storage = storage

    @post(
        "",
        response_model=UploadResult,
        status_code=status.HTTP_201_CREATED,
        summary="上传静态资源（如 logo）",
        description="multipart 上传图片（png/jpg/webp/gif，≤5MB）到 MinIO，"
        "返回 object key（用于 Pick.logo）+ 短时效预签名 URL。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            400: {"description": "类型不支持 / 超过 5MB / 上传失败"},
        },
    )
    async def upload(
        self,
        file: UploadFile,
        _: Account = Depends(require_admin),
    ) -> UploadResult:
        ctype = (file.content_type or "").lower()
        suffix = _ALLOWED.get(ctype)
        if suffix is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的类型：{ctype}")
        data = await file.read()
        if len(data) > _MAX_BYTES:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件超过 5MB 限制")
        key = self.storage.gen_key("logos", suffix)
        try:
            self.storage.put(key, data, ctype)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"上传失败：{exc}"
            ) from exc
        return UploadResult(key=key, url=self.storage.presigned_url(key))

    @get(
        "/sign",
        response_model=UploadResult,
        summary="签发预签名 URL",
        description="按 object key 签发短时效 GET URL（前端 logo URL 过期后刷新用）。",
        responses={401: {"description": "未携带有效令牌"}},
    )
    def sign(
        self,
        key: str,
        _: Account = Depends(get_current_account),
    ) -> UploadResult:
        return UploadResult(key=key, url=self.storage.presigned_url(key))
