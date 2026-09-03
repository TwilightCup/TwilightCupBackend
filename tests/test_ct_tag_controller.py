"""词条库（/admin/ct-tags）+ 图池 CT 类别 ct_tags 往返测试。"""

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


def test_default_tags_seeded_and_crud(env) -> None:  # type: ignore[no-untyped-def]
    client, db, _, token = env

    # 首次初始化已种入默认词条
    resp = client.get("/admin/ct-tags", headers=_auth(token))
    assert resp.status_code == 200
    names = {x["name"] for x in resp.json()}
    assert {"Glitchless", "Pinch", "Checkpoint", "Jumpless", "No Checkpoint", "No EC"} <= names

    # 新增
    resp = client.post(
        "/admin/ct-tags", json={"name": "Speedrun"}, headers=_auth(token)
    )
    assert resp.status_code == 201
    tag = resp.json()
    assert tag["name"] == "Speedrun"

    # 重名 409
    resp = client.post(
        "/admin/ct-tags", json={"name": "Speedrun"}, headers=_auth(token)
    )
    assert resp.status_code == 409

    # 删除
    resp = client.delete(f"/admin/ct-tags/{tag['id']}", headers=_auth(token))
    assert resp.status_code == 204
    assert client.get("/admin/ct-tags", headers=_auth(token)).json()[-1]["name"] != "Speedrun"


def test_non_admin_forbidden(env) -> None:  # type: ignore[no-untyped-def]
    client, db, _, token = env
    player = Account(
        username="p",
        password_hash=hash_password("pw"),
        roles=[AccountType.PLAYER],
        display_name="选手",
    )
    db.accounts.insert(player)
    ptok = client.post(
        "/auth/login", json={"username": "p", "password": "pw"}
    ).json()["access_token"]
    resp = client.get("/admin/ct-tags", headers=_auth(ptok))
    assert resp.status_code == 403


def test_mappool_ct_tags_roundtrip(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    body = {
        "name": "带词条图池",
        "mappool": {
            "categories": [
                {
                    "name": "CT",
                    "picks": [
                        {
                            "code": "CT1",
                            "name": "词条图",
                            "type": 2,
                            "collection": {"raw": {"levels": ["L1"]}},
                            "category": "CT",
                        }
                    ],
                    "ct_tags": ["Glitchless", "Pinch"],
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
    resp = client.post("/admin/mappools", json=body, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    cat = next(c for c in resp.json()["mappool"]["categories"] if c["name"] == "CT")
    assert cat["ct_tags"] == ["Glitchless", "Pinch"]
