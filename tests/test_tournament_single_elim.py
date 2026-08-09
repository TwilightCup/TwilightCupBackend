"""单败赛程引擎测试：seed 播位、生成、bye、推进、排名 + REST 端点。"""

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
    FixtureStatus,
    Tournament,
    TournamentFormat,
    TournamentStatus,
)
from twilightcupbackend.main import create_app
from twilightcupbackend.tournament_engine import TournamentEngine, _seed_slots

# ---------------------------------------------------------------- 引擎单测


@pytest.fixture()
def db() -> DBController:
    d = DBController(settings, client=mongomock.MongoClient())
    d.ensure_indexes()
    return d


def _tournament(db: DBController, n: int, seed: bool = False) -> Tournament:
    players = [f"p{i}" for i in range(n)]
    t = Tournament(
        name="T",
        format=TournamentFormat.SINGLE_ELIM,
        created_by="admin",
        participant_ids=players,
    )
    if seed:
        t.seed_order = list(players)
    db.tournaments.insert(t)
    return t


def test_seed_slots_standard_order() -> None:
    assert _seed_slots(2) == [1, 2]
    assert _seed_slots(4) == [1, 4, 2, 3]
    assert _seed_slots(8) == [1, 8, 4, 5, 2, 7, 3, 6]


def test_single_elim_4_players_no_bye(db: DBController) -> None:
    t = _tournament(db, 4, seed=True)
    eng = TournamentEngine(db)
    first = eng.generate(t)
    assert len(first) == 2
    fixtures = db.fixtures.find_by_tournament(t.id)
    assert len(fixtures) == 3  # 2 首轮 + 1 决赛
    assert all(not f.is_bye for f in fixtures)
    final = next(f for f in fixtures if f.advances_to is None)
    assert final.round_no == 2
    assert final.player_a_id is None and final.player_b_id is None  # 待首轮胜者
    t2 = db.tournaments.get(t.id)
    assert t2 is not None
    assert t2.status == TournamentStatus.IN_PROGRESS
    assert t2.total_rounds == 2


def test_single_elim_6_players_two_byes(db: DBController) -> None:
    t = _tournament(db, 6, seed=True)
    TournamentEngine(db).generate(t)
    fixtures = db.fixtures.find_by_tournament(t.id)
    first = [f for f in fixtures if f.round_no == 1]
    assert len(first) == 4
    assert len([f for f in first if f.is_bye]) == 2  # bracket_size=8 → 2 bye
    # bye 的胜者已填入第 2 轮对应槽
    completed = [f for f in first if f.is_bye and f.winner_id]
    assert len(completed) == 2
    round2 = [f for f in fixtures if f.round_no == 2]
    filled = sum(
        1 for f in round2 if f.player_a_id is not None or f.player_b_id is not None
    )
    assert filled == 2  # 两个 bye 胜者各填一个槽
    t2 = db.tournaments.get(t.id)
    assert t2 is not None and t2.total_rounds == 3


def test_single_elim_seed_determines_matchup(db: DBController) -> None:
    t = _tournament(db, 4, seed=True)  # seed_order=[p0,p1,p2,p3]
    TournamentEngine(db).generate(t)
    first = sorted(
        [f for f in db.fixtures.find_by_tournament(t.id) if f.round_no == 1],
        key=lambda f: f.match_index,
    )
    # bracket_size=4 slots=[1,4,2,3] → 槽0=p0,槽1=p3 / 槽2=p1,槽3=p2
    assert (first[0].player_a_id, first[0].player_b_id) == ("p0", "p3")
    assert (first[1].player_a_id, first[1].player_b_id) == ("p1", "p2")


async def test_single_elim_advance_and_complete(db: DBController) -> None:
    t = _tournament(db, 4, seed=True)
    eng = TournamentEngine(db)
    eng.generate(t)
    first = sorted(
        [f for f in db.fixtures.find_by_tournament(t.id) if f.round_no == 1],
        key=lambda f: f.match_index,
    )
    # 指派裁判 + 实例化 + 模拟 A 胜
    for f in first:
        f.referee_id = "ref"
        db.fixtures.replace(f)
        session = eng.materialize_match(f, _mk_body(), _mappool())
        await eng.on_match_ended(session, "A")
    final = next(
        f for f in db.fixtures.find_by_tournament(t.id) if f.advances_to is None
    )
    assert final.player_a_id is not None and final.player_b_id is not None
    assert final.status == FixtureStatus.READY
    # 打决赛：A 胜
    final.referee_id = "ref"
    db.fixtures.replace(final)
    fs = eng.materialize_match(final, _mk_body(), _mappool())
    await eng.on_match_ended(fs, "A")
    t2 = db.tournaments.get(t.id)
    assert t2 is not None
    assert t2.status == TournamentStatus.COMPLETED
    assert t2.winner_id == final.player_a_id
    assert t2.final_standings is not None
    champion = next(s for s in t2.final_standings if s.rank == 1)
    assert champion.account_id == final.player_a_id
    assert champion.note == "冠军"


