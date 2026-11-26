"""对象存储（MinIO）logo 签发 + 上传端点测试（用 FakeStorage 避免真实 minio）。"""

from __future__ import annotations

from typing import Any

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController, resolve_pick_logo_url
from twilightcupbackend.datatypes import (
    Account,
    AccountType,
    Category,
    CollectionConfig,
    Mappool,
    MappoolDoc,
    Pick,
    PickType,
)
from twilightcupbackend.main import create_app
from twilightcupbackend.rest.schemas import MappoolOut


class FakeStorage:
    """内存假存储：记 put 调用 + presigned 返回可识别 URL。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def gen_key(self, prefix: str, suffix: str) -> str:
        return f"{prefix}/fake-key{suffix}"

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = data
        return key

    def public_url(self, key: str | None) -> str | None:
        if not key:
            return None
        return f"http://fake-minio/twilightcup/{key}?sig=OK"

    # 向后兼容旧调用名（与真实 Storage 同）
    presigned_url = public_url


def _pick(code: str, logo: str | None = None) -> Pick:
    return Pick(
        code=code,
        name=code,
        type=PickType.MULTI,
        collection=CollectionConfig(raw={}),
        category="ML",
        logo=logo,
    )


def _mappool_doc(db: DBController, logo: str | None = "logos/abc.png") -> MappoolDoc:
    doc = MappoolDoc(
        name="M1",
        mappool=Mappool(categories=[Category(name="ML", picks=[_pick("ML1", logo)])]),
        created_by="admin",
    )
    db.mappools.insert(doc)
    return doc


def test_mappool_out_signs_logo_url() -> None:
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    doc = _mappool_doc(db, logo="logos/abc.png")
    storage = FakeStorage()
    out = MappoolOut.from_doc(doc, storage)
    pick = out.mappool.categories[0].picks[0]
    assert pick.logo == "logos/abc.png"
    assert pick.logo_url == "http://fake-minio/twilightcup/logos/abc.png?sig=OK"


def test_mappool_out_no_storage_keeps_logo_url_none() -> None:
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    doc = _mappool_doc(db, logo="logos/abc.png")
    out = MappoolOut.from_doc(doc, None)
    pick = out.mappool.categories[0].picks[0]
    assert pick.logo == "logos/abc.png"
    assert pick.logo_url is None


def test_mappool_out_no_logo() -> None:
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    doc = _mappool_doc(db, logo=None)
    storage = FakeStorage()
    out = MappoolOut.from_doc(doc, storage)
    pick = out.mappool.categories[0].picks[0]
    assert pick.logo is None
    assert pick.logo_url is None


# ---- 选图展示图按关卡回退（controllers.resolve_pick_logo_url）----


def _mk_pick(levels: list[str], logo: str | None = None) -> Pick:
    return Pick(
        code="P1",
        name="P1",
        type=PickType.MULTI,
        collection=CollectionConfig(raw={"name": "P1", "levels": levels}),
        category="ML",
        logo=logo,
    )


def _mk_db_levels() -> DBController:
    """建 3 关卡库：L1/L2 配展示图，L3 不配（模拟 Any% 终点 Intro_Reprise 无图）。"""
    from twilightcupbackend.datatypes import Level

    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    for name, logo in (("L1", "logos/l1.png"), ("L2", "logos/l2.png"), ("L3", None)):
        db.levels.insert(Level(name=name, display_name=name, logo=logo))
    return db


def _logo_url(db: DBController, pick: Pick) -> str | None:
    return resolve_pick_logo_url(pick, db, FakeStorage())


def test_pick_logo_wins_over_level_logo() -> None:
    db = _mk_db_levels()
    pick = _mk_pick(["L1", "L2"], logo="logos/own.png")
    assert _logo_url(db, pick) == "http://fake-minio/twilightcup/logos/own.png?sig=OK"


def test_pick_logo_falls_back_to_endpoint_level() -> None:
    db = _mk_db_levels()
    # 终点关 L2 配图 → 显示终点关（Aztec% → Aztec 口径）
    pick = _mk_pick(["L1", "L2"])
    assert _logo_url(db, pick) == "http://fake-minio/twilightcup/logos/l2.png?sig=OK"
    # 终点关 L3 无图 → 逆序退到最近一个配图关（Any% 终点无图退到上一关口径）
    pick = _mk_pick(["L1", "L2", "L3"])
    assert _logo_url(db, pick) == "http://fake-minio/twilightcup/logos/l2.png?sig=OK"
    pick = _mk_pick(["L1", "L3"])
    assert _logo_url(db, pick) == "http://fake-minio/twilightcup/logos/l1.png?sig=OK"


def test_pick_logo_level_by_legacy_name_and_workshop_skip() -> None:
    db = _mk_db_levels()
    # 遗留名引用（值是关卡名而非库内 id）：按名查库命中
    pick = _mk_pick(["L2"])
    assert db.levels.get("L2") is None  # 「L2」不是合法 id，走 get_by_name 分支
    assert _logo_url(db, pick) == "http://fake-minio/twilightcup/logos/l2.png?sig=OK"
    # 工坊数字 id / 空合集：返回 None（前端再按名称回退官方关卡图）
    ws = _mk_pick(["1234567890"])
    assert _logo_url(db, ws) is None
    empty = _mk_pick([])
    assert _logo_url(db, empty) is None


def test_pick_logo_no_db_only_pick_logo() -> None:
    # db 缺席（如 start/pause/resume 响应）：仅签 pick 自有，不做关卡回退
    pick = _mk_pick(["L2"], logo="logos/own.png")
    assert resolve_pick_logo_url(pick, None, FakeStorage()) == (
        "http://fake-minio/twilightcup/logos/own.png?sig=OK"
    )
    assert resolve_pick_logo_url(_mk_pick(["L2"]), None, FakeStorage()) is None


@pytest.fixture()
def env(monkeypatch):  # type: ignore[no-untyped-def]
    fake = FakeStorage()
    # 让 create_app 内的 Storage(settings) 返回 FakeStorage（避免连真 minio）
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
        yield client, db, fake, token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_upload_logo(env) -> None:  # type: ignore[no-untyped-def]
    client, _, fake, token = env
    resp = client.post(
        "/admin/uploads",
        files={"file": ("a.png", b"\x89PNG fake", "image/png")},
        headers=_auth(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["key"] == "logos/fake-key.png"
    assert body["url"].startswith("http://fake-minio/")
    assert "logos/fake-key.png" in fake.objects


def test_upload_rejects_bad_type(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    resp = client.post(
        "/admin/uploads",
        files={"file": ("a.txt", b"hello", "text/plain")},
        headers=_auth(token),
    )
    assert resp.status_code == 400


def test_upload_rejects_too_large(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, token = env
    big = b"x" * (5 * 1024 * 1024 + 1)
    resp = client.post(
        "/admin/uploads",
        files={"file": ("a.png", big, "image/png")},
        headers=_auth(token),
    )
    assert resp.status_code == 400


def test_pick_logo_persists_through_mappool(env) -> None:  # type: ignore[no-untyped-def]
    """建图池带 Pick.logo → 读回 logo 透传 + logo_url 签发。"""
    client, _, _, token = env
    body: dict[str, Any] = {
        "name": "决赛图池",
        "mappool": {
            "categories": [
                {
                    "name": "ML",
                    "picks": [
                        {
                            "code": "ML1",
                            "name": "图1",
                            "type": 1,
                            "collection": {"raw": {}},
                            "category": "ML",
                            "logo": "logos/existing.png",
                        }
                    ],
                }
            ]
        },
    }
    resp = client.post("/admin/mappools", json=body, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    # 读回
    resp = client.get(f"/admin/mappools/{mid}", headers=_auth(token))
    pick = resp.json()["mappool"]["categories"][0]["picks"][0]
    assert pick["logo"] == "logos/existing.png"
    assert pick["logo_url"] == "http://fake-minio/twilightcup/logos/existing.png?sig=OK"
