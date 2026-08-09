"""M2 持久化层测试：用 mongomock 验证 Repository 的 CRUD 与模型往返。

不依赖真实 MongoDB 服务；同时直接测试 model_to_doc/doc_to_model 映射（精确）。
"""

from __future__ import annotations

from typing import cast

import mongomock
import pytest
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from twilightcupbackend.databases import (
    Accounts,
    Matches,
    Rounds,
    doc_to_model,
    model_to_doc,
)
from twilightcupbackend.datatypes import (
    Account,
    AccountType,
    CollectionConfig,
    Mappool,
    Match,
    MatchStatus,
    Pick,
    PickType,
    PlayerRoundState,
    RoundRecord,
    ScoringMethod,
)


@pytest.fixture()
def db() -> Database:
    client = mongomock.MongoClient()
    return cast(Database, client["twilightcup_test"])


def _account(username: str = "player_a") -> Account:
    return Account(
        username=username,
        password_hash="hash",
        roles=[AccountType.PLAYER],
        display_name="选手A",
    )


def test_account_crud(db: Database) -> None:
    repo = Accounts(db)
    repo.collection.create_index("username", unique=True)

    acc = _account()
    inserted_id = repo.insert(acc)
    assert inserted_id == acc.id

    got = repo.get(acc.id)
    assert got is not None
    assert got.username == "player_a"
    assert AccountType.PLAYER in got.roles
    # 时间往返：比较 UTC 瞬时（mongomock 的 tz 实现可能归一化）
    assert got.created_at.utctimetuple() == acc.created_at.utctimetuple()

    by_name = repo.get_by_username("player_a")
    assert by_name is not None and by_name.id == acc.id
    assert repo.get_by_username("nope") is None

    repo.update_fields(acc.id, {"display_name": "选手甲"})
    updated = repo.get(acc.id)
    assert updated is not None and updated.display_name == "选手甲"

    assert repo.count() == 1
    repo.delete(acc.id)
    assert repo.get(acc.id) is None
    assert repo.count() == 0


def test_username_unique(db: Database) -> None:
    repo = Accounts(db)
    repo.collection.create_index("username", unique=True)
    repo.insert(_account("dup"))
    with pytest.raises(DuplicateKeyError):
        repo.insert(_account("dup"))


def test_round_record_nested_roundtrip(db: Database) -> None:
    """嵌套模型（PlayerRoundState/Pick/CollectionConfig）往返完整。"""
    pick = Pick(
        code="ML1",
        name="测试",
        type=PickType.MULTI,
        collection=CollectionConfig(raw={"levels": ["a", "b"]}),
        category="ML",
    )
    rec = RoundRecord(
        match_id="sess1",
        round_no=1,
        pick_code="ML1",
        pick_snapshot=pick,
        collection_snapshot=pick.collection,
        state_a=PlayerRoundState(account_id="A"),
        state_b=PlayerRoundState(account_id="B"),
    )
    repo = Rounds(db)
    repo.insert(rec)
    got = repo.get(rec.id)
    assert got is not None
    assert got.pick_snapshot.code == "ML1"
    assert got.pick_snapshot.collection.raw == {"levels": ["a", "b"]}
    assert got.state_a.account_id == "A"
    assert got.counted is True
    assert got.verdict is None

    # replace 整文档 upsert
    repo.replace(got)
    assert repo.count() == 1


def test_mapping_exact() -> None:
    acc = _account()
    doc = model_to_doc(acc)
    assert "id" not in doc and doc["_id"] == acc.id
    back = doc_to_model(Account, doc)
    assert back == acc


def test_session_find_by_member(db: Database) -> None:
    repo = Matches(db)
    sess = Match(
        name="决赛",
        bo_format=9,
        win_threshold=5,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=5,
        mappool=Mappool(),
        player_a_id="A",
        player_b_id="B",
        referee_id="R",
        director_id="D",
        status=MatchStatus.CREATED,
    )
    repo.insert(sess)
    found = repo.find_by_member("B")
    assert len(found) == 1 and found[0].id == sess.id
    assert repo.find_by_member("X") == []
