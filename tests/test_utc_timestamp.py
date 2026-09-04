"""选手 UTC 时间戳（utc_timestamp）中转验收测试。

选手连接后按固定间隔上报 Unix UTC 毫秒时间戳；服务端按席暂存最近一条，
仅中转裁判/导播（选手双方互不转发），裁判/导播晚连时握手补发最近值。
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
    got: list[dict] = []
    for _ in range(max_msgs):
        m = ws.receive_json()
        got.append(m)
        if predicate(m):
            return got
    raise AssertionError("未在限定消息内匹配到目标")


def _ping(ws) -> None:  # type: ignore[no-untyped-def]
    ws.send_json({"type": "chat", "text": "ping"})


def _assert_absent_since_ping(ws_target, ws_sender, msg_type: str) -> None:  # type: ignore[no-untyped-def]
    _ping(ws_sender)
    got = _collect_until(
        ws_target, lambda m: m["type"] == "chat" and m["text"] == "ping"
    )
    assert not any(m["type"] == msg_type for m in got), (
        f"不应出现 {msg_type}：{[m['type'] for m in got]}"
    )


def test_utc_timestamp_relayed_to_referee_and_director_only(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d,
    ):
        for ws in (ws_r, ws_a, ws_b, ws_d):
            _drain(ws, 5)
        ws_a.send_json({"type": "utc_timestamp", "utc_ms": 1700000000000})
        for ws, tag in ((ws_r, "ref"), (ws_d, "dri")):
            m = _recv_until(ws, lambda m: m["type"] == "utc_timestamp")
            assert m["seat"] == "PLAYER_A", tag
            assert m["utc_ms"] == 1700000000000, tag
        # 发送方无回声，对方选手也不收
        _assert_absent_since_ping(ws_a, ws_a, "utc_timestamp")
        _assert_absent_since_ping(ws_b, ws_b, "utc_timestamp")


def test_latest_kept_and_replayed_to_late_director(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_a.send_json({"type": "utc_timestamp", "utc_ms": 1700000000000})
        _recv_until(ws_r, lambda m: m["type"] == "utc_timestamp")
        ws_a.send_json({"type": "utc_timestamp", "utc_ms": 1700000001000})
        _recv_until(ws_r, lambda m: m["type"] == "utc_timestamp")
        ws_b.send_json({"type": "utc_timestamp", "utc_ms": 1700000002000})
        _recv_until(ws_r, lambda m: m["type"] == "utc_timestamp")
        with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d:
            # 握手按插入序补发双方最近一条；收齐两席再断言
            seen: set[str] = set()

            def _until_both_seats(m: dict) -> bool:
                if m["type"] == "utc_timestamp":
                    seen.add(m["seat"])
                return len(seen) == 2

            got = _collect_until(ws_d, _until_both_seats)
            utcs = {m["seat"]: m for m in got if m["type"] == "utc_timestamp"}
            assert utcs["PLAYER_A"]["utc_ms"] == 1700000001000  # 最新一条
            assert utcs["PLAYER_B"]["utc_ms"] == 1700000002000


def test_referee_rejected(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "utc_timestamp", "utc_ms": 1700000000000})
        m = _recv_until(ws_r, lambda m: m["type"] == "error")
        assert m["code"] == 403


def test_protocol_strict() -> None:
    good = {"type": "utc_timestamp", "utc_ms": 1700000000000}
    parse_client_message(json.dumps(good))
    with pytest.raises(ValidationError):
        parse_client_message(json.dumps({**good, "extra": 1}))
    with pytest.raises(ValidationError):
        parse_client_message(json.dumps({"type": "utc_timestamp"}))
