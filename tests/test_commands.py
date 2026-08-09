"""M5 聊天命令测试：!ready / !roll / !timer / !timer reset / 未知命令。

注：``!`` 命令经 _on_chat 会先作为普通聊天消息回显（含发送方），再由命令路由器执行，
故每条命令后需先消费一条回声 chat，再读取 ready_state/system/error 等响应。
"""

from __future__ import annotations

from twilightcupbackend.timer_service import alert_seconds


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _skip_echo(ws) -> None:  # type: ignore[no-untyped-def]
    """跳过命令回显的那条普通 chat 消息。"""
    msg = ws.receive_json()
    assert msg["type"] == "chat"


def test_alert_seconds() -> None:
    assert alert_seconds(10) == {5, 4, 3, 2, 1}
    assert alert_seconds(65) == {60, 30, 20, 10, 5, 4, 3, 2, 1}
    assert alert_seconds(180) == {120, 60, 30, 20, 10, 5, 4, 3, 2, 1}


def test_ready_toggle(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
        _drain(ws_a, 5)
        ws_a.send_json({"type": "chat", "text": "!ready"})
        _skip_echo(ws_a)
        ready_msg = ws_a.receive_json()
        sys_msg = ws_a.receive_json()
        assert ready_msg == {
            "type": "ready_state",
            "a_ready": True,
            "b_ready": False,
        }
        assert sys_msg["type"] == "system"
        assert "Player A" in sys_msg["text"] and "is ready" in sys_msg["text"]

        # 再切换一次 → 取消
        ws_a.send_json({"type": "chat", "text": "!ready"})
        _skip_echo(ws_a)
        ready_msg2 = ws_a.receive_json()
        sys_msg2 = ws_a.receive_json()
        assert ready_msg2["a_ready"] is False
        assert "cancelled ready" in sys_msg2["text"]


def test_ready_only_players(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "chat", "text": "!ready"})
        _skip_echo(ws_r)
        err = ws_r.receive_json()
        assert err["type"] == "error" and err["code"] == 403


def test_roll(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
        _drain(ws_a, 5)
        ws_a.send_json({"type": "chat", "text": "!roll"})
        _skip_echo(ws_a)
        msg = ws_a.receive_json()
        assert msg["type"] == "system"
        assert "rolled" in msg["text"] and "(1-100)" in msg["text"]


def test_unknown_command(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
        _drain(ws_a, 5)
        ws_a.send_json({"type": "chat", "text": "!foobar"})
        _skip_echo(ws_a)
        msg = ws_a.receive_json()
        assert msg["type"] == "system"
        assert "Unknown command" in msg["text"]


def test_counter_via_ws(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "chat", "text": "!timer 2"})
        _skip_echo(ws_r)
        collected = []
        for _ in range(12):
            msg = ws_r.receive_json()
            collected.append(msg)
            if msg.get("type") == "system" and "Timer ended" in msg.get("text", ""):
                break
        alerts = [
            m["remaining_secs"] for m in collected if m["type"] == "counter_alert"
        ]
        assert 1 in alerts and 0 in alerts
        states = [
            m["remaining_secs"] for m in collected if m["type"] == "counter_state"
        ]
        assert 2 in states and None in states


def test_counter_reset(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "chat", "text": "!timer 30"})
        _skip_echo(ws_r)
        ws_r.receive_json()  # counter_state(30)
        ws_r.receive_json()  # system 启动
        ws_r.send_json({"type": "chat", "text": "!timer reset"})
        _skip_echo(ws_r)
        state_msg = ws_r.receive_json()
        sys_msg = ws_r.receive_json()
        assert (
            state_msg["type"] == "counter_state" and state_msg["remaining_secs"] is None
        )
        assert "reset" in sys_msg["text"]


def test_counter_overwrites_active(world) -> None:  # type: ignore[no-untyped-def]
    """已有计时器在跑时再次 !timer 直接覆盖，旧计时器不产生额外输出。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        # 启动第一个 30s 计时器：counter_state(30) + system
        ws_r.send_json({"type": "chat", "text": "!timer 30"})
        _skip_echo(ws_r)
        first_state = ws_r.receive_json()
        assert (
            first_state["type"] == "counter_state"
            and first_state["remaining_secs"] == 30
        )
        ws_r.receive_json()  # system 启动

        # 启动第二个 10s 计时器：仅 counter_state(10) + system，旧计时器无任何输出
        ws_r.send_json({"type": "chat", "text": "!timer 10"})
        _skip_echo(ws_r)
        new_state = ws_r.receive_json()
        assert (
            new_state["type"] == "counter_state" and new_state["remaining_secs"] == 10
        )
        sys_msg = ws_r.receive_json()
        assert sys_msg["type"] == "system"

        # 收完第二个计时器自然结束的全程（10s 内无旧计时器残留告警/结束消息）
        collected = []
        for _ in range(15):
            msg = ws_r.receive_json()
            collected.append(msg)
            if msg.get("type") == "system" and "Timer ended" in msg.get("text", ""):
                break
        # 新计时器仅应出现一次结束；旧 30s 计时器被静默取消，不应有第二份结束
        ended = [
            m
            for m in collected
            if m.get("type") == "system" and "Timer ended" in m.get("text", "")
        ]
        assert len(ended) == 1
        alerts = [
            m["remaining_secs"] for m in collected if m["type"] == "counter_alert"
        ]
        assert 1 in alerts and 0 in alerts
