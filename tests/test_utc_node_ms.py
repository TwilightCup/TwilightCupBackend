"""节点事件携带 utc_ms 验收测试（backend-utc_node_utcms）。

插件停发周期 utc_timestamp 心跳后，判定/导播时钟改由四个节点事件
（level_time_upload / attempt_skip / project_complete / forfeit_signal）
自带的 utc_ms 承载：带 utc_ms 的报文不再被 extra="forbid" 拒收，且生效时
写入 store.utc_timestamps[seat]，晚连的裁判/导播仍能补发到该选手最近的
时钟来源。
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from twilightcupbackend.protocol import (
    ClientAttemptSkip,
    ClientForfeitSignal,
    ClientLevelTimeUpload,
    ClientProjectComplete,
    parse_client_message,
)


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 60):  # type: ignore[no-untyped-def]
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


def test_node_events_require_utc_ms_and_accept_multiple() -> None:
    """四个节点事件带额外 utc_ms 字段被接受；缺 utc_ms 反被拒绝。"""
    # 插件新报文中多出的 utc_ms 不再触发 extra="forbid"
    for good in (
        {
            "type": "level_time_upload",
            "round_id": "r",
            "level_index": 0,
            "this_level_ms": 1000,
            "utc_ms": 1700000000000,
        },
        {
            "type": "attempt_skip",
            "round_id": "r",
            "attempt_index": 1,
            "utc_ms": 1700000000000,
        },
        {
            "type": "project_complete",
            "round_id": "r",
            "final_total_ms": 1000,
            "utc_ms": 1700000000000,
        },
        {
            "type": "forfeit_signal",
            "round_id": "r",
            "reason": "multi_exit",
            "utc_ms": 1700000000000,
        },
    ):
        parse_client_message(json.dumps(good))
    # utc_ms 为必填：缺省应被拒绝
    for bad in (
        {"type": "level_time_upload", "round_id": "r", "level_index": 0},
        {"type": "attempt_skip", "round_id": "r", "attempt_index": 1},
        {"type": "project_complete", "round_id": "r"},
        {"type": "forfeit_signal", "round_id": "r", "reason": "multi_exit"},
    ):
        try:
            parse_client_message(json.dumps(bad))
        except ValidationError:
            continue
        raise AssertionError(f"应拒绝缺 utc_ms 的报文：{bad}")


def test_node_event_utc_ms_fed_to_referee_clock_source(world) -> None:  # type: ignore[no-untyped-def]
    """节点事件生效时把 utc_ms 写进 store.utc_timestamps[seat]；晚连裁判/导播
    据此补发，即使插件已停发周期 utc_timestamp。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        with client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a:
            _drain(ws_a, 6)
            with client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b:
                _drain(ws_b, 6)
                rid = _drive_to_round(ws_r, ws_a)
                # 仅通过节点事件携带 utc_ms（不回发周期 utc_timestamp）
                ws_a.send_json(
                    {
                        "type": "level_time_upload",
                        "round_id": rid,
                        "level_index": 0,
                        "this_level_ms": 1000,
                        "utc_ms": 1700000001000,
                    }
                )
                ws_b.send_json(
                    {
                        "type": "project_complete",
                        "round_id": rid,
                        "final_total_ms": 2000,
                        "utc_ms": 1700000002000,
                    }
                )
                # 晚连的导播：握手补发两条，各自取节点事件注入的最新 utc_ms
                with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d:
                    _drain(ws_d, 5)
                    seen: set[str] = set()

                    def _until_both_seats(m: dict) -> bool:
                        if m["type"] == "utc_timestamp":
                            seen.add(m["seat"])
                        return len(seen) == 2

                    got = []
                    for _ in range(60):
                        m = ws_d.receive_json()
                        got.append(m)
                        if _until_both_seats(m):
                            break
                    utcs = {m["seat"]: m for m in got if m["type"] == "utc_timestamp"}
                    assert utcs["PLAYER_A"]["utc_ms"] == 1700000001000
                    assert utcs["PLAYER_B"]["utc_ms"] == 1700000002000


def test_node_event_examples_typed():  # type: ignore[no-untyped-def]
    # 模型直接构造示例（确保字段名/类型与协议一致）
    assert ClientLevelTimeUpload(
        round_id="r", level_index=0, this_level_ms=1, utc_ms=2
    ).utc_ms == 2
    assert ClientAttemptSkip(
        round_id="r", attempt_index=0, utc_ms=2
    ).utc_ms == 2
    assert ClientProjectComplete(round_id="r", utc_ms=2).utc_ms == 2
    assert ClientForfeitSignal(
        round_id="r", reason="multi_exit", utc_ms=2
    ).utc_ms == 2
