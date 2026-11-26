"""带稳定错误码的 HTTP 异常。

错误体全局约定为 ``{"msg": 人话}``（见 main.py 的 HTTPException handler）；
本模块的异常在此基础上追加稳定字符串 ``code`` 字段，供前端按码分支/i18n
（如登录的 ``ENDPOINT_FORBIDDEN`` / ``INVALID_CREDENTIALS``）。
"""

from __future__ import annotations

from fastapi import HTTPException


class CodedHTTPException(HTTPException):
    """携带稳定字符串错误码的 HTTPException。

    错误体输出 ``{"msg": ..., "code": ...}``；普通 HTTPException（无 code）
    仍输出 ``{"msg": ...}``，对旧客户端完全向后兼容。
    """

    def __init__(self, status_code: int, msg: str, code: str) -> None:
        super().__init__(status_code=status_code, detail=msg)
        self.code = code
