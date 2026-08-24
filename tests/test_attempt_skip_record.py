"""跳过的单关尝试落库（backend-attempt-skip-record）验收测试。

fixture 图池 CT01 为 SINGLE；start_countdown_delay=2。
"""

from __future__ import annotations

from twilightcupbackend.datatypes import AttemptStatus


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 40):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def _drive_to_round(ws_r, ws_a, pick: str = "CT01") -> str:  # type: ignore[no-untyped-def]
    ws_r.send_json({"type": "referee_mark_prep"})
    _drain(ws_r, 2)
    # CT 单关的重试次数改由裁判选图时指定（必填）
    retry = {"retry_count": 1} if pick == "CT01" else {}
    ws_r.send_json({"type": "referee_select_pick", "pick_code": pick, **retry})
    _drain(ws_r, 1)
    ws_r.send_json({"type": "referee_manual_start"})
    return _recv_until(ws_a, lambda m: m["type"] == "round_start")["round_id"]


def _upload(ws, rid: str, index: int, ms: int) -> None:  # type: ignore[no-untyped-def]
    ws.send_json(
        {
            "type": "level_time_upload",
            "round_id": rid,
            "level_index": index,
            "this_level_ms": ms,
        }
    )


def _skip(ws, rid: str, index: int) -> None:  # type: ignore[no-untyped-def]
    ws.send_json({"type": "attempt_skip", "round_id": rid, "attempt_index": index})


def _status_of(ws_a, expect: int, max_msgs: int = 60):  # type: ignore[no-untyped-def]
    """收取第 expect 条 player_status 广播（每条上行触发一次），返回它。

    上行条数与广播一一对应，读到第 expect 条即为全部上行的最终状态。
    """
    seen = 0
    for _ in range(max_msgs):
        m = ws_a.receive_json()
        if m["type"] == "player_status":
            seen += 1
            if seen == expect:
                return m
    raise AssertionError("未在限定消息内收到足够的 player_status")


def test_skipped_attempt_recorded(world) -> None:  # type: ignore[no-untyped-def]
    """验收 1：通过1 → 跳过2 → 通过3 → 明细含三条，#2 为 SKIPPED/time_ms=None。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        _drain(ws_r, 5)
        _drain(ws_a, 6)
        rid = _drive_to_round(ws_r, ws_a)
        _upload(ws_a, rid, 1, 12345)
        _skip(ws_a, rid, 2)
        _upload(ws_a, rid, 3, 9876)
        status = _status_of(ws_a, 3)
        attempts = sorted(status["attempts"], key=lambda a: a["index"])
        assert [a["index"] for a in attempts] == [1, 2, 3]
        assert attempts[1]["status"] == AttemptStatus.SKIPPED
        assert attempts[1]["time_ms"] is None
        assert attempts[0]["time_ms"] == 12345
        assert attempts[2]["time_ms"] == 9876


def test_skip_then_upload_overwrites(world) -> None:  # type: ignore[no-untyped-def]
    """验收 2：先跳过、后补传成绩 → 该条目覆盖为 VALID。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        _drain(ws_r, 5)
        _drain(ws_a, 6)
        rid = _drive_to_round(ws_r, ws_a)
        _skip(ws_a, rid, 1)
        _upload(ws_a, rid, 1, 5000)
        status = _status_of(ws_a, 2)
        assert len(status["attempts"]) == 1
        assert status["attempts"][0]["status"] == AttemptStatus.VALID
        assert status["attempts"][0]["time_ms"] == 5000


def test_repeat_skip_idempotent(world) -> None:  # type: ignore[no-untyped-def]
    """验收 3：重复跳过同一尝试 → 仍只有一条记录。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        _drain(ws_r, 5)
        _drain(ws_a, 6)
        rid = _drive_to_round(ws_r, ws_a)
        _skip(ws_a, rid, 1)
        _skip(ws_a, rid, 1)
        status = _status_of(ws_a, 2)
        assert len(status["attempts"]) == 1
        assert status["attempts"][0]["status"] == AttemptStatus.SKIPPED


def test_skip_persisted_to_round_record(world) -> None:  # type: ignore[no-untyped-def]
    """验收 5：回合明细（RoundRecord.attempts）同样包含跳过的尝试。"""
    client, db, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        _drain(ws_r, 5)
        _drain(ws_a, 6)
        rid = _drive_to_round(ws_r, ws_a)
        _upload(ws_a, rid, 1, 1000)
        _skip(ws_a, rid, 2)
        _status_of(ws_a, 2)
        record = db.rounds.get(rid)
        assert record is not None
        idx = {a.index: a for a in record.state_a.attempts}
        assert idx[2].status == AttemptStatus.SKIPPED
        assert idx[2].time_ms is None
        assert idx[1].time_ms == 1000


def test_multi_round_skip_ignored(world) -> None:  # type: ignore[no-untyped-def]
    """验收 4：多关回合的 attempt_skip 被过滤，不产生任何尝试记录。"""
    client, db, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        _drain(ws_r, 5)
        _drain(ws_a, 6)
        rid = _drive_to_round(ws_r, ws_a, "ML1")
        _skip(ws_a, rid, 1)
        # 无 player_status 广播（被过滤），直接查库确认无 attempts
        record = db.rounds.get(rid)
        assert record is not None
        assert record.state_a.attempts == []
        assert record.state_a.completed_levels == []
