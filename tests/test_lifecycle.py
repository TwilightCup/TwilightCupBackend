"""比赛比赛生命周期测试：RUNNING 激活、选手单场强制、结束自动断连、管理员强制结束。"""

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
    pc = acct("pc", AccountType.PLAYER, "C")
    pd = acct("pd", AccountType.PLAYER, "D")
    ref = acct("ref", AccountType.REFEREE, "Ref")
    ref2 = acct("ref2", AccountType.REFEREE, "Ref2")
    dri = acct("dri", AccountType.DIRECTOR, "Dir")
    admin = acct("admin", AccountType.ADMIN, "Admin")

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

    s1 = mk("上半区", pa, pb, ref)
    s2 = mk("下半区", pc, pd, ref2)

    app = create_app(db=db)
    with TestClient(app) as client:
        yield client, db, (pa, pb, pc, pd, ref, ref2, admin), (s1, s2)


def test_player_needs_running_match(env) -> None:  # type: ignore[no-untyped-def]
    """选手在比赛仅 CREATED（未激活）时连接 → auth_error；置 RUNNING 后可连入。"""
    client, db, (pa, _, _, _, _, _, _), (s1, _) = env
    tok = issue_token(pa, settings)
    with client.websocket_connect(f"/ws/{tok}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "auth_error"  # s1 未 RUNNING

    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    with client.websocket_connect(f"/ws/{tok}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "auth_ok"
        assert msg["seat"] == "PLAYER_A"


def test_begin_prep_activates_running(env) -> None:  # type: ignore[no-untyped-def]
    """裁判 mark_prep 首次激活 → 比赛变 RUNNING + started_at；二次不重复激活。"""
    client, db, (_, _, _, _, ref, _, _), (s1, _) = env
    tok = issue_token(ref, settings)
    with client.websocket_connect(f"/ws/{tok}?match={s1.id}") as ws_r:
        ws_r.receive_json()  # auth_ok
        ws_r.send_json({"type": "referee_mark_prep"})
        ws_r.receive_json()  # phase_change PREP
        ws_r.receive_json()  # system prep
        ws_r.receive_json()  # ready_state
    after = db.matches.get(s1.id)
    assert after.status == MatchStatus.RUNNING
    assert after.started_at is not None
    started = after.started_at
    # 二次 mark_prep（回合结束后）不重复激活
    with client.websocket_connect(f"/ws/{tok}?match={s1.id}") as ws_r:
        ws_r.receive_json()
        ws_r.send_json({"type": "referee_mark_prep"})
        ws_r.receive_json()
    after2 = db.matches.get(s1.id)
    assert after2.started_at == started  # 未变


def test_single_active_match_enforced(env) -> None:  # type: ignore[no-untyped-def]
    """选手 pa 已在 s1（RUNNING）→ 含 pa 的 s3 begin_prep 被单场规则拒绝。"""
    client, db, (pa, pb, _, _, _, ref2, _), (s1, _) = env
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    # s3 复用 pa（选手A 正在 s1），裁判 ref2
    s3 = Match(
        name="冲突场",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=_mp(),
        player_a_id=pa.id,
        player_b_id=pb.id,
        referee_id=ref2.id,
        director_id=s1.director_id,
    )
    db.matches.insert(s3)
    tok = issue_token(ref2, settings)
    with client.websocket_connect(f"/ws/{tok}?match={s3.id}") as ws_r:
        for _ in range(5):  # auth_ok / ready_state / phase_change / seat_state×2
            ws_r.receive_json()
        ws_r.send_json({"type": "referee_mark_prep"})
        msg = ws_r.receive_json()
        # 激活被拒：下发 error（400）
        assert msg["type"] == "error"
        assert msg["code"] == 400
    # s3 仍未激活
    assert db.matches.get(s3.id).status == MatchStatus.CREATED


def test_match_end_kicks_players(env) -> None:  # type: ignore[no-untyped-def]
    """比赛结束（这里走管理员强制结束）触发 kick_players，选手连接被关闭。"""
    client, db, (pa, _, _, _, _, _, admin), (s1, _) = env
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    pa_tok = issue_token(pa, settings)
    admin_tok = issue_token(admin, settings)
    with client.websocket_connect(f"/ws/{pa_tok}") as ws_pa:
        ws_pa.receive_json()  # auth_ok
        resp = client.post(
            f"/admin/matches/{s1.id}/end",
            headers={"Authorization": f"Bearer {admin_tok}"},
        )
        assert resp.status_code == 200
        # 服务端先广播 system + phase(MATCH_END)，再踢人 → 排空后 receive 触发断开
        from starlette.websockets import WebSocketDisconnect

        disconnected = False
        try:
            for _ in range(10):
                ws_pa.receive_json()
        except WebSocketDisconnect:
            disconnected = True
        assert disconnected


def test_admin_force_end(env) -> None:  # type: ignore[no-untyped-def]
    """管理员强制结束：RUNNING → ENDED。"""
    client, db, (_, _, _, _, _, _, admin), (s1, _) = env
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    tok = issue_token(admin, settings)
    resp = client.post(
        f"/admin/matches/{s1.id}/end",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 200
    assert db.matches.get(s1.id).status == MatchStatus.ENDED


def test_admin_force_end_with_score_sets_winner(env) -> None:  # type: ignore[no-untyped-def]
    """有领先比分时强制结束走完整流程：记录 winner 与 final_result。"""
    client, db, (pa, pb, _, _, ref, _, admin), (s1, _) = env
    s1.status = MatchStatus.RUNNING
    db.matches.replace(s1)
    tok_ref = issue_token(ref, settings)
    tok_admin = issue_token(admin, settings)
    with (
        client.websocket_connect(f"/ws/{tok_ref}") as ws_r,
        client.websocket_connect(f"/ws/{issue_token(pa, settings)}") as ws_a,
        client.websocket_connect(f"/ws/{issue_token(pb, settings)}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            for _ in range(5):
                ws.receive_json()
        # 打一回合判 A 胜（1:0，未达阈值 2）→ 自动停在 ROUND_END（未结束）
        ws_r.send_json({"type": "referee_mark_prep"})
        for _ in range(2):
            ws_r.receive_json()
        ws_r.send_json({"type": "referee_select_pick", "pick_code": "ML1"})
        ws_r.receive_json()
        ws_r.send_json({"type": "referee_manual_start"})

        def _recv_until(ws, predicate, max_msgs: int = 30):  # type: ignore[no-untyped-def]
            for _ in range(max_msgs):
                m = ws.receive_json()
                if predicate(m):
                    return m
            raise AssertionError("未在限定消息内匹配到目标")

        rid = _recv_until(ws_a, lambda m: m["type"] == "round_start")["round_id"]
        for ws in (ws_a, ws_b):
            ws.send_json(
                {"type": "project_complete", "round_id": rid, "final_total_ms": 1000}
            )
        _recv_until(
            ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 4
        )
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 1}  # A_WIN
        )
        _recv_until(ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 5)

        resp = client.post(
            f"/admin/matches/{s1.id}/end",
            headers={"Authorization": f"Bearer {tok_admin}"},
        )
        assert resp.status_code == 200
        ended = db.matches.get(s1.id)
        assert ended.status == MatchStatus.ENDED
        assert ended.winner == "A"  # 领先方胜
        log = db.match_logs.get_by_match(s1.id)
        assert log is not None and log.final_result == {
            "winner": "A",
            "wins_a": 1,
            "wins_b": 0,
        }
        # 完整流程同样踢选手（排空收尾广播后触发断开）
        from starlette.websockets import WebSocketDisconnect

        disconnected = False
        try:
            for _ in range(50):
                ws_a.receive_json()
        except WebSocketDisconnect:
            disconnected = True
        assert disconnected


def test_referee_start_activates(env) -> None:  # type: ignore[no-untyped-def]
    """裁判 POST /me/matches/{id}/start：
    CREATED → RUNNING + started_at；已 RUNNING 幂等。"""
    client, db, (_, _, _, _, ref, _, _), (s1, _) = env
    assert db.matches.get(s1.id).status == MatchStatus.CREATED
    tok = issue_token(ref, settings)
    resp = client.post(
        f"/me/matches/{s1.id}/start",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == MatchStatus.RUNNING
    assert db.matches.get(s1.id).started_at is not None
    # 幂等：再次调用仍 200
    resp2 = client.post(
        f"/me/matches/{s1.id}/start",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp2.status_code == 200


def test_start_rejects_non_referee(env) -> None:  # type: ignore[no-untyped-def]
    """非该场裁判（如选手）调用 start → 403。"""
    client, _, (pa, _, _, _, _, _, _), (s1, _) = env
    tok = issue_token(pa, settings)
    resp = client.post(
        f"/me/matches/{s1.id}/start",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 403
