"""账号删除安全校验测试：删自己/删管理员/引用完整性/末位管理员降级保护。"""

from __future__ import annotations

from types import SimpleNamespace
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
    MatchStatus,
    now_ts,
)
from twilightcupbackend.main import create_app


def _mappool_body() -> dict[str, Any]:
    return {
        "name": "M1",
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
    accounts: list[tuple[str, list[AccountType]]] = [
        ("admin1", [AccountType.ADMIN]),
        ("admin2", [AccountType.ADMIN]),
        ("ref", [AccountType.REFEREE]),
        ("dri", [AccountType.DIRECTOR]),
    ]
    accounts += [(f"p{i}", [AccountType.PLAYER]) for i in range(4)]
    for uname, roles in accounts:
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
        token = client.post(
            "/auth/login", json={"username": "admin1", "password": "pw"}
        ).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        accounts_list = client.get("/admin/accounts", headers=h).json()
        ids = {a["username"]: a["id"] for a in accounts_list}
        mid = client.post("/admin/mappools", json=_mappool_body(), headers=h).json()[
            "id"
        ]
        return SimpleNamespace(client=client, db=db, token=token, ids=ids, mid=mid)


def _h(env: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {env.token}"}


def _create_match(env: SimpleNamespace, player_b: str = "p1") -> dict[str, Any]:
    body = {
        "name": "孤立赛",
        "bo_format": 3,
        "scoring_method": 1,
        "player_a": "p0",
        "player_b": player_b,
        "referee": "ref",
        "director": "dri",
        "mappool_id": env.mid,
    }
    resp = env.client.post("/admin/matches", json=body, headers=_h(env))
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_delete_self_forbidden(env: SimpleNamespace) -> None:
    resp = env.client.delete(f"/admin/accounts/{env.ids['admin1']}", headers=_h(env))
    assert resp.status_code == 403
    assert resp.json()["msg"] == "不能删除当前登录账号"


def test_delete_admin_forbidden(env: SimpleNamespace) -> None:
    resp = env.client.delete(f"/admin/accounts/{env.ids['admin2']}", headers=_h(env))
    assert resp.status_code == 403
    assert resp.json()["msg"] == "不能删除管理员账号"


def test_delete_clean_account(env: SimpleNamespace) -> None:
    resp = env.client.delete(f"/admin/accounts/{env.ids['p0']}", headers=_h(env))
    assert resp.status_code == 204
    assert (
        env.client.get(f"/admin/accounts/{env.ids['p0']}", headers=_h(env)).status_code
        == 404
    )


def test_delete_referenced_by_match(env: SimpleNamespace) -> None:
    m = _create_match(env)
    resp = env.client.delete(f"/admin/accounts/{env.ids['p0']}", headers=_h(env))
    assert resp.status_code == 409
    assert "1 场未归档比赛" in resp.json()["msg"]
    # 裁判/导播同样被引用阻挡
    resp = env.client.delete(f"/admin/accounts/{env.ids['ref']}", headers=_h(env))
    assert resp.status_code == 409
    resp = env.client.delete(f"/admin/accounts/{env.ids['dri']}", headers=_h(env))
    assert resp.status_code == 409
    # 比赛结束且归档后不再阻挡（口径与 /me/matches 一致）
    env.db.matches.update_fields(
        m["id"], {"status": MatchStatus.ENDED, "archived_at": now_ts()}
    )
    resp = env.client.delete(f"/admin/accounts/{env.ids['p0']}", headers=_h(env))
    assert resp.status_code == 204


def test_delete_referenced_by_tournament(env: SimpleNamespace) -> None:
    tid = env.client.post(
        "/admin/tournaments", json={"name": "T", "format": 1}, headers=_h(env)
    ).json()["id"]
    resp = env.client.post(
        f"/admin/tournaments/{tid}/participants",
        json={"usernames": ["p0"]},
        headers=_h(env),
    )
    assert resp.status_code == 200
    resp = env.client.delete(f"/admin/accounts/{env.ids['p0']}", headers=_h(env))
    assert resp.status_code == 409
    assert "1 个赛事" in resp.json()["msg"]


def test_delete_referenced_by_fixture(env: SimpleNamespace) -> None:
    tid = env.client.post(
        "/admin/tournaments", json={"name": "T", "format": 1}, headers=_h(env)
    ).json()["id"]
    env.client.post(
        f"/admin/tournaments/{tid}/participants",
        json={"usernames": ["p0", "p1", "p2", "p3"]},
        headers=_h(env),
    )
    assert (
        env.client.post(
            f"/admin/tournaments/{tid}/generate-bracket", headers=_h(env)
        ).status_code
        == 200
    )
    resp = env.client.delete(f"/admin/accounts/{env.ids['p0']}", headers=_h(env))
    assert resp.status_code == 409
    msg = resp.json()["msg"]
    assert "赛事" in msg and "对阵节点" in msg


def test_last_admin_demote_forbidden(env: SimpleNamespace) -> None:
    # 两管理员时降级其一：允许（还剩一个）
    resp = env.client.patch(
        f"/admin/accounts/{env.ids['admin2']}",
        json={"roles": [1]},
        headers=_h(env),
    )
    assert resp.status_code == 200
    assert 1 in resp.json()["roles"]
    # 末位管理员降级（含自降）：拒绝，防先降级再删除绕过守卫
    resp = env.client.patch(
        f"/admin/accounts/{env.ids['admin1']}",
        json={"roles": [1]},
        headers=_h(env),
    )
    assert resp.status_code == 403
    assert resp.json()["msg"] == "不能移除最后一个管理员角色"
    # 改密码/展示名不受影响
    resp = env.client.patch(
        f"/admin/accounts/{env.ids['admin1']}",
        json={"display_name": "超管"},
        headers=_h(env),
    )
    assert resp.status_code == 200
