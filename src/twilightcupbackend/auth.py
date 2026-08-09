"""账号鉴权：口令哈希（stdlib pbkdf2）、JWT 签发/校验、FastAPI 依赖。

不引入 bcrypt 等原生依赖，口令哈希用标准库 ``hashlib.pbkdf2_hmac``。
令牌用 JWT（PyJWT），无状态，WS 与 REST 共用。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status

from .config import Settings
from .controllers import DBController
from .datatypes import Account, AccountType

_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERATIONS = 200_000


# ---------------------------------------------------------------------------
# 口令哈希
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """生成 ``pbkdf2_sha256$iterations$salt$hash`` 格式的口令哈希。"""
    salt = hashlib.sha256(_random_bytes(16)).hexdigest()
    dk = _pbkdf2(password, salt)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令是否与已存哈希匹配。"""
    try:
        algo, iters_str, salt, expected = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iters_str)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO, password.encode("utf-8"), bytes.fromhex(salt), iterations
    )
    return hmac.compare_digest(dk.hex(), expected)


def _pbkdf2(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO,
        password.encode("utf-8"),
        bytes.fromhex(salt),
        _PBKDF2_ITERATIONS,
    )
    return dk.hex()


def _random_bytes(n: int) -> bytes:
    # 用 hashlib 而非 secrets，避免在受限环境下的额外导入差异
    return hashlib.sha256(str(time.time_ns()).encode()).digest()[:n]


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def issue_token(account: Account, settings: Settings) -> str:
    """为账号签发 JWT。"""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": account.id,
        "type": int(account.primary_role),  # 主角色，兼容旧消费方
        "roles": [int(r) for r in account.roles],
        "name": account.display_name,
        "iat": now,
        "exp": now + settings.access_token_expire_seconds,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, settings: Settings) -> dict[str, Any]:
    """校验并解码 JWT，失败抛出 ``jwt.PyJWTError``。"""
    return jwt.decode(  # type: ignore[no-any-return]
        token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
    )


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------


def get_db(request: Request) -> DBController:
    """从 app.state 取数据库门面。"""
    return request.app.state.db  # type: ignore[no-any-return]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _extract_bearer(request: Request) -> str | None:
    auth = request.headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


async def get_current_account(
    request: Request,
    db: DBController = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Account:
    """解析 Bearer 令牌并返回当前账号；失败返回 401。"""
    token = _extract_bearer(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer 令牌")
    try:
        claims = decode_token(token, settings)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效或已过期") from exc
    sub = claims.get("sub")
    if not isinstance(sub, str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效")
    account = db.accounts.get(sub)
    if account is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在")
    return account


def require_role(*roles: AccountType):  # type: ignore[no-untyped-def]
    """返回一个依赖：校验当前账号类型在 ``roles`` 内，否则 403。"""

    async def _dep(
        account: Account = Depends(get_current_account),
    ) -> Account:
        if not set(account.roles) & set(roles):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")
        return account

    return _dep


# 预构建常用角色依赖，避免在默认参数中调用函数（ruff B008）
require_admin = require_role(AccountType.ADMIN)
require_official = require_role(AccountType.ADMIN, AccountType.REFEREE)
require_viewer = require_role(
    AccountType.ADMIN, AccountType.REFEREE, AccountType.DIRECTOR
)
