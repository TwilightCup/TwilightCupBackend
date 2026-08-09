"""赛程引擎钩子测试：create_app 装配 + on_match_ended 幂等/忽略。"""

from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import (
    FixtureStatus,
    Mappool,
    Match,
    ScoringMethod,
    Tournament,
    TournamentFormat,
)
from twilightcupbackend.main import create_app
from twilightcupbackend.tournament_engine import TournamentEngine


@pytest.fixture()
def env():  # type: ignore[no-untyped-def]
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    app = create_app(db=db)
    with TestClient(app) as client:
        yield db, app, client


def test_engine_wired_in_create_app(env) -> None:  # type: ignore[no-untyped-def]
    _, app, _ = env
    cm = app.state.connection_manager
    assert cm.tournament_engine is not None
    assert cm.match_engine is not None
    assert isinstance(cm.tournament_engine, TournamentEngine)


async def test_on_match_ended_ignores_non_tournament(env) -> None:  # type: ignore[no-untyped-def]
    _, app, _ = env
    engine = app.state.connection_manager.tournament_engine
    assert engine is not None
    # 非赛事对决（tournament_id/fixture_id 均为 None）→ 直接返回，无副作用
    session = Match(
        name="x",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=Mappool(categories=[]),
        player_a_id="a",
        player_b_id="b",
        referee_id="r",
        director_id="d",
    )
    await engine.on_match_ended(session, "A")  # 不应抛异常


async def test_on_match_ended_idempotent(env) -> None:  # type: ignore[no-untyped-def]
    db, app, _ = env
    engine = app.state.connection_manager.tournament_engine
    assert engine is not None
    t = Tournament(
        name="T",
        format=TournamentFormat.SINGLE_ELIM,
        created_by="admin",
        participant_ids=["a", "b", "c", "d"],
        seed_order=["a", "b", "c", "d"],
    )
    db.tournaments.insert(t)
    engine.generate(t)
    first = sorted(
        [f for f in db.fixtures.find_by_tournament(t.id) if f.round_no == 1],
        key=lambda f: f.match_index,
    )[0]
    first.referee_id = "r"
    db.fixtures.replace(first)
    session = engine.materialize_match(first, _mk_body(), _mappool())

    await engine.on_match_ended(session, "A")
    f1 = db.fixtures.get(first.id)
    assert f1 is not None
    assert f1.status == FixtureStatus.COMPLETED
    assert f1.winner_id == first.player_a_id

    # 重复触发：不应重复推进或报错
    await engine.on_match_ended(session, "A")
    f2 = db.fixtures.get(first.id)
    assert f2 is not None
    assert f2.winner_id == first.player_a_id  # 不变
    # 目标节点槽位只填一次（match_index=0 → slot A）
    target = db.fixtures.get(f1.advances_to)
    assert target is not None
    assert target.player_a_id == first.player_a_id


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
