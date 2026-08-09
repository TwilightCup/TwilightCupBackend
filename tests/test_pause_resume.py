# ruff: noqa: RUF059
"""比赛暂停/恢复 + 选手跨会话占用冲突校验测试。

对应需求文档 backend-pause-resume.md：
- §2 状态机 PAUSED；§3 仅 RUNNING 占用选手；
- §4 pause/resume 接口；§5 冲突校验统一；§6 create 校验；§7.1 PATCH；
- §7.2 session_status（match_status）广播；§9 端到端验收。
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
    ref = acct("ref", AccountType.REFEREE, "裁判")
    dri = acct("dri", AccountType.DIRECTOR, "导播")
    admin = acct("admin", AccountType.ADMIN, "管理员")

    def mk(name: str, a: Account, b: Account, referee: Account) -> Match:
        s = Match(
            name=name,
            bo_format=3,
            win_threshold=2,
            scoring_method=ScoringMethod.FASTEST,
            start_countdown_delay=2,
            mappool=_mp(),
            player_a_id=a.id,
            player_b_id=b.id,
            referee_id=referee.id,
            director_id=dri.id,
        )
        db.matches.insert(s)
        return s

    s1 = mk("决赛A", pa, pb, ref)

    app = create_app(db=db)
    with TestClient(app) as client:
        yield client, db, (pa, pb, ref, dri, admin), s1


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# pause / resume 基本流程
# ---------------------------------------------------------------------------


def test_pause_requires_running(env) -> None:  # type: ignore[no-untyped-def]
    client, db, (_, _, ref, _, _), s1 = env
    # CREATED 不可暂停 → 409
    tok = issue_token(ref, settings)
    resp = client.post(f"/me/matches/{s1.id}/pause", headers=_auth(tok))
    assert resp.status_code == 409
    assert db.matches.get(s1.id).status == MatchStatus.CREATED


def test_pause_then_resume_releases_and_reoccupies(env) -> None:  # type: ignore[no-untyped-def]
    client, db, (pa, _, ref, _, _), s1 = env
    tok = issue_token(ref, settings)
    # 激活到 RUNNING
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)

    resp = client.post(f"/me/matches/{s1.id}/pause", headers=_auth(tok))
    assert resp.status_code == 200
    assert resp.json()["status"] == MatchStatus.PAUSED
    after = db.matches.get(s1.id)
    assert after.status == MatchStatus.PAUSED
    assert after.paused_at is not None
    # 暂停后选手不再被「进行中」占用
    assert db.matches.find_running_for_player(pa.id) == []

    # resume → RUNNING，选手重新被本场占用
    resp2 = client.post(f"/me/matches/{s1.id}/resume", headers=_auth(tok))
    assert resp2.status_code == 200
    assert resp2.json()["status"] == MatchStatus.RUNNING
    assert db.matches.get(s1.id).status == MatchStatus.RUNNING
    assert {m.id for m in db.matches.find_running_for_player(pa.id)} == {s1.id}


def test_resume_requires_paused(env) -> None:  # type: ignore[no-untyped-def]
    client, _, (_, _, ref, _, _), s1 = env
    tok = issue_token(ref, settings)
    # RUNNING 时 resume → 409
    resp = client.post(f"/me/matches/{s1.id}/resume", headers=_auth(tok))
    assert resp.status_code == 409


def test_pause_resume_non_referee_forbidden(env) -> None:  # type: ignore[no-untyped-def]
    client, db, (pa, _, _, _, _), s1 = env
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    tok = issue_token(pa, settings)
    assert (
        client.post(f"/me/matches/{s1.id}/pause", headers=_auth(tok)).status_code == 403
    )


def test_admin_can_pause(env) -> None:  # type: ignore[no-untyped-def]
    client, db, (_, _, _, _, admin), s1 = env
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    tok = issue_token(admin, settings)
    assert (
        client.post(f"/me/matches/{s1.id}/pause", headers=_auth(tok)).status_code == 200
    )


# ---------------------------------------------------------------------------
# 冲突校验
# ---------------------------------------------------------------------------


def test_resume_conflict_409_with_msg(env) -> None:  # type: ignore[no-untyped-def]
    """§9：S1 暂停后选手去打 S2(RUNNING)，恢复 S1 → 409，msg 指明选手与卡在哪场。"""
    client, db, (pa, pb, ref, dri, _), s1 = env
    ref_tok = issue_token(ref, settings)
    # S1 RUNNING → 暂停（释放 A/B）
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    assert (
        client.post(f"/me/matches/{s1.id}/pause", headers=_auth(ref_tok)).status_code
        == 200
    )

    # 建另一场 S2，复用 A、B（此时 A/B 无 RUNNING 占用 → 创建通过）
    s2 = Match(
        name="决赛B",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=_mp(),
        player_a_id=pa.id,
        player_b_id=pb.id,
        referee_id=ref.id,
        director_id=dri.id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(s2)

    # 恢复 S1 → A/B 在 S2 进行中 → 409 + msg
    resp = client.post(f"/me/matches/{s1.id}/resume", headers=_auth(ref_tok))
    assert resp.status_code == 409
    body = resp.json()
    assert "msg" in body
    assert "决赛B" in body["msg"]
    assert db.matches.get(s1.id).status == MatchStatus.PAUSED  # 未变


def test_start_rejects_paused_with_409(env) -> None:  # type: ignore[no-untyped-def]
    client, db, (_, _, ref, _, _), s1 = env
    s1.status = MatchStatus.PAUSED
    db.matches.replace(s1)
    tok = issue_token(ref, settings)
    resp = client.post(f"/me/matches/{s1.id}/start", headers=_auth(tok))
    assert resp.status_code == 409


def test_start_conflict_409(env) -> None:  # type: ignore[no-untyped-def]
    """选手已在另一场 RUNNING → start 被拒（409，原为 400）。"""
    client, db, (pa, pb, ref, dri, _), s1 = env
    # 让 pa 在另一场 RUNNING
    other = Match(
        name="占用场",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=_mp(),
        player_a_id=pa.id,
        player_b_id=pb.id,
        referee_id=ref.id,
        director_id=dri.id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(other)
    tok = issue_token(ref, settings)
    resp = client.post(f"/me/matches/{s1.id}/start", headers=_auth(tok))
    assert resp.status_code == 409
    assert "占用场" in resp.json()["msg"]


def test_create_conflict_409(env) -> None:  # type: ignore[no-untyped-def]
    """§6：创建时指定选手已在 RUNNING 会话 → 409。"""
    client, db, (pa, pb, ref, dri, admin), _ = env
    # 占用场：pa 正在 RUNNING
    other = Match(
        name="占用场",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=_mp(),
        player_a_id=pa.id,
        player_b_id=pb.id,
        referee_id=ref.id,
        director_id=dri.id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(other)
    admin_tok = issue_token(admin, settings)
    body = {
        "name": "新建场",
        "bo_format": 3,
        "scoring_method": ScoringMethod.FASTEST,
        "mappool": {"categories": []},
        "player_a": "pa",
        "player_b": "pb",
        "referee": "ref",
        "director": "dri",
    }
    resp = client.post("/admin/matches", json=body, headers=_auth(admin_tok))
    assert resp.status_code == 409
    assert "占用场" in resp.json()["msg"]


# ---------------------------------------------------------------------------
# PATCH（§7.1）
# ---------------------------------------------------------------------------


def test_patch_rename_and_status(env) -> None:  # type: ignore[no-untyped-def]
    client, db, (_, _, _, _, admin), s1 = env
    tok = issue_token(admin, settings)
    resp = client.patch(
        f"/admin/matches/{s1.id}",
        json={"name": "改名后", "status": int(MatchStatus.RUNNING)},
        headers=_auth(tok),
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["name"] == "改名后"
    assert out["status"] == MatchStatus.RUNNING
    assert db.matches.get(s1.id).started_at is not None


def test_patch_invalid_transition_409(env) -> None:  # type: ignore[no-untyped-def]
    client, _, (_, _, _, _, admin), s1 = env
    tok = issue_token(admin, settings)
    # CREATED → PAUSED 非法（须先 RUNNING）
    resp = client.patch(
        f"/admin/matches/{s1.id}",
        json={"status": int(MatchStatus.PAUSED)},
        headers=_auth(tok),
    )
    assert resp.status_code == 409


def test_patch_change_player_conflict_409(env) -> None:  # type: ignore[no-untyped-def]
    """PATCH 改 RUNNING 场的选手到一名已被占用者 → 409。"""
    client, db, (pa, pb, ref, dri, admin), s1 = env
    # 再建一名选手 pc，已被另一场 RUNNING 占用
    pc = Account(
        username="pc",
        password_hash=hash_password("pw"),
        roles=[AccountType.PLAYER],
        display_name="C",
    )
    db.accounts.insert(pc)
    other = Match(
        name="占用场",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=_mp(),
        player_a_id=pc.id,
        player_b_id=pb.id,
        referee_id=ref.id,
        director_id=dri.id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(other)
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    tok = issue_token(admin, settings)
    # 把 s1 的 A 换成 pc（pc 正在 other 进行中）→ 409
    resp = client.patch(
        f"/admin/matches/{s1.id}",
        json={"player_a": "pc"},
        headers=_auth(tok),
    )
    assert resp.status_code == 409
    assert "占用场" in resp.json()["msg"]


# ---------------------------------------------------------------------------
# WS：暂停守卫 + session_status 广播
# ---------------------------------------------------------------------------


def test_ws_paused_blocks_referee_prep(env) -> None:  # type: ignore[no-untyped-def]
    """暂停后裁判 referee_mark_prep 被拒（error 409）。"""
    client, db, (pa, _, ref, _, _), s1 = env
    s1.status = MatchStatus.PAUSED
    db.matches.replace(s1)
    tok = issue_token(ref, settings)
    with client.websocket_connect(f"/ws/{tok}?match={s1.id}") as ws:
        ws.receive_json()  # auth_ok
        ws.receive_json()  # ready_state
        ws.receive_json()  # phase_change
        ws.receive_json()  # seat_state A
        ws.receive_json()  # seat_state B
        ws.send_json({"type": "referee_mark_prep"})
        msg = ws.receive_json()
    assert msg["type"] == "error"
    assert msg["code"] == 409


def test_ws_pause_resume_broadcast_match_status(env) -> None:  # type: ignore[no-untyped-def]
    """§7.2：pause/resume 广播 match_status（3 / 1）。"""
    client, db, (pa, _, ref, _, _), s1 = env
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    ref_tok = issue_token(ref, settings)
    with client.websocket_connect(f"/ws/{ref_tok}?match={s1.id}") as ws:
        ws.receive_json()  # auth_ok
        ws.receive_json()  # ready_state
        ws.receive_json()  # phase_change
        ws.receive_json()  # seat_state A
        ws.receive_json()  # seat_state B
        # pause → 广播 system（暂停）+ match_status(3)
        assert (
            client.post(
                f"/me/matches/{s1.id}/pause", headers=_auth(ref_tok)
            ).status_code
            == 200
        )
        msgs = [ws.receive_json() for _ in range(2)]
        status_msgs = [m for m in msgs if m["type"] == "match_status"]
        assert status_msgs and status_msgs[-1]["status"] == MatchStatus.PAUSED


# ---------------------------------------------------------------------------
# §9 端到端验收
# ---------------------------------------------------------------------------


def test_acceptance_e2e(env) -> None:  # type: ignore[no-untyped-def]
    client, db, (pa, pb, ref, dri, admin), s1 = env
    admin_tok = issue_token(admin, settings)
    ref_tok = issue_token(ref, settings)

    # 1) start s1 → RUNNING
    assert (
        client.post(f"/me/matches/{s1.id}/start", headers=_auth(ref_tok)).status_code
        == 200
    )
    assert db.matches.get(s1.id).status == MatchStatus.RUNNING

    # 2) pause s1 → 释放 A/B
    assert (
        client.post(f"/me/matches/{s1.id}/pause", headers=_auth(ref_tok)).status_code
        == 200
    )
    assert db.matches.find_running_for_player(pa.id) == []

    # 3) 建 s2（A/B 已释放）→ 创建通过；start → A/B 占用于 s2
    body = {
        "name": "决赛B",
        "bo_format": 3,
        "scoring_method": ScoringMethod.FASTEST,
        "mappool": {"categories": []},
        "player_a": "pa",
        "player_b": "pb",
        "referee": "ref",
        "director": "dri",
    }
    resp = client.post("/admin/matches", json=body, headers=_auth(admin_tok))
    assert resp.status_code == 201, resp.text
    s2_id = resp.json()["id"]
    assert (
        client.post(f"/me/matches/{s2_id}/start", headers=_auth(ref_tok)).status_code
        == 200
    )
    assert {m.id for m in db.matches.find_running_for_player(pa.id)} == {s2_id}

    # 4) 恢复 s1 → A/B 在 s2 进行中 → 409
    resp = client.post(f"/me/matches/{s1.id}/resume", headers=_auth(ref_tok))
    assert resp.status_code == 409
    assert "决赛B" in resp.json()["msg"]

    # 5) 暂停 s2 后恢复 s1 成功；s1 历史（无）保留语义，状态 RUNNING
    assert (
        client.post(f"/me/matches/{s2_id}/pause", headers=_auth(ref_tok)).status_code
        == 200
    )
    assert (
        client.post(f"/me/matches/{s1.id}/resume", headers=_auth(ref_tok)).status_code
        == 200
    )
    assert db.matches.get(s1.id).status == MatchStatus.RUNNING
