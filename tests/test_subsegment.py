"""MULTI 回合分段采样（subsegment）实时差距跟踪验收测试。

fixture 的 start_countdown_delay=2；图池 ML1（多关，L1+L2）、CT01（单关，需
retry）。采样中转只达对方 seat，命中向全场广播时间差；纯内存回合级数据，
重连经 reconnect_resync 按序补放。
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from twilightcupbackend.protocol import parse_client_message

PHASE_PREP = 1
PHASE_IN_ROUND = 3


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


def _drive_to_round(ws_r, ws_a, pick: str = "ML1", retry: int | None = None) -> str:  # type: ignore[no-untyped-def]
    ws_r.send_json({"type": "referee_mark_prep"})
    _drain(ws_r, 2)
    msg: dict = {"type": "referee_select_pick", "pick_code": pick}
    if retry is not None:
        msg["retry_count"] = retry
    ws_r.send_json(msg)
    _drain(ws_r, 1)
    ws_r.send_json({"type": "referee_manual_start"})
    rs = _recv_until(ws_a, lambda m: m["type"] == "round_start")
    return rs["round_id"]


def _sample(ws, rid: str, level: int, seq: int, t_ms: int) -> None:  # type: ignore[no-untyped-def]
    ws.send_json(
        {
            "type": "subsegment_sample",
            "round_id": rid,
            "level_index": level,
            "seq": seq,
            "t_ms": t_ms,
            "px": 1.5,
            "py": 2.5,
            "pz": 3.5,
            "dx": 0.5,
            "dy": 0.0,
            "dz": 0.0,
        }
    )


def _hit(ws, rid: str, level: int, seq: int, t_ms: int) -> None:  # type: ignore[no-untyped-def]
    ws.send_json(
        {
            "type": "subsegment_hit",
            "round_id": rid,
            "level_index": level,
            "seq": seq,
            "t_ms": t_ms,
        }
    )


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


# ---------------------------------------------------------------------------
# 采样中转
# ---------------------------------------------------------------------------


def test_sample_relayed_to_opponent_only(world) -> None:  # type: ignore[no-untyped-def]
    """采样只中转给对方 seat（本人无回声、裁判/导播不收），字段完整。"""
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
        _sample(ws_a, rid, level=0, seq=0, t_ms=1000)
        m = _recv_until(ws_b, lambda m: m["type"] == "subsegment_sample")
        assert m["seat"] == "PLAYER_A"
        assert m["round_id"] == rid
        assert m["level_index"] == 0 and m["seq"] == 0 and m["t_ms"] == 1000
        assert m["px"] == 1.5 and m["py"] == 2.5 and m["pz"] == 3.5
        assert m["dx"] == 0.5 and m["dy"] == 0.0 and m["dz"] == 0.0
        # 本人无回声；裁判与导播不收
        _assert_absent_since_ping(ws_a, ws_a, "subsegment_sample")
        _assert_absent_since_ping(ws_r, ws_r, "subsegment_sample")
        _assert_absent_since_ping(ws_d, ws_d, "subsegment_sample")


def test_duplicate_sample_ignored(world) -> None:  # type: ignore[no-untyped-def]
    """同 (seat, level, seq) 重复上报 → 只中转一次。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        _sample(ws_a, rid, level=0, seq=0, t_ms=1000)
        _recv_until(ws_b, lambda m: m["type"] == "subsegment_sample")
        _sample(ws_a, rid, level=0, seq=0, t_ms=2000)  # 重复键
        _sample(ws_a, rid, level=0, seq=1, t_ms=3000)  # 新键照常中转
        _ping(ws_a)
        got = _collect_until(
            ws_b, lambda m: m["type"] == "subsegment_sample" and m["seq"] == 1
        )
        # 哨兵回声后仅有 seq=1 一条中转：重复键不产生第二条，也不覆盖已存样本
        relays = [m for m in got if m["type"] == "subsegment_sample"]
        assert len(relays) == 1 and relays[0]["t_ms"] == 3000, relays


