"""关卡库 CRUD + 下发展开 + 存量迁移 测试。"""

from __future__ import annotations

from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import (
    Account,
    AccountType,
    Category,
    CollectionConfig,
    Level,
    Mappool,
    MappoolDoc,
    Pick,
    PickType,
)
from twilightcupbackend.main import create_app


class FakeStorage:
    def public_url(self, key: str | None) -> str | None:
        return f"http://fake/{key}" if key else None

    presigned_url = public_url


@pytest.fixture()
def env(monkeypatch) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    fake = FakeStorage()
    monkeypatch.setattr("twilightcupbackend.main.Storage", lambda _settings: fake)
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    admin = Account(
        username="admin",
        password_hash=hash_password("x"),
        roles=[AccountType.ADMIN],
        display_name="管理员",
    )
    db.accounts.insert(admin)
    app = create_app(db=db)
    with TestClient(app) as client:
        token = client.post(
            "/auth/login", json={"username": "admin", "password": "x"}
        ).json()["access_token"]
        return SimpleNamespace(client=client, db=db, token=token, fake=fake)


def _h(env: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {env.token}"}


def test_level_crud(env: SimpleNamespace) -> None:
    h = _h(env)
    resp = env.client.post("/admin/levels", json={"name": "Intro"}, headers=h)
    assert resp.status_code == 201, resp.text
    lv = resp.json()
    assert lv["name"] == "Intro"
    assert lv["display_name"] == "Intro"  # 缺省同 name
    assert lv["logo_url"] is None
    lid = lv["id"]

    # 重复名 → 409
    assert (
        env.client.post("/admin/levels", json={"name": "Intro"}, headers=h).status_code
        == 409
    )

    # 列表
    assert len(env.client.get("/admin/levels", headers=h).json()) == 1

    # patch（display_name + logo → logo_url 签发）
    resp = env.client.patch(
        f"/admin/levels/{lid}",
        json={"display_name": "新手关", "logo": "logos/intro.png"},
        headers=h,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "新手关"
    assert body["logo_url"] == "http://fake/logos/intro.png"

    # get / delete
    assert env.client.get(f"/admin/levels/{lid}", headers=h).status_code == 200
    assert env.client.delete(f"/admin/levels/{lid}", headers=h).status_code == 204
    assert env.client.get(f"/admin/levels/{lid}", headers=h).status_code == 404


def test_level_non_admin_forbidden(env: SimpleNamespace) -> None:
    env.client.post(
        "/admin/accounts",
        json={
            "username": "p",
            "password": "pw",
            "display_name": "选手",
            "roles": [int(AccountType.PLAYER)],
        },
        headers=_h(env),
    )
    ptok = env.client.post(
        "/auth/login", json={"username": "p", "password": "pw"}
    ).json()["access_token"]
    assert (
        env.client.post(
            "/admin/levels",
            json={"name": "X"},
            headers={"Authorization": f"Bearer {ptok}"},
        ).status_code
        == 403
    )


def test_expand_collection_ids_to_names() -> None:
    """_expand_collection：raw 里的 level_id 展开为关卡名；查不到原样保留。"""
    from twilightcupbackend.connection_manager import ConnectionManager  # noqa: F401
    from twilightcupbackend.match_fsm import MatchEngine

    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    lv = Level(name="Intro", display_name="Intro")
    db.levels.insert(lv)

    class _FakeCM:  # 最小 cm 桩（expand 只用 db）
        pass

    engine = MatchEngine(_FakeCM(), db)  # type: ignore[arg-type]
    coll = CollectionConfig(raw={"levels": [lv.id, "Unknown"], "level": lv.id})
    out = engine._expand_collection(coll)
    assert out.raw["levels"] == ["Intro", "Unknown"]  # id→名；查不到原样
    assert out.raw["level"] == "Intro"


def test_migrate_levels_idempotent() -> None:
    """迁移脚本：名字→Level 入库 + raw 改 id；重复运行零改动。"""
    import scripts.migrate_levels as m

    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    # 造两个图池：多关（名字）+ 单关（名字）
    for pool_name in ("m1", "m2"):
        doc = MappoolDoc(
            name=pool_name,
            mappool=Mappool(
                categories=[
                    Category(
                        name="ML",
                        picks=[
                            Pick(
                                code="ML1",
                                name="x",
                                type=PickType.MULTI,
                                collection=CollectionConfig(
                                    raw={"levels": ["Intro", "Water"]}
                                ),
                                category="ML",
                            )
                        ],
                    ),
                    Category(
                        name="SL",
                        picks=[
                            Pick(
                                code="SL1",
                                name="y",
                                type=PickType.SINGLE,
                                collection=CollectionConfig(raw={"level": "Aztec"}),
                                category="SL",
                            )
                        ],
                    ),
                ]
            ),
            created_by="admin",
        )
        db.mappools.insert(doc)

    names = m.collect_names(db)
    assert names == {"Intro", "Water", "Aztec"}
    mapping = m.ensure_levels(db, names)
    pools, picks = m.rewrite_mappools(db, mapping)
    assert (pools, picks) == (2, 4)

    # 关卡库 3 条；raw 全为 id
    assert len(db.levels.find()) == 3
    for d in db.mappools.find():
        picks_by_code = {p.code: p for p in d.mappool.all_picks()}
        assert picks_by_code["ML1"].collection.raw["levels"] == [
            mapping["Intro"],
            mapping["Water"],
        ]
        assert picks_by_code["SL1"].collection.raw["level"] == mapping["Aztec"]

    # 幂等：再跑一遍——collect_names 已查不到未迁移名字（全是 id），零改写
    names2 = m.collect_names(db)
    assert names2 == set()
    mapping2 = m.ensure_levels(db, names2)
    pools2, picks2 = m.rewrite_mappools(db, {**mapping, **mapping2})
    assert (pools2, picks2) == (0, 0)
    assert len(db.levels.find()) == 3  # 没有为 id 重复建条目
