"""默认赛事（孤立比赛容器）测试：seed 幂等、挂靠、守卫、永不结束、存量回填。"""

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
    DEFAULT_TOURNAMENT_ID,
    Account,
    AccountType,
    Mappool,
    Match,
    ScoringMethod,
    Tournament,
    TournamentFormat,
    TournamentStatus,
)
from twilightcupbackend.main import create_app


def _mappool_body(name: str = "M1") -> dict[str, Any]:
    return {
        "name": name,
        "mappool": {
            "categories": [
                {
                    "name": "ML",
                    "picks": [
                        {
                            "code": "ML1",
                            "name": "测试",
                            "type": 1,
                            "collection": {"raw": {}},
                            "category": "ML",
                        }
                    ],
                }
            ]
        },
    }


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
    for i in range(2):
        db.accounts.insert(
            Account(
                username=f"p{i}",
                password_hash=hash_password("p"),
                roles=[AccountType.PLAYER],
                display_name=f"选手{i}",
            )
        )
    # 与任何比赛无关的旁观账号（门控 403 用）
    db.accounts.insert(
        Account(
            username="other",
            password_hash=hash_password("p"),
            roles=[AccountType.PLAYER],
            display_name="无关选手",
        )
    )
    db.accounts.insert(
        Account(
            username="ref",
            password_hash=hash_password("p"),
            roles=[AccountType.REFEREE],
            display_name="裁判",
        )
    )
    db.accounts.insert(
        Account(
            username="dri",
            password_hash=hash_password("p"),
            roles=[AccountType.DIRECTOR],
            display_name="导播",
        )
    )

    app = create_app(db=db)  # 传 db → own_db=False，lifespan 不做 seed（测兜底路径）
    with TestClient(app) as client:
        token = client.post(
            "/auth/login", json={"username": "admin", "password": "a"}
        ).json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}
        mid = client.post("/admin/mappools", json=_mappool_body(), headers=h).json()[
            "id"
        ]
        return SimpleNamespace(client=client, db=db, token=token, mid=mid, app=app)


def _h(env: SimpleNamespace) -> dict[str, str]:
    return {"Authorization": f"Bearer {env.token}"}


def _match_body(env: SimpleNamespace) -> dict[str, Any]:
    return {
        "name": "孤立赛",
        "bo_format": 3,
        "scoring_method": 1,  # FASTEST
        "player_a": "p0",
        "player_b": "p1",
        "referee": "ref",
        "director": "dri",
        "mappool_id": env.mid,
    }


def test_seed_idempotent(env: SimpleNamespace) -> None:
    env.db.ensure_default_tournament()
    env.db.ensure_default_tournament()  # 重复调用幂等
    t = env.db.tournaments.get(DEFAULT_TOURNAMENT_ID)
    assert t is not None
    assert t.name == "黄昏杯"
    assert t.status == TournamentStatus.DRAFT
    assert t.participant_ids == []


def test_rename_legacy_default_tournament(env: SimpleNamespace) -> None:
    """存量迁移：旧 seed 名「默认赛事」→「黄昏杯」；幂等；自定义名不覆盖。"""
    env.db.tournaments.insert(
        Tournament(
            id=DEFAULT_TOURNAMENT_ID,
            name="默认赛事",
            format=TournamentFormat.SINGLE_ELIM,
            created_by="system",
        )
    )
    env.db.ensure_default_tournament()
    t = env.db.tournaments.get(DEFAULT_TOURNAMENT_ID)
    assert t is not None
    assert t.name == "黄昏杯"
    # 已改过名再跑：幂等不动
    env.db.ensure_default_tournament()
    t = env.db.tournaments.get(DEFAULT_TOURNAMENT_ID)
    assert t is not None
    assert t.name == "黄昏杯"
    # 非旧名（如管理员手动改过）：不得覆盖
    t.name = "自定义名"
    env.db.tournaments.replace(t)
    env.db.ensure_default_tournament()
    t = env.db.tournaments.get(DEFAULT_TOURNAMENT_ID)
    assert t is not None
    assert t.name == "自定义名"


def test_standalone_match_attaches_default(env: SimpleNamespace) -> None:
    # 未走 lifespan seed，验证 POST /admin/matches 的兜底 get-or-create
    assert env.db.tournaments.get(DEFAULT_TOURNAMENT_ID) is None
    resp = env.client.post("/admin/matches", json=_match_body(env), headers=_h(env))
    assert resp.status_code == 201, resp.text
    assert resp.json()["tournament_id"] == DEFAULT_TOURNAMENT_ID
    assert env.db.tournaments.get(DEFAULT_TOURNAMENT_ID) is not None


