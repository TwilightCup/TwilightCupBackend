"""选手实时计时（live_time）中转验收测试。

fixture 的 start_countdown_delay=2；图池 ML1（多关）。选手每秒上报
RoundTotalMs/CurrentSegmentMs（以及可选 real_time_ms 现实/墙钟时间）；
仅中转裁判/导播（选手间互不转发），按席暂存最近一条，裁判/导播回合中
晚连时握手补发。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from twilightcupbackend.protocol import parse_client_message


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 60):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def _collect_until(ws, predicate, max_msgs: int = 60):  # type: ignore[no-untyped-def]
    """收集消息直到命中 predicate，返回全部已读列表（用于“未发生”断言）。"""
    got: list[dict] = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        got.append(m)
        if predicate(m):
            return got
    raise AssertionError("未在限定消息内匹配到目标")


def _drive_to_round(ws_r, ws_a, pick: str = "ML1") -> str:  # type: ignore[no-untyped-def]
    ws_r.send_json({"type": "referee_mark_prep"})
    _drain(ws_r, 2)
    ws_r.send_json({"type": "referee_select_pick", "pick_code": pick})
    _drain(ws_r, 1)
    ws_r.send_json({"type": "referee_manual_start"})
    rs = _recv_until(ws_a, lambda m: m["type"] == "round_start")
    return rs["round_id"]


def _live(  # type: ignore[no-untyped-def]
    ws,
    rid: str,
    level: int,
    total_ms: int,
    segment_ms: int,
    real_time_ms: int | None = None,
) -> None:
    msg = {
        "type": "live_time",
        "round_id": rid,
        "level_index": level,
        "total_ms": total_ms,
        "segment_ms": segment_ms,
    }
    if real_time_ms is not None:
        msg["real_time_ms"] = real_time_ms
    ws.send_json(msg)


def _ping(ws) -> None:  # type: ignore[no-untyped-def]
    ws.send_json({"type": "chat", "text": "ping"})


def _assert_absent_since_ping(ws_target, ws_sender, msg_type: str) -> None:  # type: ignore[no-untyped-def]
    """发送方发聊天哨兵，到哨兵回声为止目标不应出现指定类型消息。"""
    _ping(ws_sender)
    got = _collect_until(
        ws_target, lambda m: m["type"] == "chat" and m["text"] == "ping"
    )
    assert not any(m["type"] == msg_type for m in got), (
        f"不应出现 {msg_type}：{[m['type'] for m in got]}"
    )


def test_live_time_relayed_to_observers_only(world) -> None:  # type: ignore[no-untyped-def]
    """实时计时只中转裁判/导播（选手双方均不收），字段完整。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d,
    ):
        for ws in (ws_r, ws_a, ws_b, ws_d):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        _live(
            ws_a, rid, level=1, total_ms=63210, segment_ms=18430, real_time_ms=63720
        )
        for ws, tag in ((ws_r, "ref"), (ws_d, "dri")):
            m = _recv_until(ws, lambda m: m["type"] == "live_time")
            assert m["seat"] == "PLAYER_A"
            assert m["round_id"] == rid
            assert m["level_index"] == 1
            assert m["total_ms"] == 63210 and m["segment_ms"] == 18430, tag
            assert m["real_time_ms"] == 63720, tag
        # 发送方无回声，对方选手也不收（对手实时进度不下发选手端）
        _assert_absent_since_ping(ws_a, ws_a, "live_time")
        _assert_absent_since_ping(ws_b, ws_b, "live_time")


def test_latest_kept_and_replayed_to_late_director(world) -> None:  # type: ignore[no-untyped-def]
    """按席只暂存最近一条；回合中晚连的导播在握手里拿到双方最新值。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        _live(
            ws_a, rid, level=0, total_ms=1000, segment_ms=1000, real_time_ms=1100
        )
        # 覆盖
        _live(
            ws_a, rid, level=0, total_ms=2000, segment_ms=2000, real_time_ms=2200
        )
        _live(
            ws_b, rid, level=0, total_ms=1500, segment_ms=1500, real_time_ms=1600
        )
        _recv_until(
            ws_r, lambda m: m["type"] == "live_time" and m["seat"] == "PLAYER_B"
        )
        with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d:
            # 握手按插入序补发双方最近一条，收齐两席再断言
            seen: set[str] = set()

            def _until_both_seats(m: dict) -> bool:
                if m["type"] == "live_time":
                    seen.add(m["seat"])
                return len(seen) == 2

            got = _collect_until(ws_d, _until_both_seats)
            live = {m["seat"]: m for m in got if m["type"] == "live_time"}
            assert live["PLAYER_A"]["total_ms"] == 2000  # 最新一条，非首条
            assert live["PLAYER_A"]["real_time_ms"] == 2200
            assert live["PLAYER_B"]["total_ms"] == 1500
            assert live["PLAYER_B"]["real_time_ms"] == 1600


def test_stale_round_ignored(world) -> None:  # type: ignore[no-untyped-def]
    """过期/伪造 round_id 的实时计时不中转。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        assert rid
        _live(ws_a, "no-such-round", level=0, total_ms=1, segment_ms=1)
        _assert_absent_since_ping(ws_r, ws_r, "live_time")


def test_protocol_strict() -> None:
    """extra/missing 字段均 ValidationError（pydantic extra=forbid 契约）。"""

    def parse(obj: dict) -> None:  # type: ignore[no-untyped-def]
        parse_client_message(json.dumps(obj))

    good = {
        "type": "live_time",
        "round_id": "r1",
        "level_index": 0,
        "total_ms": 100,
        "segment_ms": 50,
    }
    parse(good)  # real_time_ms 可选：旧版计时器不带也合法
    parse({**good, "real_time_ms": 120})  # 新版计时器附带现实/墙钟时间
    with pytest.raises(ValidationError):
        parse({**good, "extra": 1})
    with pytest.raises(ValidationError):
        parse({k: v for k, v in good.items() if k != "segment_ms"})
