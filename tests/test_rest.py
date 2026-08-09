"""M3 鉴权与管理 REST 测试：以 mongomock 支撑的 app 跑 HTTP 流程。"""

from __future__ import annotations

from typing import Any

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import (
    Account,
    AccountType,
    ScoringMethod,
)
from twilightcupbackend.main import create_app


@pytest.fixture()
def env():  # type: ignore[no-untyped-def]
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    admin = Account(
        username="admin",
        password_hash=hash_password("adminpw"),
        roles=[AccountType.ADMIN],
        display_name="管理员",
    )
    db.accounts.insert(admin)
    app = create_app(db=db)
    with TestClient(app) as client:
        resp = client.post(
            "/auth/login", json={"username": "admin", "password": "adminpw"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["username"] == "admin"  # TokenResponse 含登录名
        token = body["access_token"]
        yield client, db, admin, token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_health(env) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    assert client.get("/health").status_code == 200


def test_create_account_as_admin(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    body: dict[str, Any] = {
        "username": "p1",
        "password": "pw",
        "display_name": "选手1",
        "roles": [AccountType.PLAYER],
    }
    resp = client.post("/admin/accounts", json=body, headers=_auth(token))
    assert resp.status_code == 201
    out = resp.json()
    assert out["username"] == "p1"
    assert AccountType.PLAYER in out["roles"]
    assert "password_hash" not in out


def test_create_account_no_token_401(env) -> None:  # type: ignore[no-untyped-def]
    client, *_ = env
    resp = client.post(
        "/admin/accounts",
        json={
            "username": "x",
            "password": "p",
            "display_name": "x",
            "roles": [AccountType.PLAYER],
        },
    )
    assert resp.status_code == 401


def test_non_admin_forbidden(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, admin_token = env
    # 创建一个选手账号
    client.post(
        "/admin/accounts",
        json={
            "username": "p2",
            "password": "pw",
            "display_name": "选手2",
            "roles": [AccountType.PLAYER],
        },
        headers=_auth(admin_token),
    )
    # 选手登录
    resp = client.post("/auth/login", json={"username": "p2", "password": "pw"})
    player_token = resp.json()["access_token"]
    # 选手尝试建账号 → 403
    resp = client.post(
        "/admin/accounts",
        json={
            "username": "p3",
            "password": "pw",
            "display_name": "x",
            "roles": [AccountType.PLAYER],
        },
        headers=_auth(player_token),
    )
    assert resp.status_code == 403


def test_duplicate_username_409(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    body = {
        "username": "dup",
        "password": "p",
        "display_name": "x",
        "roles": [AccountType.PLAYER],
    }
    assert (
        client.post("/admin/accounts", json=body, headers=_auth(token)).status_code
        == 201
    )
    resp = client.post("/admin/accounts", json=body, headers=_auth(token))
    assert resp.status_code == 409


def test_me(env) -> None:  # type: ignore[no-untyped-def]
    client, _, admin, token = env
    resp = client.get("/auth/me", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["account_id"] == admin.id
    assert body["account_type"] == AccountType.ADMIN


def test_create_session_win_threshold(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    # 创建 4 个角色账号
    for uname, role in [
        ("pa", AccountType.PLAYER),
        ("pb", AccountType.PLAYER),
        ("ref", AccountType.REFEREE),
        ("dir", AccountType.DIRECTOR),
    ]:
        client.post(
            "/admin/accounts",
            json={
                "username": uname,
                "password": "p",
                "display_name": uname,
                "roles": [role],
            },
            headers=_auth(token),
        )
    body = {
        "name": "决赛",
        "bo_format": 9,
        "scoring_method": ScoringMethod.FASTEST,
        "start_countdown_delay": 5,
        "mappool": {"categories": []},
        "player_a": "pa",
        "player_b": "pb",
        "referee": "ref",
        "director": "dir",
    }
    resp = client.post("/admin/matches", json=body, headers=_auth(token))
    assert resp.status_code == 201
    out = resp.json()
    assert out["bo_format"] == 9
    assert out["win_threshold"] == 5  # (9//2)+1


def test_create_session_wrong_role_type_400(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    # 仅建一个选手，把裁判位指给选手 → 类型不符
    client.post(
        "/admin/accounts",
        json={
            "username": "pa",
            "password": "p",
            "display_name": "pa",
            "roles": [AccountType.PLAYER],
        },
        headers=_auth(token),
    )
    client.post(
        "/admin/accounts",
        json={
            "username": "pb",
            "password": "p",
            "display_name": "pb",
            "roles": [AccountType.PLAYER],
        },
        headers=_auth(token),
    )
    client.post(
        "/admin/accounts",
        json={
            "username": "dir",
            "password": "p",
            "display_name": "dir",
            "roles": [AccountType.DIRECTOR],
        },
        headers=_auth(token),
    )
    body = {
        "name": "x",
        "bo_format": 3,
        "scoring_method": ScoringMethod.FASTEST,
        "mappool": {"categories": []},
        "player_a": "pa",
        "player_b": "pb",
        "referee": "pa",  # 选手当裁判 → 类型不符
        "director": "dir",
    }
    resp = client.post("/admin/matches", json=body, headers=_auth(token))
    assert resp.status_code == 400


def test_session_create_ban_protect_counts(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    for uname, role in [
        ("pa", AccountType.PLAYER),
        ("pb", AccountType.PLAYER),
        ("ref", AccountType.REFEREE),
        ("dir", AccountType.DIRECTOR),
    ]:
        client.post(
            "/admin/accounts",
            json={
                "username": uname,
                "password": "p",
                "display_name": uname,
                "roles": [role],
            },
            headers=_auth(token),
        )
    body = {
        "name": "深度赛",
        "bo_format": 9,
        "scoring_method": ScoringMethod.FASTEST,
        "start_countdown_delay": 5,
        "ban_count": 2,
        "protect_count": 0,
        "mappool": {"categories": []},
        "player_a": "pa",
        "player_b": "pb",
        "referee": "ref",
        "director": "dir",
    }
    resp = client.post("/admin/matches", json=body, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["ban_count"] == 2
    assert out["protect_count"] == 0
