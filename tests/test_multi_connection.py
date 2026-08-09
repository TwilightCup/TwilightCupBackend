"""多连接 / 多身份测试：

- 导播多开（网页 + OBS）不互挤，都能收到广播；
- 一个多角色账号可同时以 PLAYER_A / REFEREE / DIRECTOR 身份各开一条连接。
"""

from __future__ import annotations

from twilightcupbackend.auth import hash_password, issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.datatypes import (
    Account,
    AccountType,
    Mappool,
    Match,
    MatchStatus,
    ScoringMethod,
)


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 30):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def test_director_multi_connection_not_evicted(world) -> None:  # type: ignore[no-untyped-def]
    """同一导播账号开两条连接，互不挤占，都能收到广播。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d1,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d2,
    ):
        _drain(ws_pa, 5)
        _drain(ws_d1, 5)
        _drain(ws_d2, 5)
        ws_pa.send_json({"type": "chat", "text": "broadcast-hi"})
        # 两条导播都收到；若互挤，先连的已被关闭，receive 会抛异常
        for ws in (ws_d1, ws_d2):
            msg = _recv_until(
                ws,
                lambda m: m.get("type") == "chat" and m.get("text") == "broadcast-hi",
            )
            assert msg["seat"] == "PLAYER_A"


def test_one_account_multi_seat(world) -> None:  # type: ignore[no-untyped-def]
    """一个多角色账号同时以选手A/裁判/导播身份各开一条 ?seat= 连接。"""
    client, db, _, _ = world
    x = Account(
        username="multirole",
        password_hash=hash_password("pw"),
        roles=[AccountType.PLAYER, AccountType.REFEREE, AccountType.DIRECTOR],
        display_name="多角色",
    )
    db.accounts.insert(x)
    pb = db.accounts.get_by_username("pb")
    assert pb is not None
    # 同一账号 X 同时被指派为选手A / 裁判 / 导播（player_b 用另一账号）
    sess = Match(
        name="一人多职",
        bo_format=1,
        win_threshold=1,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=Mappool(),
        player_a_id=x.id,
        player_b_id=pb.id,
        referee_id=x.id,
        director_id=x.id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(sess)
    tok = issue_token(x, settings)

    seats: list[str] = []
    with (
        client.websocket_connect(f"/ws/{tok}?seat=PLAYER_A") as ws_a,
        client.websocket_connect(f"/ws/{tok}?seat=REFEREE") as ws_r,
        client.websocket_connect(f"/ws/{tok}?seat=DIRECTOR") as ws_d,
    ):
        for ws in (ws_a, ws_r, ws_d):
            msg = ws.receive_json()
            assert msg["type"] == "auth_ok"
            seats.append(msg["seat"])
    assert seats == ["PLAYER_A", "REFEREE", "DIRECTOR"]
