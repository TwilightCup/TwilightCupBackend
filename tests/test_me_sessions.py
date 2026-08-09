"""/me/matches 列表 + WS ?match= 选择比赛 测试。"""

from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.auth import hash_password, issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import (
    Account,
    AccountType,
    Category,
    CollectionConfig,
    Mappool,
    Match,
    Pick,
    PickType,
    ScoringMethod,
)
from twilightcupbackend.main import create_app


def _mp() -> Mappool:
    return Mappool(
        categories=[
            Category(
                name="ML",
                picks=[
                    Pick(
                        code="ML1",
                        name="x",
                        type=PickType.MULTI,
                        collection=CollectionConfig(raw={}),
                    )
                ],
            ),
            Category(
                name="TB",
                picks=[
                    Pick(
                        code="TB",
                        name="t",
                        type=PickType.MULTI,
                        collection=CollectionConfig(raw={}),
                    )
                ],
            ),
        ]
    )


@pytest.fixture()
def env():  # type: ignore[no-untyped-def]
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()

    def acct(u: str, role: AccountType, d: str) -> Account:
        a = Account(
            username=u,
            password_hash=hash_password("p"),
            roles=[role],
            display_name=d,
        )
        db.accounts.insert(a)
        return a

    pa = acct("pa", AccountType.PLAYER, "A")
    pb = acct("pb", AccountType.PLAYER, "B")
    pc = acct("pc", AccountType.PLAYER, "C")
    pd = acct("pd", AccountType.PLAYER, "D")
    ref = acct("ref", AccountType.REFEREE, "裁判")
    dri = acct("dri", AccountType.DIRECTOR, "导播")

    def mk(name: str, a: Account, b: Account) -> Match:
        s = Match(
            name=name,
            bo_format=3,
            win_threshold=2,
            scoring_method=ScoringMethod.FASTEST,
            start_countdown_delay=5,
            mappool=_mp(),
            player_a_id=a.id,
            player_b_id=b.id,
            referee_id=ref.id,
            director_id=dri.id,
        )
        db.matches.insert(s)
        return s

    s1 = mk("上半区", pa, pb)
    s2 = mk("下半区", pc, pd)

    app = create_app(db=db)
    with TestClient(app) as client:
        ref_token = issue_token(ref, settings)
        pa_token = issue_token(pa, settings)
        yield client, db, (s1, s2), ref_token, pa_token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_me_sessions_lists_both_for_referee(env) -> None:  # type: ignore[no-untyped-def]
    client, _, (s1, s2), ref_token, _ = env
    resp = client.get("/me/matches", headers=_auth(ref_token))
    assert resp.status_code == 200
    items = resp.json()
    names = {i["name"] for i in items}
    assert names == {"上半区", "下半区"}
    ids = {i["id"] for i in items}
    assert {s1.id, s2.id} == ids


def test_me_sessions_excludes_non_member(env) -> None:  # type: ignore[no-untyped-def]
    client, _, (s1, _), _, pa_token = env
    # pa 只参与上半区
    resp = client.get("/me/matches", headers=_auth(pa_token))
    assert resp.status_code == 200
    items = resp.json()
    assert [i["id"] for i in items] == [s1.id]


def test_ws_session_select_and_name(env) -> None:  # type: ignore[no-untyped-def]
    client, _, (s1, s2), ref_token, _ = env
    with client.websocket_connect(f"/ws/{ref_token}?match={s1.id}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "auth_ok"
        assert msg["match_id"] == s1.id
        assert msg["match_name"] == "上半区"
    with client.websocket_connect(f"/ws/{ref_token}?match={s2.id}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "auth_ok"
        assert msg["match_id"] == s2.id
        assert msg["match_name"] == "下半区"


def test_ws_session_non_member_auth_error(env) -> None:  # type: ignore[no-untyped-def]
    client, _, (_, s2), _, pa_token = env
    # pa 不在下半区 → auth_error
    with client.websocket_connect(f"/ws/{pa_token}?match={s2.id}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "auth_error"


def test_ws_session_auto_pick_when_unspecified(env) -> None:  # type: ignore[no-untyped-def]
    """不指定 session 时维持旧行为：自动挑该账号的一场（并列时取最新，不锁死）。"""
    client, _, (s1, s2), ref_token, _ = env
    with client.websocket_connect(f"/ws/{ref_token}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "auth_ok"
        assert msg["match_id"] in {s1.id, s2.id}
        assert msg["match_name"] in {"上半区", "下半区"}


def test_session_detail_member(env) -> None:  # type: ignore[no-untyped-def]
    client, _, (s1, _), ref_token, _ = env
    resp = client.get(f"/me/matches/{s1.id}", headers=_auth(ref_token))
    assert resp.status_code == 200
    out = resp.json()
    assert out["id"] == s1.id
    assert out["mappool"]["categories"][0]["name"] == "ML"  # 含结构化图池
    assert out["ban_count"] == 1 and out["protect_count"] == 1


def test_session_detail_non_member_403(env) -> None:  # type: ignore[no-untyped-def]
    client, _, (_, s2), _, pa_token = env
    # pa 不在 s2
    resp = client.get(f"/me/matches/{s2.id}", headers=_auth(pa_token))
    assert resp.status_code == 403


def test_session_detail_not_found_404(env) -> None:  # type: ignore[no-untyped-def]
    client, _, _, ref_token, _ = env
    resp = client.get("/me/matches/nope", headers=_auth(ref_token))
    assert resp.status_code == 404
