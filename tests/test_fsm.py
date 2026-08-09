"""M6 比赛状态机 + 开始倒计时测试。

COUNTDOWN 数值：fixture 的 start_countdown_delay=2。
"""

from __future__ import annotations


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 20):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def test_prep_and_select_pick(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        phase = _recv_until(ws_r, lambda m: m["type"] == "phase_change")
        assert phase["phase"] == 1  # PREP
        ws_r.send_json({"type": "referee_select_pick", "pick_code": "ML1"})
        pick_msg = _recv_until(
            ws_r, lambda m: m["type"] == "system" and "ML1" in m["text"]
        )
        assert "selected" in pick_msg["text"]


def test_select_pick_invalid(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "referee_select_pick", "pick_code": "NOPE"})
        err = _recv_until(ws_r, lambda m: m["type"] == "error")
        assert err["code"] == 400


def test_manual_start_reaches_round(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        _drain(ws_r, 5)
        _drain(ws_a, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 2)  # phase_change + system
        ws_r.send_json({"type": "referee_select_pick", "pick_code": "ML1"})
        _drain(ws_r, 1)  # system pick
        ws_r.send_json({"type": "referee_manual_start"})
        # 倒计时 2 秒后发令 → 选手收到 round_start
        rs = _recv_until(ws_a, lambda m: m["type"] == "round_start", max_msgs=20)
        assert rs["pick"]["code"] == "ML1"
        assert rs["round_id"]


def test_auto_countdown_aborted_by_unready(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 2)
        ws_r.send_json({"type": "referee_select_pick", "pick_code": "ML1"})
        _drain(ws_r, 1)
        ws_a.send_json({"type": "chat", "text": "!ready"})
        ws_b.send_json({"type": "chat", "text": "!ready"})
        # 双就绪 → 自动倒计时（phase COUNTDOWN）
        _recv_until(ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 2)
        # A 取消准备 → 倒计时中断
        ws_a.send_json({"type": "chat", "text": "!ready"})
        abort = _recv_until(ws_r, lambda m: m["type"] == "countdown_abort")
        assert abort["reason"] == "player_unready"
        # 随后回到 PREP
        phase = _recv_until(
            ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 1
        )
        assert phase["phase"] == 1
