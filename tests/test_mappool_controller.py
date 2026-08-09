"""图池库 CRUD + 比赛按 mappool_id 解析 测试。"""

from __future__ import annotations

from typing import Any

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import Account, AccountType, ScoringMethod
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
        token = resp.json()["access_token"]
        yield client, db, admin, token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mappool_body(name: str = "决赛图池") -> dict[str, Any]:
    return {
        "name": name,
        "mappool": {
            "categories": [
                {
                    "name": "ML",
                    "picks": [
                        {
                            "code": "ML1",
                            "name": "测试全关",
                            "type": 1,
                            "collection": {"raw": {"levels": ["L1"]}},
                            "category": "ML",
                        }
                    ],
                },
                {
                    "name": "TB",
                    "picks": [
                        {
                            "code": "TB",
                            "name": "决胜",
                            "type": 1,
                            "collection": {"raw": {}},
                            "category": "TB",
                        }
                    ],
                },
            ]
        },
    }


def test_mappool_crud(env) -> None:  # type: ignore[no-untyped-def]
    client, _, admin, token = env

    # create
    resp = client.post("/admin/mappools", json=_mappool_body(), headers=_auth(token))
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["name"] == "决赛图池"
    assert created["created_by"] == admin.id
    mid = created["id"]

    # list（倒序）
    resp = client.get("/admin/mappools", headers=_auth(token))
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == mid

    # get
    resp = client.get(f"/admin/mappools/{mid}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["mappool"]["categories"][0]["name"] == "ML"

    # update（改名）
    resp = client.patch(
        f"/admin/mappools/{mid}",
        json={"name": "决赛图池 v2"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "决赛图池 v2"

    # delete
    resp = client.delete(f"/admin/mappools/{mid}", headers=_auth(token))
    assert resp.status_code == 204
    assert client.get(f"/admin/mappools/{mid}", headers=_auth(token)).status_code == 404


def test_mappool_duplicate_name_409(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    assert (
        client.post(
            "/admin/mappools", json=_mappool_body(), headers=_auth(token)
        ).status_code
        == 201
    )
    resp = client.post("/admin/mappools", json=_mappool_body(), headers=_auth(token))
    assert resp.status_code == 409


def test_mappool_non_admin_forbidden(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    client.post(
        "/admin/accounts",
        json={
            "username": "p",
            "password": "pw",
            "display_name": "选手",
            "roles": [AccountType.PLAYER],
        },
        headers=_auth(token),
    )
    ptok = client.post("/auth/login", json={"username": "p", "password": "pw"}).json()[
        "access_token"
    ]
    resp = client.post("/admin/mappools", json=_mappool_body(), headers=_auth(ptok))
    assert resp.status_code == 403


def _make_role_accounts(client, token) -> None:  # type: ignore[no-untyped-def]
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


def test_session_create_with_mappool_id(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    _make_role_accounts(client, token)

    mid = client.post(
        "/admin/mappools", json=_mappool_body(), headers=_auth(token)
    ).json()["id"]

    body = {
        "name": "决赛",
        "bo_format": 3,
        "scoring_method": ScoringMethod.FASTEST,
        "start_countdown_delay": 5,
        "mappool_id": mid,
        "player_a": "pa",
        "player_b": "pb",
        "referee": "ref",
        "director": "dir",
    }
    resp = client.post("/admin/matches", json=body, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    out = resp.json()
    # 内嵌图池与库一致
    cats = {c["name"] for c in out["mappool"]["categories"]}
    assert {"ML", "TB"} <= cats


def test_session_create_bad_mappool_id_404(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    _make_role_accounts(client, token)
    body = {
        "name": "x",
        "bo_format": 3,
        "scoring_method": ScoringMethod.FASTEST,
        "start_countdown_delay": 5,
        "mappool_id": "nope",
        "player_a": "pa",
        "player_b": "pb",
        "referee": "ref",
        "director": "dir",
    }
    resp = client.post("/admin/matches", json=body, headers=_auth(token))
    assert resp.status_code == 404


def test_session_create_no_mappool_400(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    _make_role_accounts(client, token)
    body = {
        "name": "x",
        "bo_format": 3,
        "scoring_method": ScoringMethod.FASTEST,
        "start_countdown_delay": 5,
        "player_a": "pa",
        "player_b": "pb",
        "referee": "ref",
        "director": "dir",
    }
    resp = client.post("/admin/matches", json=body, headers=_auth(token))
    assert resp.status_code == 400
