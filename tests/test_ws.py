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
        _drain(ws_a, 6)  # auth_ok / ready_state / phase_change
        with client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b:
            _drain(ws_b, 6)
            # B 连入对 A 产生 seat_state(PLAYER_B, online) + system(seat) 广播
            _recv_until(
                ws_a,
                lambda m: m["type"] == "seat_state" and m["seat"] == "PLAYER_B",
            )
            _recv_until(
                ws_a,
                lambda m: m["type"] == "system" and m["kind"] == "seat",
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
            _drain(ws_a, 6)
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
        _drain(ws_a, 6)
        ws_a.send_json({"type": "chat", "text": "留存"})
        ws_a.receive_json()  # 收到自己的回声
    msgs = db.chat_messages.find_by_match(session.id)
    user_msgs = [m for m in msgs if not m.is_system]
    assert len(user_msgs) == 1
    assert user_msgs[0].text == "留存"
    assert user_msgs[0].is_system is False


def test_system_sender_prefix(world) -> None:  # type: ignore[no-untyped-def]
    """全场广播的 system 消息带 sender=Twilight（落库 sender_name 同步）；
    仅触发方可见的错误回执（error）不带 sender，客户端沿用 System 前缀。"""
    client, db, session, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
        _drain(ws_a, 6)
        # !roll → 全场广播的系统回执
        ws_a.send_json({"type": "chat", "text": "!roll"})
        _recv_until(ws_a, lambda m: m["type"] == "chat")  # 自己的命令回声
        sys_msg = _recv_until(
            ws_a, lambda m: m["type"] == "system" and m["kind"] == "roll"
        )
        assert sys_msg["sender"] == "Twilight"
        # !timer 仅裁判 → 定向错误回执（无 sender 字段）
        ws_a.send_json({"type": "chat", "text": "!timer 30"})
        _recv_until(ws_a, lambda m: m["type"] == "chat")
        err = _recv_until(ws_a, lambda m: m["type"] == "error")
        assert err["code"] == 403
        assert "sender" not in err
    msgs = db.chat_messages.find_by_match(session.id)
    sys_rows = [m for m in msgs if m.is_system]
    assert sys_rows  # 上线提示 / roll 回执等均已落库
    assert all(m.sender_name == "Twilight" for m in sys_rows)


def test_prep_reconnect_hint_targeted(world) -> None:  # type: ignore[no-untyped-def]
    """PREP 期间连入的未就绪选手席收仅其可见的 System 前缀 prep 提示（裁判
    不收、不落库）；已就绪席重连不重复提示（再 !ready 会取消就绪）。"""
    client, db, session, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 4)  # phase_change + system(prep) + ready_state + preload_state
        ws_r.send_json({"type": "referee_select_pick", "pick_code": "ML1"})
        _drain(ws_r, 3)  # system(pick) + pick_announced + preload_state 重置

        def _handshake(ws):  # type: ignore[no-untyped-def]
            seen: list[dict] = []

            def _track(m: dict) -> bool:
                seen.append(m)
                return m["type"] == "system" and m["kind"] == "seat"

            _recv_until(ws, _track)  # 本人上线提示是握手最后一条
            return seen

        with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
            seen = _handshake(ws_a)
            hints = [m for m in seen if m["type"] == "system" and m["kind"] == "prep"]
            assert len(hints) == 1
            assert hints[0]["sender"] == "System"
            assert "!ready" in hints[0]["text"]
            pick_hints = [
                m for m in seen if m["type"] == "system" and m["kind"] == "pick"
            ]
            assert len(pick_hints) == 1
            assert pick_hints[0]["sender"] == "System"
            assert "ML1" in pick_hints[0]["text"]
            # 裁判侧仅有上线广播（seat_state + system seat），无 prep/pick 提示
            for _ in range(2):
                m = ws_r.receive_json()
                assert not (
                    m["type"] == "system" and m.get("kind") in ("prep", "pick")
                )
            ws_a.send_json({"type": "chat", "text": "!ready"})
            _recv_until(
                ws_a, lambda m: m["type"] == "system" and m["kind"] == "ready"
            )
        _recv_until(ws_r, lambda m: m["type"] == "system" and m["kind"] == "seat")
        with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a2:
            seen2 = _handshake(ws_a2)
            # 已就绪重连：无 prep 提示，但当前选图提示仍在（与就绪态无关）
            assert not any(
                m["type"] == "system" and m["kind"] == "prep" for m in seen2
            )
            assert any(
                m["type"] == "system"
                and m["kind"] == "pick"
                and "ML1" in m["text"]
                for m in seen2
            )
    # 定向提示不落库：系统行只有 prep.started / ready / 上下线（均 Twilight）
    sys_rows = [m for m in db.chat_messages.find_by_match(session.id) if m.is_system]
    assert all(m.sender_name == "Twilight" for m in sys_rows)


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
