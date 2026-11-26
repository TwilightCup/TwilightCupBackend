"""登录端（endpoint）角色校验测试：兼容性、放行、403/401 稳定错误码、审计日志。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import Account, AccountType
from twilightcupbackend.main import create_app


@pytest.fixture()
def env() -> SimpleNamespace:
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    for uname, roles in (
        ("player1", [AccountType.PLAYER]),
        ("ref1", [AccountType.REFEREE]),
        ("admin1", [AccountType.ADMIN]),
        ("multi", [AccountType.PLAYER, AccountType.DIRECTOR]),
    ):
        db.accounts.insert(
            Account(
                username=uname,
                password_hash=hash_password("pw"),
                roles=roles,
                display_name=uname,
            )
        )
    app = create_app(db=db)
    with TestClient(app) as client:
        return SimpleNamespace(client=client)


def _login(env: SimpleNamespace, body: dict[str, object]) -> httpx.Response:
    return env.client.post("/auth/login", json=body)


def test_login_without_endpoint_unchanged(env: SimpleNamespace) -> None:
    resp = _login(env, {"username": "player1", "password": "pw"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["username"] == "player1"
    assert "code" not in body  # 成功响应结构不变


def test_login_endpoint_with_matching_role(env: SimpleNamespace) -> None:
    for uname, endpoint in (
        ("ref1", "referee"),
        ("admin1", "admin"),
        ("multi", "player"),
        ("multi", "director"),
    ):
        resp = _login(env, {"username": uname, "password": "pw", "endpoint": endpoint})
        assert resp.status_code == 200, f"{uname}/{endpoint} → {resp.text}"
        assert resp.json()["access_token"]


def test_login_endpoint_forbidden(env: SimpleNamespace) -> None:
    resp = _login(env, {"username": "player1", "password": "pw", "endpoint": "admin"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "ENDPOINT_FORBIDDEN"
    assert body["msg"]
    assert "access_token" not in body  # 不签发令牌
    # 多角色账号只被无权限的端拒绝
    resp = _login(env, {"username": "multi", "password": "pw", "endpoint": "admin"})
    assert resp.status_code == 403


def test_login_invalid_endpoint_value(env: SimpleNamespace) -> None:
    resp = _login(env, {"username": "player1", "password": "pw", "endpoint": "xxx"})
    assert resp.status_code == 422
    # null 等价于不传（自动）
    resp = _login(env, {"username": "player1", "password": "pw", "endpoint": None})
    assert resp.status_code == 200


def test_login_wrong_password_coded(env: SimpleNamespace) -> None:
    resp = _login(env, {"username": "player1", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_CREDENTIALS"
    # 密码错误优先于端权限：不因 endpoint 缺角色而 403 泄露信息
    resp = _login(
        env, {"username": "player1", "password": "wrong", "endpoint": "admin"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_CREDENTIALS"
    # 不存在的账号同样 401（不枚举用户名）
    resp = _login(env, {"username": "ghost", "password": "pw", "endpoint": "admin"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "INVALID_CREDENTIALS"


def test_endpoint_forbidden_audited(
    env: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(
        logging.WARNING, logger="twilightcupbackend.rest.auth_controller"
    ):
        resp = _login(
            env, {"username": "player1", "password": "pw", "endpoint": "director"}
        )
    assert resp.status_code == 403
    assert any(
        "player1" in r.message and "director" in r.message for r in caplog.records
    ), caplog.text
