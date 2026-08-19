"""比赛归档（archived_at）测试：归档/取消归档端点与列表行为。

归档是纯列表整理功能：与状态机正交，/admin/matches 仍含已归档行，
/me/matches 不再下发。
"""

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
    MatchStatus,
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
            )
        ]
    )


@pytest.fixture()
def env():  # type: ignore[no-untyped-def]
    db = DBController(settings, client=mongomock.MongoClient())
    db.ensure_indexes()

    def acct(u: str, role: AccountType, d: str) -> Account:
        a = Account(
            username=u, password_hash=hash_password("pw"), roles=[role], display_name=d
        )
        db.accounts.insert(a)
        return a

    pa = acct("pa", AccountType.PLAYER, "A")
    pb = acct("pb", AccountType.PLAYER, "B")
    ref = acct("ref", AccountType.REFEREE, "Ref")
    dri = acct("dri", AccountType.DIRECTOR, "Dir")
    admin = acct("admin", AccountType.ADMIN, "Admin")

    def mk(name: str, status: MatchStatus) -> Match:
        s = Match(
            name=name,
            bo_format=3,
            win_threshold=2,
            scoring_method=ScoringMethod.FASTEST,
            start_countdown_delay=2,
            mappool=_mp(),
            player_a_id=pa.id,
            player_b_id=pb.id,
            referee_id=ref.id,
            director_id=dri.id,
            status=status,
        )
        db.matches.insert(s)
        return s

    s_ended = mk("已结束场", MatchStatus.ENDED)
    s_running = mk("进行中场", MatchStatus.RUNNING)
    s_created = mk("未开始场", MatchStatus.CREATED)

    app = create_app(db=db)
    with TestClient(app) as client:
        yield client, db, (s_ended, s_running, s_created), (pa, ref, admin)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_archive_ended_then_lists(env) -> None:  # type: ignore[no-untyped-def]
    """归档 ENDED 比赛 → 200；管理列表带 archived_at；/me/matches 不再下发。"""
    client, db, (s_ended, _, _), (pa, ref, admin) = env
    tok_admin = issue_token(admin, settings)
    resp = client.post(f"/admin/matches/{s_ended.id}/archive", headers=_auth(tok_admin))
    assert resp.status_code == 200
    out = resp.json()
    assert out["id"] == s_ended.id
    assert out["archived_at"] is not None
    # 管理端响应须解析双方用户名（供前端按选手搜索；db 须传入 from_match）
    assert out["player_a_username"] == "pa"
    assert out["player_b_username"] == "pb"
    # 状态机不受影响
    assert out["status"] == MatchStatus.ENDED
    assert db.matches.get(s_ended.id).archived_at is not None

    # 管理列表仍含该行且带 archived_at
    resp = client.get("/admin/matches", headers=_auth(tok_admin))
    assert resp.status_code == 200
    row = next(m for m in resp.json() if m["id"] == s_ended.id)
    assert row["archived_at"] is not None
    assert row["player_a_username"] == "pa"

    # 选手/裁判「我的比赛」不再包含
    for account in (pa, ref):
        resp = client.get("/me/matches", headers=_auth(issue_token(account, settings)))
        assert resp.status_code == 200
        assert s_ended.id not in {m["id"] for m in resp.json()}


def test_unarchive_restores_me_matches(env) -> None:  # type: ignore[no-untyped-def]
    """取消归档 → archived_at 置空，/me/matches 重新包含该行。"""
    client, _, (s_ended, _, _), (_, ref, admin) = env
    tok_admin = issue_token(admin, settings)
    assert (
        client.post(
            f"/admin/matches/{s_ended.id}/archive", headers=_auth(tok_admin)
        ).status_code
        == 200
    )
    resp = client.post(
        f"/admin/matches/{s_ended.id}/unarchive", headers=_auth(tok_admin)
    )
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is None

    resp = client.get("/me/matches", headers=_auth(issue_token(ref, settings)))
    assert resp.status_code == 200
    assert s_ended.id in {m["id"] for m in resp.json()}


def test_archive_requires_ended(env) -> None:  # type: ignore[no-untyped-def]
    """非 ENDED（RUNNING/CREATED）比赛归档 → 400，错误体带 msg。"""
    client, _, (_, s_running, s_created), (_, _, admin) = env
    tok = _auth(issue_token(admin, settings))
    for s in (s_running, s_created):
        resp = client.post(f"/admin/matches/{s.id}/archive", headers=tok)
        assert resp.status_code == 400
        assert resp.json()["msg"] == "仅已结束的比赛可归档"


def test_archive_twice_400(env) -> None:  # type: ignore[no-untyped-def]
    """重复归档 → 400。"""
    client, _, (s_ended, _, _), (_, _, admin) = env
    tok = _auth(issue_token(admin, settings))
    first = client.post(f"/admin/matches/{s_ended.id}/archive", headers=tok)
    assert first.status_code == 200
    resp = client.post(f"/admin/matches/{s_ended.id}/archive", headers=tok)
    assert resp.status_code == 400
    assert resp.json()["msg"] == "比赛已归档"


def test_unarchive_not_archived_400(env) -> None:  # type: ignore[no-untyped-def]
    """未归档比赛取消归档 → 400。"""
    client, _, (s_ended, _, _), (_, _, admin) = env
    resp = client.post(
        f"/admin/matches/{s_ended.id}/unarchive",
        headers=_auth(issue_token(admin, settings)),
    )
    assert resp.status_code == 400
    assert resp.json()["msg"] == "比赛未归档"


def test_archive_unarchive_not_found_404(env) -> None:  # type: ignore[no-untyped-def]
    """不存在的比赛 → 404。"""
    client, _, _, (_, _, admin) = env
    tok = _auth(issue_token(admin, settings))
    for path in ("/admin/matches/nope/archive", "/admin/matches/nope/unarchive"):
        resp = client.post(path, headers=tok)
        assert resp.status_code == 404


def test_archive_requires_admin(env) -> None:  # type: ignore[no-untyped-def]
    """非管理员（裁判）调用归档端点 → 403。"""
    client, _, (s_ended, _, _), (_, ref, _) = env
    tok = _auth(issue_token(ref, settings))
    for path in (
        f"/admin/matches/{s_ended.id}/archive",
        f"/admin/matches/{s_ended.id}/unarchive",
    ):
        resp = client.post(path, headers=tok)
        assert resp.status_code == 403


def test_admin_list_carries_archived_at_field(env) -> None:  # type: ignore[no-untyped-def]
    """未归档比赛在 /admin/matches 中 archived_at 为 null（旧数据无需迁移）。"""
    client, _, (s_ended, _, _), (_, _, admin) = env
    resp = client.get("/admin/matches", headers=_auth(issue_token(admin, settings)))
    assert resp.status_code == 200
    row = next(m for m in resp.json() if m["id"] == s_ended.id)
    assert row["archived_at"] is None
