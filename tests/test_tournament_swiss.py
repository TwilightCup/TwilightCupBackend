"""瑞士轮引擎测试：首轮、bye、next-round 荷兰式配对 + 避免重赛、积分排名。"""

from __future__ import annotations

import mongomock
import pytest

from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import (
    FixtureStatus,
    Tournament,
    TournamentFormat,
)
from twilightcupbackend.tournament_engine import TournamentEngine


@pytest.fixture()
def db() -> DBController:
    d = DBController(settings, client=mongomock.MongoClient())
    d.ensure_indexes()
    return d


def _tournament(db: DBController, n: int, rounds: int | None = None) -> Tournament:
    players = [f"p{i}" for i in range(n)]
    t = Tournament(
        name="T",
        format=TournamentFormat.SWISS,
        created_by="admin",
        participant_ids=players,
        seed_order=list(players),
    )
    if rounds is not None:
        t.swiss_rounds = rounds
    db.tournaments.insert(t)
    return t


async def _play_ready(
    db: DBController, eng: TournamentEngine, t: Tournament, side: str = "A"
) -> None:
    """打完当前所有 READY 且未生成比赛的 fixtures。"""
    for f in db.fixtures.find_by_tournament(t.id):
        if f.status == FixtureStatus.READY and f.match_id is None:
            f.referee_id = "ref"
            db.fixtures.replace(f)
            s = eng.materialize_match(f, _mk_body(), _mappool())
            await eng.on_match_ended(s, side)


def test_swiss_first_round_8(db: DBController) -> None:
    t = _tournament(db, 8)
    first = TournamentEngine(db).generate(t)
    assert len(first) == 4  # 无 bye
    assert all(not f.is_bye for f in first)
    t2 = db.tournaments.get(t.id)
    assert t2 is not None
    assert t2.total_rounds == 3  # ceil(log2(8))


def test_swiss_first_round_5_has_bye(db: DBController) -> None:
    t = _tournament(db, 5)
    first = TournamentEngine(db).generate(t)
    byes = [f for f in first if f.is_bye]
    assert len(byes) == 1
    assert byes[0].winner_id == byes[0].player_a_id
    assert len([f for f in first if not f.is_bye]) == 2  # 2 场实战


async def test_swiss_next_round_avoids_rematch(db: DBController) -> None:
    t = _tournament(db, 8)
    eng = TournamentEngine(db)
    eng.generate(t)
    await _play_ready(db, eng, t, "A")
    round2 = eng.pair_swiss_round(t, 2)
    assert len(round2) == 4
    history = eng._opponent_history(t)
    # 第二轮每对选手不应在首轮交过手
    for f in round2:
        if f.is_bye:
            continue
        assert f.player_b_id not in history.get(f.player_a_id, set())


async def test_swiss_standings_scores(db: DBController) -> None:
    t = _tournament(db, 4)  # seed [p0,p1,p2,p3]，首轮 p0vp1 / p2vp3
    eng = TournamentEngine(db)
    eng.generate(t)
    await _play_ready(db, eng, t, "A")  # p0、p2 赢
    standings = eng.compute_standings(t)
    by_id = {s.account_id: s for s in standings}
    assert by_id["p0"].points == 1 and by_id["p0"].wins == 1
    assert by_id["p1"].points == 0
    assert by_id["p2"].points == 1 and by_id["p2"].wins == 1
    # 积分相同时 p0/p2 并列前两名
    assert standings[0].points >= standings[-1].points


async def test_swiss_full_tournament_completes(db: DBController) -> None:
    t = _tournament(db, 4, rounds=2)  # 固定 2 轮
    eng = TournamentEngine(db)
    eng.generate(t)
    # 第 1 轮
    await _play_ready(db, eng, t, "A")
    # 生成并打第 2 轮
    eng.pair_swiss_round(t, 2)
    await _play_ready(db, eng, t, "A")
    t2 = db.tournaments.get(t.id)
    assert t2 is not None
    assert t2.status.name == "COMPLETED"
    assert t2.winner_id is not None
    assert t2.final_standings is not None
    assert len(t2.final_standings) == 4


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
