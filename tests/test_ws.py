"""M4 连接管理 + 协议测试：聊天中转、导播只读、鉴权。"""

from __future__ import annotations


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 30):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def test_connect_auth_ok(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "auth_ok"
        assert msg["seat"] == "PLAYER_A"


def test_chat_broadcast_to_all(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
        _drain(ws_a, 5)  # auth_ok / ready_state / phase_change
        with client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b:
            _drain(ws_b, 5)
            # B 连入对 A 产生一条 seat_state(PLAYER_B, online) 广播
            _recv_until(
                ws_a,
                lambda m: m["type"] == "seat_state" and m["seat"] == "PLAYER_B",
            )
            ws_a.send_json({"type": "chat", "text": "你好"})
            echo_a = ws_a.receive_json()
            echo_b = ws_b.receive_json()
            for msg in (echo_a, echo_b):
                assert msg["type"] == "chat"
                assert msg["text"] == "你好"
                assert msg["seat"] == "PLAYER_A"


def test_director_read_only(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d:
        _drain(ws_d, 5)
        ws_d.send_json({"type": "chat", "text": "导播发言"})
        err = ws_d.receive_json()
        assert err["type"] == "error"
        assert err["code"] == 403


def test_director_receives_chat(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d:
        _drain(ws_d, 5)
        with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
            _drain(ws_a, 5)
            ws_a.send_json({"type": "chat", "text": "hi"})
            # 跳过 A 连入产生的 seat_state 广播，取真正的 chat
            msg = _recv_until(ws_d, lambda m: m["type"] == "chat")
            assert msg["text"] == "hi"


def test_invalid_token_auth_error(world) -> None:  # type: ignore[no-untyped-def]
    client, *_ = world
    with client.websocket_connect("/ws/not.a.valid.token") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "auth_error"


def test_chat_persisted(world) -> None:  # type: ignore[no-untyped-def]
    client, db, session, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
        _drain(ws_a, 5)
        ws_a.send_json({"type": "chat", "text": "留存"})
        ws_a.receive_json()  # 收到自己的回声
    msgs = db.chat_messages.find_by_match(session.id)
    assert len(msgs) == 1
    assert msgs[0].text == "留存"
    assert msgs[0].is_system is False


def test_draft_sync_broadcast_to_director(world) -> None:  # type: ignore[no-untyped-def]
    """裁判上报 ban/pick 草稿状态 → 存储 + 广播给导播。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d:
        _drain(ws_d, 5)  # auth_ok / ready_state / phase_change
        with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
            _drain(ws_r, 5)
            state = {
                "stage": "PICK",
                "actions": [{"by": "A", "code": "ML1", "kind": "ban"}],
                "picks": [],
            }
            ws_r.send_json({"type": "draft_sync", "state": state})
            msg = ws_d.receive_json()
            assert msg["type"] == "draft_state"
            assert msg["state"] == state


def test_draft_sync_only_referee(world) -> None:  # type: ignore[no-untyped-def]
    """非裁判发 draft_sync → 403。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d:
        _drain(ws_d, 5)
        ws_d.send_json({"type": "draft_sync", "state": {"stage": "PICK"}})
        err = ws_d.receive_json()
        assert err["type"] == "error"
        assert err["code"] == 403
