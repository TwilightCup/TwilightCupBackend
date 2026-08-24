"""账号管理控制器（管理员）：创建/查询/修改/删除账号。"""

from __future__ import annotations

from classy_fastapi import Routable, delete, get, patch, post
from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from ..auth import hash_password, require_admin
from ..controllers import DBController
from ..datatypes import Account, AccountType
from .schemas import AccountCreate, AccountOut


class AccountUpdate(BaseModel):
    display_name: str | None = None
    password: str | None = None
    roles: list[AccountType] | None = None
    # speedrun.com 绑定；空串 = 解绑（传 None = 不改）
    speedrun_id: str | None = None


def _normalize_roles(roles: list[AccountType]) -> list[AccountType]:
    """去重保序；管理员默认兼裁判 + 导播。"""
    result = list(dict.fromkeys(roles))
    if AccountType.ADMIN in result:
        for r in (AccountType.REFEREE, AccountType.DIRECTOR):
            if r not in result:
                result.append(r)
    return result


class AccountController(Routable):
    def __init__(self, db: DBController) -> None:
        super().__init__(prefix="/admin/accounts", tags=["accounts"])
        self.db = db

    @post(
        "",
        response_model=AccountOut,
        status_code=status.HTTP_201_CREATED,
        summary="创建账号",
        description="管理员预创建选手/裁判/导播/管理员账号并分发。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            409: {"description": "用户名已存在"},
        },
    )
    def create(
        self,
        body: AccountCreate,
        _: Account = Depends(require_admin),
    ) -> AccountOut:
        account = Account(
            username=body.username,
            password_hash=hash_password(body.password),
            roles=_normalize_roles(body.roles),
            display_name=body.display_name,
            speedrun_id=body.speedrun_id.strip() or None if body.speedrun_id else None,
        )
        try:
            self.db.accounts.insert(account)
        except DuplicateKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在") from exc
        return AccountOut.from_account(account)

    @get(
        "",
        response_model=list[AccountOut],
        summary="账号列表",
        description="返回全部账号（不含口令哈希）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
        },
    )
    def list(
        self,
        _: Account = Depends(require_admin),
    ) -> list[AccountOut]:
        return [AccountOut.from_account(a) for a in self.db.accounts.find()]

    @get(
        "/{account_id}",
        response_model=AccountOut,
        summary="查询单个账号",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "账号不存在"},
        },
    )
    def get_one(
        self,
        account_id: str,
        _: Account = Depends(require_admin),
    ) -> AccountOut:
        account = self.db.accounts.get(account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
        return AccountOut.from_account(account)

    @patch(
        "/{account_id}",
        response_model=AccountOut,
        summary="修改账号",
        description="按字段局部更新（display_name / password / type，传哪个改哪个）。",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "账号不存在"},
            409: {"description": "用户名冲突"},
        },
    )
    def update(
        self,
        account_id: str,
        body: AccountUpdate,
        _: Account = Depends(require_admin),
    ) -> AccountOut:
        account = self.db.accounts.get(account_id)
        if account is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
        if body.display_name is not None:
            account.display_name = body.display_name
        if body.password is not None:
            account.password_hash = hash_password(body.password)
        if body.roles is not None:
            account.roles = _normalize_roles(body.roles)
        if body.speedrun_id is not None:
            account.speedrun_id = body.speedrun_id.strip() or None
        try:
            self.db.accounts.replace(account)
        except DuplicateKeyError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在") from exc
        return AccountOut.from_account(account)

    @delete(
        "/{account_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="删除账号",
        responses={
            401: {"description": "未携带有效令牌"},
            403: {"description": "需要管理员权限"},
            404: {"description": "账号不存在"},
        },
    )
    def remove(
        self,
        account_id: str,
        _: Account = Depends(require_admin),
    ) -> None:
        if self.db.accounts.get(account_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "账号不存在")
        self.db.accounts.delete(account_id)
