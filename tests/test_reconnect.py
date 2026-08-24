"""M8 重连补传 + 异常测试：幂等上报、reconnect_resync 快照、terminate_round。"""

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


def _drive_to_round(ws_r, ws_a, pick: str = "ML1") -> str:  # type: ignore[no-untyped-def]
    ws_r.send_json({"type": "referee_mark_prep"})
    _drain(ws_r, 2)
    ws_r.send_json({"type": "referee_select_pick", "pick_code": pick})
    _drain(ws_r, 1)
    ws_r.send_json({"type": "referee_manual_start"})
    rs = _recv_until(ws_a, lambda m: m["type"] == "round_start")
    return rs["round_id"]


def test_idempotent_level_upload(world) -> None:  # type: ignore[no-untyped-def]
    client, db, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        # 同一关卡上报两次 → 幂等，仅一条
        for _ in range(2):
            ws_a.send_json(
                {
                    "type": "level_time_upload",
                    "round_id": rid,
                    "level_index": 0,
                    "this_level_ms": 1000,
                    "total_ms": 1000,
                }
            )
        _drain(ws_a, 1)
        record = db.rounds.get(rid)
        assert record is not None
        assert len(record.state_a.completed_levels) == 1
        assert record.state_a.completed_levels[0].level_index == 0


def test_reconnect_snapshot(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        ws_a.send_json(
            {
                "type": "level_time_upload",
                "round_id": rid,
                "level_index": 0,
                "this_level_ms": 1500,
                "total_ms": 1500,
            }
        )
        ws_b.send_json(
            {
                "type": "level_time_upload",
                "round_id": rid,
                "level_index": 0,
                "this_level_ms": 2000,
                "total_ms": 2000,
            }
        )
        # 选手 A 断线（关闭旧连接）
    # 重连：新连接同座位，请求权威快照
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r2,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a2,
    ):
        _drain(ws_r2, 5)
        _drain(ws_a2, 6)
        ws_a2.send_json({"type": "reconnect_resync", "round_id": rid})
        msgs = [ws_a2.receive_json() for _ in range(2)]
        assert all(m["type"] == "player_status" for m in msgs)
        assert {m["seat"] for m in msgs} == {"PLAYER_A", "PLAYER_B"}
        own_msg = next(m for m in msgs if m["seat"] == "PLAYER_A")
        assert own_msg["completed_levels"][0]["time_ms"] == 1500


def test_terminate_round(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        ws_r.send_json(
            {"type": "referee_terminate_round", "round_id": rid, "reason": "选手崩溃"}
        )
        judging = _recv_until(
            ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 4
        )
        assert judging["phase"] == 4  # ROUND_JUDGING
        # 裁判判 A 断连负（B 胜）
        ws_r.send_json(
            {
                "type": "referee_verdict",
                "round_id": rid,
                "verdict": 4,
            }  # A_DISCONNECT_LOSS
        )
        cum = _recv_until(ws_r, lambda m: m["type"] == "cumulative_score")
        assert cum["wins_b"] == 1
