"""鉴权控制器：登录签发令牌、查看当前账号。"""

from __future__ import annotations

from classy_fastapi import Routable, get, post
from fastapi import Depends, HTTPException, status

from ..auth import get_current_account, issue_token, verify_password
from ..config import Settings
from ..controllers import DBController
from ..datatypes import Account
from .schemas import LoginRequest, TokenResponse


class AuthController(Routable):
    def __init__(self, db: DBController, settings: Settings) -> None:
        super().__init__(prefix="/auth", tags=["auth"])
        self.db = db
        self.settings = settings

    @post(
        "/login",
        response_model=TokenResponse,
        summary="登录签发令牌",
        description="用用户名+口令换取 JWT 令牌，REST 与 WebSocket 均以该令牌鉴权。",
        responses={
            401: {"description": "用户名或密码错误"},
        },
    )
    def login(self, body: LoginRequest) -> TokenResponse:
        account = self.db.accounts.get_by_username(body.username)
        if account is None or not verify_password(body.password, account.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
        token = issue_token(account, self.settings)
        return TokenResponse(
            access_token=token,
            account_id=account.id,
            username=account.username,
            account_type=account.primary_role,
            roles=account.roles,
            display_name=account.display_name,
        )

    @get(
        "/me",
        response_model=TokenResponse,
        summary="查看当前账号",
        description="依据 Bearer 令牌返回当前账号信息（access_token 不回显）。",
        responses={401: {"description": "令牌无效或缺失"}},
    )
    def me(self, account: Account = Depends(get_current_account)) -> TokenResponse:
        return TokenResponse(
            access_token="",  # 不回显令牌
            account_id=account.id,
            username=account.username,
            account_type=account.primary_role,
            roles=account.roles,
            display_name=account.display_name,
        )