def test_default_tournament_mutations_rejected(env: SimpleNamespace) -> None:
    env.db.ensure_default_tournament()
    h = _h(env)
    tid = DEFAULT_TOURNAMENT_ID
    # 读操作不受影响
    assert env.client.get(f"/admin/tournaments/{tid}", headers=h).status_code == 200
    # 全部变更端点 → 400「默认赛事不允许该操作」
    cases: list[tuple[str, str, dict[str, Any] | None]] = [
        ("PATCH", f"/admin/tournaments/{tid}", {"name": "改 名"}),
        ("DELETE", f"/admin/tournaments/{tid}", None),
        ("POST", f"/admin/tournaments/{tid}/participants", {"usernames": ["p0"]}),
        (
            "POST",
            f"/admin/tournaments/{tid}/participants/remove",
            {"usernames": ["p0"]},
        ),
        ("POST", f"/admin/tournaments/{tid}/referees", {"usernames": ["ref"]}),
        ("POST", f"/admin/tournaments/{tid}/directors", {"usernames": ["dri"]}),
        ("POST", f"/admin/tournaments/{tid}/seeds", {"seed_order": ["p0"]}),
        ("POST", f"/admin/tournaments/{tid}/generate-bracket", None),
        ("POST", f"/admin/tournaments/{tid}/next-round", None),
    ]
    for method, url, body in cases:
        resp = env.client.request(method, url, json=body, headers=h)
        assert resp.status_code == 400, f"{method} {url} → {resp.status_code}"
        assert resp.json()["msg"] == "默认赛事不允许该操作"
    # 赛事仍存在且未被改动
    t = env.db.tournaments.get(tid)
    assert t is not None
    assert t.name == "黄昏杯"


async def test_match_end_does_not_complete_default(env: SimpleNamespace) -> None:
    env.db.ensure_default_tournament()
    engine = env.app.state.connection_manager.tournament_engine
    assert engine is not None
    session = Match(
        name="孤立赛",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=Mappool(categories=[]),
        player_a_id="a",
        player_b_id="b",
        referee_id="r",
        director_id="d",
        tournament_id=DEFAULT_TOURNAMENT_ID,
    )
    await engine.on_match_ended(session, "A")  # 不应抛异常
    t = env.db.tournaments.get(DEFAULT_TOURNAMENT_ID)
    assert t is not None
    assert t.status == TournamentStatus.DRAFT  # 永不结束
    assert t.winner_id is None
    assert t.completed_at is None


def test_backfill_legacy_standalone_matches(env: SimpleNamespace) -> None:
    legacy = Match(
        name="存量孤立赛",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=5,
        mappool=Mappool(categories=[]),
        player_a_id="a",
        player_b_id="b",
        referee_id="r",
        director_id="d",
    )
    env.db.matches.insert(legacy)
    env.db.ensure_default_tournament()
    m = env.db.matches.get(legacy.id)
    assert m is not None
    assert m.tournament_id == DEFAULT_TOURNAMENT_ID


def _login(env: SimpleNamespace, username: str) -> str:
    return env.client.post(
        "/auth/login", json={"username": username, "password": "p"}
    ).json()["access_token"]


def test_default_tournament_member_gate_by_match_role(env: SimpleNamespace) -> None:
    """默认赛事成员门控按比赛参与关系判定：比赛级选手/裁判/导播放行，无关账号 403。"""
    url = f"/me/tournaments/{DEFAULT_TOURNAMENT_ID}/bracket"
    env.db.ensure_default_tournament()
    # 尚无任何孤立比赛 → 参与者也是非成员
    dri_h = {"Authorization": f"Bearer {_login(env, 'dri')}"}
    assert env.client.get(url, headers=dri_h).status_code == 403

    resp = env.client.post("/admin/matches", json=_match_body(env), headers=_h(env))
    assert resp.status_code == 201, resp.text

    # 比赛级选手/裁判/导播均可读（空表）；无关账号仍 403
    for username in ("p0", "ref", "dri"):
        h = {"Authorization": f"Bearer {_login(env, username)}"}
        resp = env.client.get(url, headers=h)
        assert resp.status_code == 200, f"{username} → {resp.status_code}"
        assert resp.json()["rounds"] == []
    other_h = {"Authorization": f"Bearer {_login(env, 'other')}"}
    assert env.client.get(url, headers=other_h).status_code == 403


def test_me_tournament_detail_includes_default(env: SimpleNamespace) -> None:
    """GET /me/tournaments/{id}：比赛参与者可读默认赛事详情（名/状态），
    无关账号 403、不存在 404。默认赛事不在 /me/tournaments 列表里，
    导播舞台 Coming Soon 场景靠本端点取赛事名/状态。"""
    # 尚无比赛 → 导播非成员 403
    env.db.ensure_default_tournament()
    url = f"/me/tournaments/{DEFAULT_TOURNAMENT_ID}"
    dri_h = {"Authorization": f"Bearer {_login(env, 'dri')}"}
    assert env.client.get(url, headers=dri_h).status_code == 403

    resp = env.client.post("/admin/matches", json=_match_body(env), headers=_h(env))
    assert resp.status_code == 201, resp.text

    resp = env.client.get(url, headers=dri_h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == DEFAULT_TOURNAMENT_ID
    assert body["name"] == "黄昏杯"
    assert body["status"] == TournamentStatus.DRAFT

    # 无关账号 403；不存在 404
    other_h = {"Authorization": f"Bearer {_login(env, 'other')}"}
    assert env.client.get(url, headers=other_h).status_code == 403
    assert env.client.get("/me/tournaments/no-such", headers=dri_h).status_code == 404
