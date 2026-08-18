"""exclusive 接管（last-wins takeover）验收测试。

- 同账号同场同座位 exclusive=1 新连接顶掉旧连接：先 displaced 再 close(4001)
- key 含 match：同账号裁判不同场多标签互不干扰（即便都带 exclusive）
- 导播不带 exclusive：OBS 多源并存不被踢；带 exclusive 则顶掉自己同场旧连接
- 被顶掉连接随后发来的在途裁判指令被忽略
- 不带 exclusive 的旧语义（同座位静默替换、关闭码 1000）不变
- 选手无缝接管：观察方不见 offline→online 抖动
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from twilightcupbackend.datatypes import (
    Mappool,
    Match,
    MatchPhase,
    MatchStatus,
    ScoringMethod,
    Seat,
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


def _another_match(session: Match) -> Match:  # type: ignore[no-untyped-def]
    """同裁判/导播/选手的另一场比赛（裁判多标签页选场用）。"""
    return Match(
        name="另一场",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=Mappool(),
        player_a_id=session.player_a_id,
        player_b_id=session.player_b_id,
        referee_id=session.referee_id,
        director_id=session.director_id,
        status=MatchStatus.RUNNING,
    )


def test_referee_same_match_exclusive_displaces_old(world) -> None:  # type: ignore[no-untyped-def]
    """验收 1：同账号 REFEREE 同场连两次（第二次 exclusive=1）——
    第一个收 displaced + close 4001；第二个 auth_ok 并照常收初始化快照。"""
    client, _, session, tokens = world
    url = f"/ws/{tokens['ref']}?match={session.id}"
    with (
        client.websocket_connect(url) as ws1,
        client.websocket_connect(url + "&exclusive=1") as ws2,
    ):
        assert ws1.receive_json()["type"] == "auth_ok"
        _drain(ws1, 4)  # ready_state / phase_change / seat_state ×2
        # 旧连接：先 displaced（非 auth_error，token 仍有效），后 close(4001)
        assert ws1.receive_json() == {
            "type": "displaced",
            "reason": "superseded_by_new_connection",
        }
        with pytest.raises(WebSocketDisconnect) as ei:
            ws1.receive_json()
        assert ei.value.code == 4001
        # 新连接：auth_ok + 全量快照照常
        assert ws2.receive_json()["type"] == "auth_ok"
        snapshot = [ws2.receive_json()["type"] for _ in range(4)]
        assert snapshot == ["ready_state", "phase_change", "seat_state", "seat_state"]


def test_referee_different_matches_coexist(world) -> None:  # type: ignore[no-untyped-def]
    """验收 2：同账号裁判不同场两个标签页（key 含 match）——共存互不影响。"""
    client, db, session, tokens = world
    other = _another_match(session)
    db.matches.insert(other)
    with (
        client.websocket_connect(
            f"/ws/{tokens['ref']}?match={session.id}&exclusive=1"
        ) as ws1,
        client.websocket_connect(
            f"/ws/{tokens['ref']}?match={other.id}&exclusive=1"
        ) as ws2,
    ):
        for ws in (ws1, ws2):
            _drain(ws, 5)
        # 两场各自收发正常；若被互踢，receive 会先抛 WebSocketDisconnect(4001)
        ws1.send_json({"type": "chat", "text": "field-1"})
        m = _recv_until(ws1, lambda m: m.get("type") == "chat")
        assert m["text"] == "field-1"
        ws2.send_json({"type": "chat", "text": "field-2"})
        m = _recv_until(ws2, lambda m: m.get("type") == "chat")
        assert m["text"] == "field-2"


def test_director_same_match_multi_conn_coexist(world) -> None:  # type: ignore[no-untyped-def]
    """验收 3：同账号 DIRECTOR 同场多连接（不带 exclusive）——共存不被踢。"""
    client, _, session, tokens = world
    url = f"/ws/{tokens['dri']}?match={session.id}"
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}?match={session.id}") as ws_r,
        client.websocket_connect(url) as ws_d1,
        client.websocket_connect(url) as ws_d2,
    ):
        for ws in (ws_r, ws_d1, ws_d2):
            _drain(ws, 5)
        ws_r.send_json({"type": "chat", "text": "to-directors"})
        # 两条导播连接都还活着且收到广播
        for ws in (ws_d1, ws_d2):
            m = _recv_until(ws, lambda m: m.get("type") == "chat")
            assert m["text"] == "to-directors"


def test_director_exclusive_displaces_own_connections(world) -> None:  # type: ignore[no-untyped-def]
    """机制补充：导播带 exclusive=1 顶掉自己同场全部旧连接（多连接 displacement）。"""
    client, _, session, tokens = world
    url = f"/ws/{tokens['dri']}?match={session.id}"
    with (
        client.websocket_connect(url) as ws_d1,
        client.websocket_connect(url) as ws_d2,
        client.websocket_connect(url + "&exclusive=1") as ws_d3,
    ):
        for ws in (ws_d1, ws_d2, ws_d3):
            _drain(ws, 5)
        for ws in (ws_d1, ws_d2):
            assert ws.receive_json()["type"] == "displaced"
            with pytest.raises(WebSocketDisconnect) as ei:
                ws.receive_json()
            assert ei.value.code == 4001
        # 幸存的新连接可正常保活；只读约束不变（chat 被拒 403）
        ws_d3.send_json({"type": "heartbeat"})
        ws_d3.send_json({"type": "chat", "text": "x"})
        m = _recv_until(ws_d3, lambda m: m.get("type") == "error")
        assert m["code"] == 403


def test_displaced_inflight_referee_command_ignored(world) -> None:  # type: ignore[no-untyped-def]
    """验收 4：被顶掉的旧连接随后发的裁判指令被忽略（不再影响比赛状态）。"""
    client, _, session, tokens = world
    url = f"/ws/{tokens['ref']}?match={session.id}"
    with client.websocket_connect(url) as ws1:
        _drain(ws1, 5)
        store = client.app.state.registry.get(session.id)
        old = store.connections[Seat.REFEREE]
        with client.websocket_connect(url + "&exclusive=1") as ws2:
            assert ws1.receive_json()["type"] == "displaced"
            with pytest.raises(WebSocketDisconnect) as ei:
                ws1.receive_json()
            assert ei.value.code == 4001
            # 已注销：槽内是新连接，旧连接不在册
            assert store.connections[Seat.REFEREE] is not old
            assert not store.has_connection(old)
            # 旧连接在途的裁判指令 → 忽略（阶段不变）
            cm = client.app.state.connection_manager
            asyncio.run(cm.handle(old, '{"type": "referee_mark_prep"}'))
            assert store.phase is MatchPhase.IDLE
            # 新连接同指令正常生效（初始化快照里也有 phase_change=IDLE，按值过滤）
            ws2.send_json({"type": "referee_mark_prep"})
            m = _recv_until(
                ws2,
                lambda m: (
                    m.get("type") == "phase_change"
                    and m["phase"] == MatchPhase.PREP.value
                ),
            )
            assert m is not None


def test_no_exclusive_silent_replace_unchanged(world) -> None:  # type: ignore[no-untyped-def]
    """验收 5：不带 exclusive 的同座位重连走旧语义——静默替换（无 displaced、
    关闭码 1000），普通断线重连行为不变。"""
    client, _, session, tokens = world
    url = f"/ws/{tokens['ref']}?match={session.id}"
    with (
        client.websocket_connect(url) as ws1,
        client.websocket_connect(url) as ws2,
    ):
        for ws in (ws1, ws2):
            _drain(ws, 5)
        with pytest.raises(WebSocketDisconnect) as ei:
            for _ in range(10):
                ws1.receive_json()
        assert ei.value.code == 1000
        ws2.send_json({"type": "chat", "text": "still-here"})
        m = _recv_until(ws2, lambda m: m.get("type") == "chat")
        assert m["text"] == "still-here"


def test_player_exclusive_takeover_no_presence_flicker(world) -> None:  # type: ignore[no-untyped-def]
    """验收 6：选手 exclusive 无缝接管——观察方不见该 seat 的 offline 广播
    （净在线状态未变；旧端点迟到的 disconnect 因槽内已是新连接而不广播）。"""
    client, _, session, tokens = world
    url = f"/ws/{tokens['pa']}?match={session.id}&exclusive=1"
    seen: list[dict] = []

    def _track(m: dict) -> bool:
        seen.append(m)
        return m.get("type") == "seat_state" and m.get("seat") == "PLAYER_A"

    with (
        client.websocket_connect(f"/ws/{tokens['ref']}?match={session.id}") as ws_r,
        client.websocket_connect(url) as ws_a1,
        client.websocket_connect(url) as ws_a2,
    ):
        _drain(ws_r, 5)
        _drain(ws_a1, 5)
        _drain(ws_a2, 5)
        # a1 连入广播 online=true
        assert _recv_until(ws_r, _track)["online"] is True
        # 接管后的下一条 PLAYER_A seat_state 仍为 online=true（期间无 offline）
        assert _recv_until(ws_r, _track)["online"] is True
        assert not any(
            m.get("type") == "seat_state"
            and m.get("seat") == "PLAYER_A"
            and m.get("online") is False
            for m in seen
        )
        # 被顶掉的 a1：displaced + 4001；此后槽内是 a2（新连接）
        assert ws_a1.receive_json()["type"] == "displaced"
        with pytest.raises(WebSocketDisconnect) as ei:
            ws_a1.receive_json()
        assert ei.value.code == 4001
        store = client.app.state.registry.get(session.id)
        assert store.connections[Seat.PLAYER_A] is not None
        assert store.seats_connected() >= {Seat.PLAYER_A, Seat.REFEREE}
