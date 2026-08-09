"""双败淘汰引擎测试：结构、败者下落、grand final、冠军、人数限制。"""

from __future__ import annotations

import mongomock
import pytest

from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.datatypes import (
    BracketSide,
    FixtureStatus,
    Tournament,
    TournamentFormat,
    TournamentStatus,
)
from twilightcupbackend.tournament_engine import TournamentEngine


@pytest.fixture()
def db() -> DBController:
    d = DBController(settings, client=mongomock.MongoClient())
    d.ensure_indexes()
    return d


def _tournament(db: DBController, n: int) -> Tournament:
    players = [f"p{i}" for i in range(n)]
    t = Tournament(
        name="T",
        format=TournamentFormat.DOUBLE_ELIM,
        created_by="admin",
        participant_ids=players,
        seed_order=list(players),
    )
    db.tournaments.insert(t)
    return t


def test_double_elim_8_structure(db: DBController) -> None:
    t = _tournament(db, 8)
    TournamentEngine(db).generate(t)
    fixtures = db.fixtures.find_by_tournament(t.id)
    winners = [f for f in fixtures if f.bracket_side == BracketSide.WINNERS]
    losers = [f for f in fixtures if f.bracket_side == BracketSide.LOSERS]
    gf = [f for f in fixtures if f.bracket_side == BracketSide.MAIN]
    assert len(winners) == 7  # 4 + 2 + 1
    assert len(losers) == 6  # 2 + 2 + 1 + 1
    assert len(gf) == 1
    # 胜者组首轮全 READY；GF 双方待定
    wr1 = [f for f in winners if f.round_no == 1]
    assert all(f.status == FixtureStatus.READY for f in wr1)
    assert gf[0].player_a_id is None and gf[0].player_b_id is None
    t2 = db.tournaments.get(t.id)
    assert t2 is not None and t2.status == TournamentStatus.IN_PROGRESS


def test_double_elim_requires_power_of_2(db: DBController) -> None:
    t = _tournament(db, 6)
    with pytest.raises(ValueError, match="2 的幂"):
        TournamentEngine(db).generate(t)


def test_double_elim_loser_drops(db: DBController) -> None:
    """胜者组首轮败者下落到败者组正确节点/slot。"""
    t = _tournament(db, 4)
    TournamentEngine(db).generate(t)
    fixtures = db.fixtures.find_by_tournament(t.id)
    wr1 = sorted(
        [
            f
            for f in fixtures
            if f.bracket_side == BracketSide.WINNERS and f.round_no == 1
        ],
        key=lambda f: f.match_index,
    )
    lr1 = [
        f for f in fixtures if f.bracket_side == BracketSide.LOSERS and f.round_no == 1
    ]
    assert len(lr1) == 1
    # 4 人：L_R1 唯一一场，双方来自 W_R1[0] 与 W_R1[1] 的败者
    assert wr1[0].losers_drops_to == lr1[0].id
    assert wr1[0].losers_drop_slot == "A"
    assert wr1[1].losers_drops_to == lr1[0].id
    assert wr1[1].losers_drop_slot == "B"


async def test_double_elim_4_grand_final(db: DBController) -> None:
    """4 人双败端到端：A 全胜 → 冠军 = 胜者组冠军。"""
    t = _tournament(db, 4)
    eng = TournamentEngine(db)
    eng.generate(t)
    # 循环打所有 READY，直到全部 COMPLETED
    while True:
        ready = [
            f
            for f in db.fixtures.find_by_tournament(t.id)
            if f.status == FixtureStatus.READY and f.match_id is None
        ]
        if not ready:
            break
        for f in ready:
            f.referee_id = "ref"
            db.fixtures.replace(f)
            session = eng.materialize_match(f, _mk_body(), _mappool())
            await eng.on_match_ended(session, "A")
    t2 = db.tournaments.get(t.id)
    assert t2 is not None
    assert t2.status == TournamentStatus.COMPLETED
    gf = next(
        f
        for f in db.fixtures.find_by_tournament(t.id)
        if f.bracket_side == BracketSide.MAIN
    )
    assert t2.winner_id == gf.winner_id
    # 所有非 bye 对阵均完成
    assert all(
        f.status == FixtureStatus.COMPLETED
        for f in db.fixtures.find_by_tournament(t.id)
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
