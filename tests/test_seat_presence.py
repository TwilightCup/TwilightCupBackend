"""座席连接状态广播 seat_state（backend-seat-presence）验收测试。

- 选手连入 → 全员收 online=true；断开 → online=false（验收 1/3）
- 顶号重连替换旧连接 → 广播最终 online 状态，不抖动
- kick_players（比赛结束）→ 双方 offline
- 新连接初始化序列补发全量座席在线状态（验收 4）
"""

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


def test_player_connect_broadcasts_online(world) -> None:  # type: ignore[no-untyped-def]
    """验收 1：选手 A 连入 → 裁判端收到 seat_state(PLAYER_A, online=true)。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)  # auth_ok / ready_state / phase_change / seat_state ×2
        with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
            _drain(ws_a, 5)
            m = _recv_until(
                ws_r,
                lambda m: m["type"] == "seat_state" and m["seat"] == "PLAYER_A",
            )
            assert m["online"] is True
            notice = _recv_until(
                ws_r,
                lambda m: m["type"] == "system" and m["kind"] == "seat",
            )
            assert "connected" in notice["text"]


def test_player_disconnect_broadcasts_offline(world) -> None:  # type: ignore[no-untyped-def]
    """验收 1/3：选手断开 → 裁判端收到 online=false + 显式 system 提示。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
            _drain(ws_a, 5)
            _recv_until(
                ws_r,
                lambda m: m["type"] == "seat_state" and m["seat"] == "PLAYER_A",
            )
        # ws_a 关闭 → disconnect → 广播 offline
        m = _recv_until(
            ws_r,
            lambda m: m["type"] == "seat_state" and m["seat"] == "PLAYER_A",
        )
        assert m["online"] is False
        notice = _recv_until(
            ws_r,
            lambda m: m["type"] == "system" and m["kind"] == "seat",
        )
        assert "disconnected" in notice["text"]


def test_init_sequence_resends_full_presence(world) -> None:  # type: ignore[no-untyped-def]
    """验收 4：新连接初始化序列补发双方在线全量状态。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
        _drain(ws_a, 5)
        # 裁判此时连入：A 在线 / B 离线，补发给裁判
        with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
            states = {
                m["seat"]: m["online"]
                for m in (
                    _recv_until(ws_r, lambda m: m["type"] == "seat_state", max_msgs=5),
                    _recv_until(ws_r, lambda m: m["type"] == "seat_state", max_msgs=5),
                )
            }
            assert states == {"PLAYER_A": True, "PLAYER_B": False}


def test_referee_reconnect_gets_current_presence(world) -> None:  # type: ignore[no-untyped-def]
    """验收 4（裁判端刷新重连）：A 先连后断，裁判再连 → 补发 A=offline。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
        _drain(ws_a, 5)
    # A 已断开。裁判此刻连入，补发的全量状态应反映 A 离线
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        m = _recv_until(
            ws_r,
            lambda m: m["type"] == "seat_state" and m["seat"] == "PLAYER_A",
            max_msgs=5,
        )
        assert m["online"] is False


def test_reconnect_no_flicker(world) -> None:  # type: ignore[no-untyped-def]
    """§3：同座位顶号重连 → 仅广播最终 online=true，无 offline 抖动。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a1:
            _drain(ws_a1, 5)
            _recv_until(
                ws_r,
                lambda m: m["type"] == "seat_state" and m["seat"] == "PLAYER_A",
            )
            # 顶号重连：旧连接被替换，新连接落定后才广播（net online）
            with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a2:
                _drain(ws_a2, 5)
                m = _recv_until(
                    ws_r,
                    lambda m: m["type"] == "seat_state" and m["seat"] == "PLAYER_A",
                )
                assert m["online"] is True


def test_match_end_kick_broadcasts_offline(world) -> None:  # type: ignore[no-untyped-def]
    """§2 第 3 点：kick_players（达阈值自动结束后踢选手）同样广播 offline。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        # 走完整回合到比赛结束（win_threshold=2，需两回合）：
        # 第二回合判完即达阈值 → 自动结束（match_end + 踢选手）。
        for round_no in range(2):
            ws_r.send_json({"type": "referee_mark_prep"})
            _drain(ws_r, 2)
            ws_r.send_json({"type": "referee_select_pick", "pick_code": "ML1"})
            _drain(ws_r, 1)
            ws_r.send_json({"type": "referee_manual_start"})
            rid = _recv_until(ws_a, lambda m: m["type"] == "round_start")["round_id"]
            for ws in (ws_a, ws_b):
                ws.send_json(
                    {
                        "type": "project_complete",
                        "round_id": rid,
                        "final_total_ms": 1000 + round_no,
                    }
                )
            _recv_until(ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 4)
            ws_r.send_json(
                {"type": "referee_verdict", "round_id": rid, "verdict": 1}  # A_WIN
            )
            if round_no == 0:
                _recv_until(
                    ws_r,
                    lambda m: m["type"] == "phase_change" and m["phase"] == 5,
                )
            else:
                # 第二回合判完即达阈值 → 自动 match_end
                _recv_until(ws_r, lambda m: m["type"] == "match_end")
        # kick_players → 双方 offline
        for seat in ("PLAYER_A", "PLAYER_B"):
            m = _recv_until(
                ws_r,
                lambda m, s=seat: m["type"] == "seat_state" and m["seat"] == s,
            )
            assert m["online"] is False
