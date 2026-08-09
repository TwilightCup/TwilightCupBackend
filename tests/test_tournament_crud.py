"""赛事 CRUD + 成员池（选手/裁判组/导播组）管理 + /me 查询 测试。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import Account, AccountType, TournamentStatus
from twilightcupbackend.main import create_app


def _mappool_body(name: str = "M1") -> dict[str, Any]:
    return {
        "name": name,
        "mappool": {
            "categories": [
                {
                    "name": "ML",
                    "picks": [
                        {
                            "code": "ML1",
                            "name": "测试",
                            "type": 1,
                            "collection": {"raw": {}},
                            "category": "ML",
                        }
                    ],
                }
            ]
        },
    }


@pytest.fixture()
def env() -> SimpleNamespace:
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    admin = Account(
        username="admin",
        password_hash=hash_password("a"),
        roles=[AccountType.ADMIN],
        display_name="管理员",
    )
    db.accounts.insert(admin)
    players = [
        Account(
            username=f"p{i}",
            password_hash=hash_password("p"),
            roles=[AccountType.PLAYER],
            display_name=f"选手{i}",
        )
        for i in range(4)
    ]
    for p in players:
        db.accounts.insert(p)
    ref = Account(
        username="ref",
        password_hash=hash_password("p"),
        roles=[AccountType.REFEREE],
        display_name="裁判",
    )
    db.accounts.insert(ref)
    dri = Account(
        username="dri",
        password_hash=hash_password("p"),
        roles=[AccountType.DIRECTOR],
        display_name="导播",
    )
    db.accounts.insert(dri)

    app = create_app(db=db)
    with TestClient(app) as client:
        token = client.post(
            "/auth/login", json={"username": "admin", "password": "a"}
        ).json()["access_token"]
        h = _auth(token)
        mid = client.post("/admin/mappools", json=_mappool_body(), headers=h).json()[
            "id"
        ]
        return SimpleNamespace(
            client=client,
            db=db,
            token=token,
            mid=mid,
            players=players,
            ref=ref,
            dri=dri,
        )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _h(env: SimpleNamespace) -> dict[str, str]:
    return _auth(env.token)


def _create_body(env: SimpleNamespace, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "T1",
        "format": 1,  # SINGLE_ELIM
    }
    body.update(overrides)
    return body


def test_tournament_create_validations(env: SimpleNamespace) -> None:
    h = _h(env)
    # 赛事创建只需 name + format（无单场规则校验，规则在生成比赛时定）
    resp = env.client.post("/admin/tournaments", json=_create_body(env), headers=h)
    assert resp.status_code == 201, resp.text
    assert resp.json()["format"] == 1


def test_tournament_crud(env: SimpleNamespace) -> None:
    h = _h(env)
    # create
    resp = env.client.post("/admin/tournaments", json=_create_body(env), headers=h)
    assert resp.status_code == 201, resp.text
    t = resp.json()
    assert t["name"] == "T1"
    assert t["status"] == 0  # DRAFT
    tid = t["id"]

    # list
    assert len(env.client.get("/admin/tournaments", headers=h).json()) == 1

    # get
    assert env.client.get(f"/admin/tournaments/{tid}", headers=h).status_code == 200

    # patch（含改 name）
    resp = env.client.patch(f"/admin/tournaments/{tid}", json={"name": "T2"}, headers=h)
    assert resp.status_code == 200
    assert resp.json()["name"] == "T2"

    # delete
    assert env.client.delete(f"/admin/tournaments/{tid}", headers=h).status_code == 204


def test_member_management(env: SimpleNamespace) -> None:
    h = _h(env)
    tid = env.client.post(
        "/admin/tournaments", json=_create_body(env), headers=h
    ).json()["id"]

    # 加入 4 名选手
    resp = env.client.post(
        f"/admin/tournaments/{tid}/participants",
        json={"usernames": ["p0", "p1", "p2", "p3"]},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["participant_ids"]) == 4

    # 裁判组
    resp = env.client.post(
        f"/admin/tournaments/{tid}/referees",
        json={"usernames": ["ref"]},
        headers=h,
    )
    assert resp.status_code == 200
    assert len(resp.json()["referee_ids"]) == 1
    assert env.ref.id in resp.json()["referee_ids"]

    # 导播组
    resp = env.client.post(
        f"/admin/tournaments/{tid}/directors",
        json={"usernames": ["dri"]},
        headers=h,
    )
    assert resp.status_code == 200
    assert env.dri.id in resp.json()["director_ids"]

    # 种子序
    t = env.client.get(f"/admin/tournaments/{tid}", headers=h).json()
    pids = t["participant_ids"]
    resp = env.client.post(
        f"/admin/tournaments/{tid}/seeds", json={"seed_order": pids}, headers=h
    )
    assert resp.status_code == 200
    assert resp.json()["seed_order"] == pids

    # 种子序长度不符 → 400
    assert (
        env.client.post(
            f"/admin/tournaments/{tid}/seeds",
            json={"seed_order": pids[:2]},
            headers=h,
        ).status_code
        == 400
    )
    # 种子含非池内 id → 400
    assert (
        env.client.post(
            f"/admin/tournaments/{tid}/seeds",
            json={"seed_order": ["nope", *pids[1:]]},
            headers=h,
        ).status_code
        == 400
    )

    # 角色不符：把选手当裁判加 → 400
    assert (
        env.client.post(
            f"/admin/tournaments/{tid}/referees",
            json={"usernames": ["p0"]},
            headers=h,
        ).status_code
        == 400
    )

    # 移除一名选手
    resp = env.client.post(
        f"/admin/tournaments/{tid}/participants/remove",
        json={"usernames": ["p3"]},
        headers=h,
    )
    assert resp.status_code == 200
    assert len(resp.json()["participant_ids"]) == 3


def test_member_management_requires_draft(env: SimpleNamespace) -> None:
    h = _h(env)
    tid = env.client.post(
        "/admin/tournaments", json=_create_body(env), headers=h
    ).json()["id"]
    t = env.db.tournaments.get(tid)
    assert t is not None
    t.status = TournamentStatus.READY
    env.db.tournaments.replace(t)
    assert (
        env.client.post(
            f"/admin/tournaments/{tid}/participants",
            json={"usernames": ["p0"]},
            headers=h,
        ).status_code
        == 400
    )


def _login(env: SimpleNamespace, username: str) -> str:
    return env.client.post(
        "/auth/login", json={"username": username, "password": "p"}
    ).json()["access_token"]


def test_me_tournaments(env: SimpleNamespace) -> None:
    h = _h(env)
    tid = env.client.post(
        "/admin/tournaments", json=_create_body(env), headers=h
    ).json()["id"]
    env.client.post(
        f"/admin/tournaments/{tid}/participants",
        json={"usernames": ["p0"]},
        headers=h,
    )
    env.client.post(
        f"/admin/tournaments/{tid}/referees", json={"usernames": ["ref"]}, headers=h
    )

    # 选手 p0 查 /me/tournaments
    ptok = _login(env, "p0")
    resp = env.client.get("/me/tournaments", headers=_auth(ptok))
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["id"] == tid

    # 裁判也能看到该赛事
    rtok = _login(env, "ref")
    assert len(env.client.get("/me/tournaments", headers=_auth(rtok)).json()) == 1

    # 未参与的选手 p1 看不到
    p1tok = _login(env, "p1")
    assert len(env.client.get("/me/tournaments", headers=_auth(p1tok)).json()) == 0
