"""M10 多比赛并发隔离测试（§4.2：比赛间严格隔离）。

用同一服务实例承载两场比赛，验证消息与状态不串。
"""

from __future__ import annotations

from twilightcupbackend.auth import hash_password, issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.datatypes import (
    Account,
    AccountType,
    Match,
    MatchStatus,
    ScoringMethod,
)


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def test_sessions_isolated(world) -> None:  # type: ignore[no-untyped-def]
    client, db, session1, tokens1 = world

    # 第二场比赛：全新账号，复用同一图池
    def acct(uname: str, role: AccountType, disp: str) -> Account:
        a = Account(
            username=uname,
            password_hash=hash_password("pw"),
            roles=[role],
            display_name=disp,
        )
        db.accounts.insert(a)
        return a

    pa2 = acct("pa2", AccountType.PLAYER, "选手A2")
    pb2 = acct("pb2", AccountType.PLAYER, "选手B2")
    ref2 = acct("ref2", AccountType.REFEREE, "裁判2")
    dri2 = acct("dri2", AccountType.DIRECTOR, "导播2")
    session2 = Match(
        name="另一场",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=session1.mappool,
        player_a_id=pa2.id,
        player_b_id=pb2.id,
        referee_id=ref2.id,
        director_id=dri2.id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(session2)
    tok2 = {
        "pa": issue_token(pa2, settings),
        "pb": issue_token(pb2, settings),
        "ref": issue_token(ref2, settings),
    }

    with (
        client.websocket_connect(f"/ws/{tokens1['pa']}") as ws_a1,
        client.websocket_connect(f"/ws/{tok2['pa']}") as ws_a2,
    ):
        _drain(ws_a1, 6)
        _drain(ws_a2, 6)
        # 比赛1 的选手发言
        ws_a1.send_json({"type": "chat", "text": "in-session-1"})
        echo1 = ws_a1.receive_json()
        assert echo1["type"] == "chat" and echo1["text"] == "in-session-1"
        # 比赛2 的选手发言；其收到的首条应为自己的消息（无比赛1泄漏）
        ws_a2.send_json({"type": "chat", "text": "in-session-2"})
        echo2 = ws_a2.receive_json()
        assert echo2["type"] == "chat"
        assert echo2["text"] == "in-session-2"  # 若隔离失效，这里会是 in-session-1


def test_two_sessions_chat_persisted_separately(world) -> None:  # type: ignore[no-untyped-def]
    client, db, session1, tokens1 = world

    def acct(uname: str, role: AccountType, disp: str) -> Account:
        a = Account(
            username=uname,
            password_hash=hash_password("pw"),
            roles=[role],
            display_name=disp,
        )
        db.accounts.insert(a)
        return a

    pa2 = acct("pb2x", AccountType.PLAYER, "选手A2")
    pb2 = acct("pb2y", AccountType.PLAYER, "选手B2")
    ref2 = acct("ref2x", AccountType.REFEREE, "裁判2")
    dri2 = acct("dri2x", AccountType.DIRECTOR, "导播2")
    session2 = Match(
        name="另一场B",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=session1.mappool,
        player_a_id=pa2.id,
        player_b_id=pb2.id,
        referee_id=ref2.id,
        director_id=dri2.id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(session2)

    with (
        client.websocket_connect(f"/ws/{tokens1['pa']}") as ws_a1,
        client.websocket_connect(f"/ws/{issue_token(pa2, settings)}") as ws_a2,
    ):
        _drain(ws_a1, 6)
        _drain(ws_a2, 6)
        ws_a1.send_json({"type": "chat", "text": "s1-msg"})
        ws_a1.receive_json()
        ws_a2.send_json({"type": "chat", "text": "s2-msg"})
        ws_a2.receive_json()

    s1_msgs = db.chat_messages.find_by_match(session1.id)
    s2_msgs = db.chat_messages.find_by_match(session2.id)
    # 座席连接提示以系统消息落库；聊天隔离断言只看用户消息。
    assert [m.text for m in s1_msgs if not m.is_system] == ["s1-msg"]
    assert [m.text for m in s2_msgs if not m.is_system] == ["s2-msg"]