def test_non_player_rejected(world) -> None:  # type: ignore[no-untyped-def]
    """裁判席位发采样 → 403，不中转。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        _sample(ws_r, rid, level=0, seq=0, t_ms=1000)
        err = _recv_until(ws_r, lambda m: m["type"] == "error" and m["code"] == 403)
        assert err is not None
        _assert_absent_since_ping(ws_a, ws_a, "subsegment_sample")


# ---------------------------------------------------------------------------
# 命中与时间差广播
# ---------------------------------------------------------------------------


def test_hit_broadcasts_gap(world) -> None:  # type: ignore[no-untyped-def]
    """B 穿越 A 的采样平面 → 双方 + 裁判均收 subsegment_gap，数值正确。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        _sample(ws_a, rid, level=0, seq=2, t_ms=5200)
        _recv_until(ws_b, lambda m: m["type"] == "subsegment_sample")
        _hit(ws_b, rid, level=0, seq=2, t_ms=6100)
        for ws, tag in ((ws_a, "A"), (ws_b, "B"), (ws_r, "ref")):
            g = _recv_until(ws, lambda m: m["type"] == "subsegment_gap")
            assert g["round_id"] == rid
            assert g["level_index"] == 0 and g["seq"] == 2
            assert g["seat"] == "PLAYER_A" and g["sample_ms"] == 5200
            assert g["hit_seat"] == "PLAYER_B" and g["hit_ms"] == 6100
            assert g["gap_ms"] == 900, tag


def test_hit_on_stationary_sample(world) -> None:  # type: ignore[no-untyped-def]
    """零位移样本（照存不建面）同样可被命中——过关强制同步针对的就是对方的
    触碰时刻末样本，它可能合法为零位移。"""
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
                "type": "subsegment_sample",
                "round_id": rid,
                "level_index": 0,
                "seq": 41,
                "t_ms": 30000,
                "px": 9.0,
                "py": 8.0,
                "pz": 7.0,
                "dx": 0.0,
                "dy": 0.0,
                "dz": 0.0,
            }
        )
        _recv_until(ws_b, lambda m: m["type"] == "subsegment_sample")
        _hit(ws_b, rid, level=0, seq=41, t_ms=31250)
        g = _recv_until(ws_a, lambda m: m["type"] == "subsegment_gap")
        assert g["sample_ms"] == 30000 and g["hit_ms"] == 31250
        assert g["gap_ms"] == 1250


def test_hit_once_only(world) -> None:  # type: ignore[no-untyped-def]
    """同一 (hitter, level, seq) 二次命中 → 不再广播时间差。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        _sample(ws_a, rid, level=0, seq=0, t_ms=1000)
        _recv_until(ws_b, lambda m: m["type"] == "subsegment_sample")
        _hit(ws_b, rid, level=0, seq=0, t_ms=1500)
        _recv_until(ws_a, lambda m: m["type"] == "subsegment_gap")
        _hit(ws_b, rid, level=0, seq=0, t_ms=9000)  # 重复命中
        _assert_absent_since_ping(ws_a, ws_a, "subsegment_gap")


def test_hit_unknown_sample_ignored(world) -> None:  # type: ignore[no-untyped-def]
    """命中引用不存在的样本（乱序/丢失）→ 静默忽略。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        _hit(ws_b, rid, level=3, seq=7, t_ms=12000)
        _assert_absent_since_ping(ws_a, ws_a, "subsegment_gap")
        _assert_absent_since_ping(ws_b, ws_b, "error")


