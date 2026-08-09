"""对象存储（MinIO）logo 签发 + 上传端点测试（用 FakeStorage 避免真实 minio）。"""

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

    def presigned_url(self, key: str | None) -> str | None:
        if not key:
            return None
        return f"http://fake-minio/twilightcup/{key}?sig=OK"


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
