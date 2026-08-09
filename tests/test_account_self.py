"""账号自助改密/改名（任意角色）+ 管理员改他人 测试。"""

from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import Account, AccountType
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
    players = {}
    for uname, role in [
        ("playerA", AccountType.PLAYER),
        ("referee", AccountType.REFEREE),
        ("director", AccountType.DIRECTOR),
    ]:
        a = Account(
            username=uname,
            password_hash=hash_password("pw"),
            roles=[role],
            display_name=uname,
        )
        db.accounts.insert(a)
        players[uname] = a
    app = create_app(db=db)
    with TestClient(app) as client:
        tokens = {}
        for uname, pwd in [
            ("admin", "adminpw"),
            ("playerA", "pw"),
            ("referee", "pw"),
            ("director", "pw"),
        ]:
            tokens[uname] = client.post(
                "/auth/login", json={"username": uname, "password": pwd}
            ).json()["access_token"]
        yield client, db, players, tokens


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("uname", ["playerA", "referee", "director"])
def test_self_change_password_all_roles(env, uname: str) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = env
    resp = client.post(
        "/me/password",
        json={"old_password": "pw", "new_password": "newpw123"},
        headers=_auth(tokens[uname]),
    )
    assert resp.status_code == 200, resp.text
    # 旧密码登录失败
    assert (
        client.post(
            "/auth/login", json={"username": uname, "password": "pw"}
        ).status_code
        == 401
    )
    # 新密码登录成功
    assert (
        client.post(
            "/auth/login", json={"username": uname, "password": "newpw123"}
        ).status_code
        == 200
    )


def test_self_change_password_wrong_old(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = env
    resp = client.post(
        "/me/password",
        json={"old_password": "wrong", "new_password": "newpw123"},
        headers=_auth(tokens["playerA"]),
    )
    assert resp.status_code == 400


def test_self_change_password_too_short(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = env
    resp = client.post(
        "/me/password",
        json={"old_password": "pw", "new_password": "ab"},
        headers=_auth(tokens["playerA"]),
    )
    assert resp.status_code == 400


def test_self_update_display_name(env) -> None:  # type: ignore[no-untyped-def]
    client, db, players, tokens = env
    resp = client.patch(
        "/me",
        json={"display_name": "新名字"},
        headers=_auth(tokens["playerA"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "新名字"
    # 落库
    assert db.accounts.get(players["playerA"].id).display_name == "新名字"


def test_self_update_display_name_empty(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = env
    resp = client.patch(
        "/me",
        json={"display_name": "   "},
        headers=_auth(tokens["playerA"]),
    )
    assert resp.status_code == 400


def test_admin_can_change_others_password_and_name(env) -> None:  # type: ignore[no-untyped-def]
    client, _, players, tokens = env
    target = players["referee"]
    # 管理员改他人的展示名 + 口令
    resp = client.patch(
        f"/admin/accounts/{target.id}",
        json={"display_name": "裁判改", "password": "adminreset"},
        headers=_auth(tokens["admin"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "裁判改"
    # 新口令可登录
    assert (
        client.post(
            "/auth/login", json={"username": "referee", "password": "adminreset"}
        ).status_code
        == 200
    )


def test_non_admin_cannot_change_others(env) -> None:  # type: ignore[no-untyped-def]
    client, _, players, tokens = env
    target = players["referee"]
    # 选手无权调管理员端
    resp = client.patch(
        f"/admin/accounts/{target.id}",
        json={"display_name": "x"},
        headers=_auth(tokens["playerA"]),
    )
    assert resp.status_code == 403