def test_stale_round_ignored(world) -> None:  # type: ignore[no-untyped-def]
    """过期/伪造 round_id 的采样与命中 → 静默丢弃（不中转、不广播）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        bogus = "no-such-round"
        _sample(ws_a, bogus, level=0, seq=0, t_ms=1000)
        _hit(ws_b, bogus, level=0, seq=0, t_ms=2000)
        assert rid  # 真回合 id 仅用于对照
        _assert_absent_since_ping(ws_b, ws_b, "subsegment_sample")
        _assert_absent_since_ping(ws_a, ws_a, "subsegment_gap")


def test_single_pick_ignored(world) -> None:  # type: ignore[no-untyped-def]
    """SINGLE 回合（level_index=尝试序号，语义不同）→ 采样不中转。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a, pick="CT01", retry=1)
        _sample(ws_a, rid, level=0, seq=0, t_ms=1000)
        _assert_absent_since_ping(ws_b, ws_b, "subsegment_sample")


# ---------------------------------------------------------------------------
# 断线重连补放
# ---------------------------------------------------------------------------


def test_reconnect_replays_opponent_samples(world) -> None:  # type: ignore[no-untyped-def]
    """重连方 reconnect_resync → 仅对方的采样按原序补放；随后新采样照常中转。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        # B 断线重连（A 保持在线）：with 退出 = B 断线
        with client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b:
            _drain(ws_b, 5)
            rid = _drive_to_round(ws_r, ws_a)
            # 双方各发数个样本（B 重连后只应回放 A 的）
            for seq in (0, 1, 2):
                _sample(ws_a, rid, level=0, seq=seq, t_ms=1000 * (seq + 1))
            _sample(ws_b, rid, level=0, seq=0, t_ms=500)
            # 等 B 收到 A 的 seq=2 中转，确保服务端已存全部样本再断线
            _recv_until(
                ws_b, lambda m: m["type"] == "subsegment_sample" and m["seq"] == 2
            )
        with client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b2:
            _recv_until(ws_b2, lambda m: m["type"] == "auth_ok")
            ws_b2.send_json({"type": "reconnect_resync", "round_id": rid})
            got = _collect_until(
                ws_b2, lambda m: m["type"] == "subsegment_sample" and m["seq"] == 2
            )
            replays = [m for m in got if m["type"] == "subsegment_sample"]
            assert [m["seq"] for m in replays] == [0, 1, 2]
            assert all(m["seat"] == "PLAYER_A" for m in replays)
            # 快照（player_status ×2）先于采样补放
            statuses = [m for m in got if m["type"] == "player_status"]
            assert len(statuses) == 2
            assert got.index(statuses[0]) < got.index(replays[0])
            # 回放后新采样照常实时中转（服务端去重不阻塞新键）
            _sample(ws_a, rid, level=0, seq=3, t_ms=9000)
            live = _recv_until(
                ws_b2, lambda m: m["type"] == "subsegment_sample" and m["seq"] == 3
            )
            assert live["t_ms"] == 9000


# ---------------------------------------------------------------------------
# 协议严格性
# ---------------------------------------------------------------------------


def test_protocol_strict() -> None:
    """extra/missing 字段均 ValidationError（pydantic extra=forbid 契约）。"""

    def parse(obj: dict) -> None:  # type: ignore[no-untyped-def]
        parse_client_message(json.dumps(obj))

    good = {
        "type": "subsegment_sample",
        "round_id": "r1",
        "level_index": 0,
        "seq": 0,
        "t_ms": 100,
        "px": 1.0,
        "py": 2.0,
        "pz": 3.0,
        "dx": 0.0,
        "dy": 0.0,
        "dz": 0.0,
    }
    parse(good)
    with pytest.raises(ValidationError):
        parse({**good, "extra": 1})
    with pytest.raises(ValidationError):
        parse({k: v for k, v in good.items() if k != "px"})
    hit_good = {
        "type": "subsegment_hit",
        "round_id": "r1",
        "level_index": 0,
        "seq": 0,
        "t_ms": 100,
    }
    parse(hit_good)
    with pytest.raises(ValidationError):
        parse({**hit_good, "t_ms": "abc"})
