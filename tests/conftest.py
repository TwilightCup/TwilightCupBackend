"""共享测试夹具：搭建一场完整的 mongomock 支撑比赛与各角色令牌。"""

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
    Level,
    Mappool,
    Match,
    MatchStatus,
    Pick,
    PickType,
    ScoringMethod,
)
from twilightcupbackend.main import create_app

# 测试关卡库（raw 按 level_id 引用；_expand_collection 下发时展开回名字）
_LEVEL_NAMES = ["L1", "L2", "C1", "E1", "P1"]


def _mappool(level_ids: dict[str, str]) -> Mappool:
    return Mappool(
        categories=[
            Category(
                name="ML",
                picks=[
                    Pick(
                        code="ML1",
                        name="测试关卡",
                        type=PickType.MULTI,
                        collection=CollectionConfig(
                            raw={"levels": [level_ids["L1"], level_ids["L2"]]}
                        ),
                        category="ML",
                    )
                ],
            ),
            Category(
                name="CT",
                picks=[
                    Pick(
                        code="CT01",
                        name="词条单关",
                        type=PickType.SINGLE,
                        collection=CollectionConfig(raw={"levels": [level_ids["C1"]]}),
                        category="CT",
                    )
                ],
            ),
            Category(
                name="EX",
                picks=[
                    Pick(
                        code="EX01",
                        name="工坊单关",
                        type=PickType.SINGLE,
                        collection=CollectionConfig(raw={"levels": [level_ids["E1"]]}),
                        category="EX",
                    )
                ],
            ),
            Category(
                name="CP",
                picks=[
                    Pick(
                        code="CP01",
                        name="存档点",
                        type=PickType.SINGLE,
                        retry_count=3,
                        collection=CollectionConfig(raw={"levels": [level_ids["P1"]]}),
                        category="CP",
                    )
                ],
            ),
        ]
    )


@pytest.fixture()
def world():  # type: ignore[no-untyped-def]
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()
    level_ids: dict[str, str] = {}
    for name in _LEVEL_NAMES:
        lv = Level(name=name, display_name=name)
        db.levels.insert(lv)
        level_ids[name] = lv.id

    def acct(username: str, role: AccountType, display: str) -> Account:
        a = Account(
            username=username,
            password_hash=hash_password("pw"),
            roles=[role],
            display_name=display,
        )
        db.accounts.insert(a)
        return a

    pa = acct("pa", AccountType.PLAYER, "选手A")
    pb = acct("pb", AccountType.PLAYER, "选手B")
    ref = acct("ref", AccountType.REFEREE, "裁判")
    dri = acct("dri", AccountType.DIRECTOR, "导播")
    session = Match(
        name="决赛",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=_mappool(level_ids),
        player_a_id=pa.id,
        player_b_id=pb.id,
        referee_id=ref.id,
        director_id=dri.id,
        status=MatchStatus.RUNNING,  # 选手连入需 RUNNING（已激活）
    )
    db.matches.insert(session)

    tokens = {
        "pa": issue_token(pa, settings),
        "pb": issue_token(pb, settings),
        "ref": issue_token(ref, settings),
        "dri": issue_token(dri, settings),
    }
    app = create_app(db=db)
    with TestClient(app) as client:
        yield client, db, session, tokens
