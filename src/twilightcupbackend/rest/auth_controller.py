"""鉴权控制器：登录签发令牌、查看当前账号。"""

from __future__ import annotations

from logging import Logger, getLogger

from classy_fastapi import Routable, get, post
from fastapi import Depends, Request, status

from ..auth import get_current_account, issue_token, verify_password
from ..config import Settings
from ..controllers import DBController
from ..datatypes import Account, AccountType
from ..errors import CodedHTTPException
from .schemas import LoginEndpoint, LoginRequest, TokenResponse

logger: Logger = getLogger(__name__)

# 登录端 → 须具备的账号角色
_ENDPOINT_ROLES: dict[LoginEndpoint, AccountType] = {
    "admin": AccountType.ADMIN,
    "referee": AccountType.REFEREE,
    "director": AccountType.DIRECTOR,
    "player": AccountType.PLAYER,
}


def _client_ip(request: Request) -> str:
    """取来源 IP（优先反向代理透传的 X-Forwarded-For 首值）。"""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class AuthController(Routable):
    def __init__(self, db: DBController, settings: Settings) -> None:
        super().__init__(prefix="/auth", tags=["auth"])
        self.db = db
        self.settings = settings

    @post(
        "/login",
        response_model=TokenResponse,
        summary="登录签发令牌",
        description="用用户名+口令换取 JWT 令牌，REST 与 WebSocket 均以该令牌鉴权。"
        "可选 endpoint 指定登录端（admin/referee/director/player）：账号无对应"
        "角色则 403 ENDPOINT_FORBIDDEN 且不签发令牌（防蹭登录，已持有令牌不受"
        "影响）；不传时行为与旧版完全一致。失败错误体带稳定 code 字段："
        "INVALID_CREDENTIALS（账密错误）/ ENDPOINT_FORBIDDEN（无该端权限）。",
        responses={
            401: {"description": "用户名或密码错误（INVALID_CREDENTIALS）"},
            403: {"description": "无该端权限（ENDPOINT_FORBIDDEN）"},
        },
    )
    def login(self, body: LoginRequest, request: Request) -> TokenResponse:
        # 先验口令再验端权限：未通过身份验证前不泄露账号角色信息
        account = self.db.accounts.get_by_username(body.username)
        if account is None or not verify_password(body.password, account.password_hash):
            raise CodedHTTPException(
                status.HTTP_401_UNAUTHORIZED, "用户名或密码错误", "INVALID_CREDENTIALS"
            )
        if body.endpoint is not None and _ENDPOINT_ROLES[body.endpoint] not in (
            account.roles
        ):
            logger.warning(
                "登录被拒（无端权限）：账号=%s endpoint=%s 来源IP=%s",
                body.username,
                body.endpoint,
                _client_ip(request),
            )
            raise CodedHTTPException(
                status.HTTP_403_FORBIDDEN,
                f"该账号无 {body.endpoint} 端登录权限",
                "ENDPOINT_FORBIDDEN",
            )
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
