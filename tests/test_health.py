"""M1 冒烟测试：应用可启动、/health 返回正常。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from twilightcupbackend.main import app


def test_health_ok() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_mappool_get_pick() -> None:
    """datatypes 基本可用性：图池按编号取选图。"""
    from twilightcupbackend.datatypes import (
        Category,
        CollectionConfig,
        Mappool,
        Pick,
        PickType,
    )

    pick = Pick(
        code="ML1",
        name="测试项目",
        type=PickType.MULTI,
        collection=CollectionConfig(raw={}),
        category="ML",
    )
    pool = Mappool(categories=[Category(name="ML", picks=[pick])])
    assert pool.get_pick("ML1") is pick
    assert pool.get_pick("NOPE") is None