# -------------------------------------------------------- REST 端点测试


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
    app = create_app(db=db)
    with TestClient(app) as client:
        token = client.post(
            "/auth/login", json={"username": "admin", "password": "a"}
        ).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        mid = client.post(
            "/admin/mappools",
            json={"name": "M", "mappool": {"categories": []}},
            headers=h,
        ).json()["id"]
        body: dict[str, Any] = {"name": "T", "format": 1}
        tid = client.post("/admin/tournaments", json=body, headers=h).json()["id"]
        client.post(
            f"/admin/tournaments/{tid}/participants",
            json={"usernames": ["p0", "p1", "p2", "p3"]},
            headers=h,
        )
        client.post(
            f"/admin/tournaments/{tid}/referees", json={"usernames": ["ref"]}, headers=h
        )
        return SimpleNamespace(
            client=client, db=db, token=token, tid=tid, mid=mid, ref=ref
        )


def test_generate_bracket_and_assign_flow(env: SimpleNamespace) -> None:
    h = {"Authorization": f"Bearer {env.token}"}
    tid = env.tid
    # 生成对阵
    resp = env.client.post(f"/admin/tournaments/{tid}/generate-bracket", headers=h)
    assert resp.status_code == 200, resp.text
    bracket = resp.json()
    assert bracket["format"] == 1
    assert bracket["total_rounds"] == 2
    assert len(bracket["rounds"]) == 2
    # 查看 bracket
    assert (
        env.client.get(f"/admin/tournaments/{tid}/bracket", headers=h).status_code
        == 200
    )
    # 首轮第一场 fixture id
    first_fix = bracket["rounds"][0]["fixtures"][0]
    fid = first_fix["id"]
    # 指派裁判
    resp = env.client.post(
        f"/admin/tournaments/{tid}/fixtures/{fid}/assign",
        json={"referee": "ref"},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["referee_id"] == env.ref.id
    # 生成实战比赛
    resp = env.client.post(
        f"/admin/tournaments/{tid}/fixtures/{fid}/create-match",
        json=_match_body(env.mid),
        headers=h,
    )
    assert resp.status_code == 201, resp.text
    session = resp.json()
    assert session["tournament_id"] == tid
    assert session["fixture_id"] == fid
    # 重复生成 → 400
    assert (
        env.client.post(
            f"/admin/tournaments/{tid}/fixtures/{fid}/create-match",
            json=_match_body(env.mid),
            headers=h,
        ).status_code
        == 400
    )
    # 未指派裁判的对阵生成比赛 → 400
    other_fid = bracket["rounds"][0]["fixtures"][1]["id"]
    assert (
        env.client.post(
            f"/admin/tournaments/{tid}/fixtures/{other_fid}/create-match",
            json=_match_body(env.mid),
            headers=h,
        ).status_code
        == 400
    )
    # 排名端点可调（赛事未完，返回部分）
    assert (
        env.client.get(f"/admin/tournaments/{tid}/standings", headers=h).status_code
        == 200
    )


def test_generate_bracket_validations(env: SimpleNamespace) -> None:
    h = {"Authorization": f"Bearer {env.token}"}
    tid = env.tid
    # 已生成过 → 400（先成功生成一次）
    env.client.post(f"/admin/tournaments/{tid}/generate-bracket", headers=h)
    assert (
        env.client.post(
            f"/admin/tournaments/{tid}/generate-bracket", headers=h
        ).status_code
        == 400
    )


def _mk_body():
    """单场规则 body（SimpleNamespace，materialize_match 访问其字段）。"""
    from types import SimpleNamespace

    from twilightcupbackend.datatypes import ScoringMethod

    return SimpleNamespace(
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        ban_count=1,
        protect_count=1,
    )


def _mappool():
    from twilightcupbackend.datatypes import Mappool

    return Mappool(categories=[])


def _match_body(mid: str) -> dict[str, object]:
    return {"bo_format": 3, "scoring_method": 1, "mappool_id": mid}
