"""speedrun 代理持久化缓存 + SWR 三模式（auto/cached/refresh）测试。

上游打桩：monkeypatch ``speedrun_proxy._http`` 返回 FakeClient（预置响应
队列 + 调用记录 + 可选 Event 阻塞）；持久层走 mongomock。注意 mongomock
会按 TTL 索引清文档——测试内构造的固定 fetched_at 必须落在 7 天保留期内。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password, issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import Account, AccountType
from twilightcupbackend.main import create_app
from twilightcupbackend.rest import speedrun_proxy as sp

CID = "n2yo3jzd"  # 任意分类 id
LB_PATH = f"/speedrun/leaderboard?category_id={CID}&top=5"


def _payload(marker: str) -> dict[str, Any]:
    """上游 leaderboard 响应样本（形状无关紧要，标记位用于区分新旧）。"""
    return {
        "data": {
            "marker": marker,
            "runs": [{"place": 1, "run": {"times": {"primary_t": 1.0}}}],
        }
    }


def _mode(path: str, mode: str) -> str:
    return f"{path}{'&' if '?' in path else '?'}mode={mode}"


class _FakeResp:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """预置响应队列的上游客户端；gate 非 None 时首次调用阻塞至放行。"""

    def __init__(self) -> None:
        self.responses: list[_FakeResp] = []
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.gate: asyncio.Event | None = None

    def enqueue(self, resp: _FakeResp) -> None:
        self.responses.append(resp)

    async def get(self, url: str, params: dict[str, str] | None = None) -> _FakeResp:
        self.calls.append((url, params))
        if self.gate is not None:
            gate, self.gate = self.gate, None
            await gate.wait()
        return self.responses.pop(0)


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    dri = Account(
        username="dri",
        password_hash=hash_password("pw"),
        roles=[AccountType.DIRECTOR],
        display_name="导播",
    )
    pla = Account(
        username="pla",
        password_hash=hash_password("pw"),
        roles=[AccountType.PLAYER],
        display_name="选手",
    )
    db.accounts.insert(dri)
    db.accounts.insert(pla)
    token = issue_token(dri, settings)
    player_token = issue_token(pla, settings)
    fake = _FakeClient()
    monkeypatch.setattr(sp, "_http", lambda: fake)
    with TestClient(create_app(db=db)) as client:
        yield client, db, fake, token, player_token


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── cached 模式 ─────────────────────────────────────────────────────────────


def test_cached_cold_cache_returns_double_null(env) -> None:  # type: ignore[no-untyped-def]
    client, db, fake, token, _ = env
    resp = client.get(_mode(LB_PATH, "cached"), headers=_hdr(token))
    assert resp.status_code == 200
    assert resp.json() == {"fetched_at": None, "data": None}
    assert fake.calls == []  # cached 绝不打上游
    assert db.speedrun_cache.count() == 0


def test_cached_hit_after_auto(env) -> None:  # type: ignore[no-untyped-def]
    client, _db, fake, token, _ = env
    fake.enqueue(_FakeResp(200, _payload("a")))
    client.get(LB_PATH, headers=_hdr(token))
    body = client.get(_mode(LB_PATH, "cached"), headers=_hdr(token)).json()
    assert body["data"] == _payload("a")
    datetime.fromisoformat(body["fetched_at"])  # 断言可解析 ISO
    assert len(fake.calls) == 1  # cached 命中零上游调用


# ── auto / refresh 模式与持久化 ─────────────────────────────────────────────


def test_auto_write_through_and_memory_hit(env) -> None:  # type: ignore[no-untyped-def]
    client, db, fake, token, _ = env
    fake.enqueue(_FakeResp(200, _payload("a")))
    r1 = client.get(LB_PATH, headers=_hdr(token))
    assert r1.status_code == 200
    assert r1.json() == _payload("a")  # 裸上游原文，非信封
    assert client.get(LB_PATH, headers=_hdr(token)).json() == _payload("a")
    assert len(fake.calls) == 1  # 二次 auto 命中内存
    docs = db.speedrun_cache.find()
    assert len(docs) == 1
    assert docs[0].kind == "leaderboard"
    assert docs[0].data == _payload("a")
    assert "/leaderboards/" in docs[0].key


def test_refresh_different_data_replaces_doc(env) -> None:  # type: ignore[no-untyped-def]
    client, db, fake, token, _ = env
    fake.enqueue(_FakeResp(200, _payload("a")))
    fake.enqueue(_FakeResp(200, _payload("b")))
    client.get(LB_PATH, headers=_hdr(token))
    resp = client.get(_mode(LB_PATH, "refresh"), headers=_hdr(token))
    assert resp.status_code == 200
    assert resp.json() == _payload("b")  # 绕过内存拉到新值
    assert len(fake.calls) == 2
    assert db.speedrun_cache.find()[0].data == _payload("b")  # 不同 → 更新缓存
    body = client.get(_mode(LB_PATH, "cached"), headers=_hdr(token)).json()
    assert body["data"] == _payload("b")


def test_refresh_same_data_updates_fetched_at_only(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    client, db, fake, token, _ = env
    # 注意：mongomock 会按 TTL 索引清文档，固定时间戳必须落在 7 天保留期内
    # （否则「出生即过期」直接消失）；且微秒被截断到毫秒，故用整秒。
    t1 = datetime.now(UTC).replace(microsecond=0)
    t2 = t1 + timedelta(days=1)
    times = iter((t1, t2))
    monkeypatch.setattr(sp, "now_ts", lambda: next(times))
    fake.enqueue(_FakeResp(200, _payload("a")))
    fake.enqueue(_FakeResp(200, _payload("a")))
    client.get(LB_PATH, headers=_hdr(token))
    client.get(_mode(LB_PATH, "refresh"), headers=_hdr(token))
    doc = db.speedrun_cache.find()[0]
    assert doc.data == _payload("a")  # 相同 → 数据不动
    # mongomock 读回 naive datetime（生产 pymongo tz_aware=True 无此问题），归一化比较
    assert doc.fetched_at.replace(tzinfo=UTC) == t2  # 只更 fetched_at


async def test_single_flight_merges_concurrent(monkeypatch: pytest.MonkeyPatch) -> None:
    """同 key 并发 auto/refresh 合并为一次上游调用（store 层直测）。"""
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    fake = _FakeClient()
    fake.enqueue(_FakeResp(200, _payload("a")))
    gate = asyncio.Event()
    fake.gate = gate
    monkeypatch.setattr(sp, "_http", lambda: fake)
    store = sp._SpeedrunStore(db.speedrun_cache)
    url = f"{sp.SR_BASE}/leaderboards/{sp.HFF_GAME_ID}/category/{CID}"
    task_a = asyncio.ensure_future(
        store.get("auto", "leaderboard", url, 60.0, {"top": "5"})
    )
    while not fake.calls:  # 等 a 创建上游任务并阻塞在 gate
        await asyncio.sleep(0)
    task_b = asyncio.ensure_future(
        store.get("refresh", "leaderboard", url, 60.0, {"top": "5"})
    )
    await asyncio.sleep(0)  # 让 b 挂到同一在途任务
    gate.set()
    ra, rb = await asyncio.gather(task_a, task_b)
    assert ra == rb == _payload("a")
    assert len(fake.calls) == 1
    assert db.speedrun_cache.count() == 1


# ── 失败路径 ────────────────────────────────────────────────────────────────


def test_upstream_420_passthrough_and_no_write(env) -> None:  # type: ignore[no-untyped-def]
    client, db, fake, token, _ = env
    fake.enqueue(_FakeResp(420))
    assert client.get(LB_PATH, headers=_hdr(token)).status_code == 420
    fake.enqueue(_FakeResp(420))
    assert client.get(_mode(LB_PATH, "refresh"), headers=_hdr(token)).status_code == 420
    assert db.speedrun_cache.count() == 0  # 失败不写缓存


def test_cached_survives_upstream_rate_limit(env) -> None:  # type: ignore[no-untyped-def]
    """上游限流时 cached 仍回旧缓存——前端『保留缓存渲染』的后端依据。"""
    client, _db, fake, token, _ = env
    fake.enqueue(_FakeResp(200, _payload("a")))
    client.get(LB_PATH, headers=_hdr(token))
    fake.enqueue(_FakeResp(420))
    assert client.get(_mode(LB_PATH, "refresh"), headers=_hdr(token)).status_code == 420
    body = client.get(_mode(LB_PATH, "cached"), headers=_hdr(token)).json()
    assert body["data"] == _payload("a")


def test_upstream_5xx_and_bad_json_502_no_write(env) -> None:  # type: ignore[no-untyped-def]
    client, db, fake, token, _ = env
    fake.enqueue(_FakeResp(500))
    assert client.get(LB_PATH, headers=_hdr(token)).status_code == 502
    fake.enqueue(_FakeResp(200, None))  # 200 但非 JSON
    assert client.get(LB_PATH, headers=_hdr(token)).status_code == 502
    assert db.speedrun_cache.count() == 0


def test_mongo_write_failure_not_fatal(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    client, db, fake, token, _ = env

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("mongo down")

    monkeypatch.setattr(db.speedrun_cache, "replace", boom)
    monkeypatch.setattr(db.speedrun_cache, "update_fields", boom)
    fake.enqueue(_FakeResp(200, _payload("a")))
    resp = client.get(LB_PATH, headers=_hdr(token))
    assert resp.status_code == 200  # 持久化失败只告警
    assert resp.json() == _payload("a")


# ── 持久化跨重启 / 鉴权与参数校验 / 端点矩阵 ───────────────────────────────


def test_persistence_across_app_rebuild(env) -> None:  # type: ignore[no-untyped-def]
    """新 app = 新内存缓存；cached 仍命中旧 app 写入的 Mongo 文档。"""
    client, db, fake, token, _ = env
    fake.enqueue(_FakeResp(200, _payload("a")))
    client.get(LB_PATH, headers=_hdr(token))
    with TestClient(create_app(db=db)) as client2:
        body = client2.get(_mode(LB_PATH, "cached"), headers=_hdr(token)).json()
        assert body["data"] == _payload("a")
        assert len(fake.calls) == 1  # 重启后零上游调用


def test_mode_validation_and_auth(env) -> None:  # type: ignore[no-untyped-def]
    client, _db, _fake, token, player_token = env
    assert client.get(_mode(LB_PATH, "bogus"), headers=_hdr(token)).status_code == 422
    assert client.get(LB_PATH).status_code == 401  # 缺令牌
    # 选手角色无权
    assert client.get(LB_PATH, headers=_hdr(player_token)).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/speedrun/game-meta",
        "/speedrun/variables?category_id=x",
        LB_PATH,
        "/speedrun/user?lookup=abc",
        "/speedrun/pb?user_id=u1",
    ],
)
def test_endpoint_matrix_cached_cold(env, path: str) -> None:  # type: ignore[no-untyped-def]
    client, _db, fake, token, _ = env
    resp = client.get(_mode(path, "cached"), headers=_hdr(token))
    assert resp.status_code == 200
    assert resp.json() == {"fetched_at": None, "data": None}
    assert fake.calls == []
