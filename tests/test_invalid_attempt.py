"""无效尝试成绩标记验收测试（INVALID_ATTEMPT_REQ §6）。

WS 层：level_time_upload.invalid_reasons → Attempt(INVALID) 入账、
SrvLevelTimeUpdate / player_status 下发携带、计分排除、全无效退出弃权、
断线重连补传（幂等 upsert 保留 INVALID 状态与原因）、MULTI 透传不影响总分。
计分纯函数用例另见 test_scoring.py。
"""

from __future__ import annotations

from twilightcupbackend.auth import issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.datatypes import AttemptStatus

PHASE_JUDGING = 4


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 40):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def _drive_to_round_start(  # type: ignore[no-untyped-def]
    ws_r, ws_a, pick: str, retry: int | None = None
):
    """CP01 图池自带 retry_count=3，不得再传 retry（该参数仅 CT/EX 可提交）。"""
    ws_r.send_json({"type": "referee_mark_prep"})
    _drain(ws_r, 2)
    msg: dict = {"type": "referee_select_pick", "pick_code": pick}
    if retry is not None:
        msg["retry_count"] = retry
    ws_r.send_json(msg)
    _drain(ws_r, 1)
    ws_r.send_json({"type": "referee_manual_start"})
    return _recv_until(ws_a, lambda m: m["type"] == "round_start")


def _upload(  # type: ignore[no-untyped-def]
    ws, rid: str, index: int, ms: int, invalid: list[str] | None = None
) -> None:
    msg: dict = {
        "type": "level_time_upload",
        "round_id": rid,
        "level_index": index,
        "this_level_ms": ms,
    }
    if invalid is not None:
        msg["invalid_reasons"] = invalid
    ws.send_json(msg)


def _attempt_of(status_msg: dict, index: int) -> dict:
    return next(a for a in status_msg["attempts"] if a["index"] == index)


def _own_status(ws, seat: str):  # type: ignore[no-untyped-def]
    return _recv_until(
        ws, lambda m: m["type"] == "player_status" and m["seat"] == seat
    )


# ---------------------------------------------------------------------------
# 验收 1：跳档通关 → INVALID 入账，时长得保留，广播携带原因
# ---------------------------------------------------------------------------


def test_single_invalid_attempt_recorded(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "CP01")
        rid = rs["round_id"]

        # 尝试 0 带可原谅标记通关；尝试 1 干净通关（更快）
        _upload(ws_a, rid, 0, 40_000, invalid=["CheckpointSkip"])
        _upload(ws_a, rid, 1, 30_000)

        # 广播顺序是 level_time_update → player_status，按序断言
        ltu = _recv_until(ws_r, lambda m: m["type"] == "level_time_update")
        assert ltu["invalid_reasons"] == ["CheckpointSkip"]

        # 取「已含尝试 1」的那条快照（第一条只反映尝试 0）
        status = _recv_until(
            ws_r,
            lambda m: m["type"] == "player_status"
            and m["seat"] == "PLAYER_A"
            and any(a["index"] == 1 for a in m["attempts"]),
        )
        a0 = _attempt_of(status, 0)
        assert a0["status"] == AttemptStatus.INVALID
        assert a0["time_ms"] == 40_000  # 证据保留
        assert a0["invalid_reasons"] == ["CheckpointSkip"]
        a1 = _attempt_of(status, 1)
        assert a1["status"] == AttemptStatus.VALID
        assert a1["invalid_reasons"] == []

        # FASTEST：成绩取有效最小值 30s，不是 40s
        _complete = {"type": "project_complete", "round_id": rid}
        ws_a.send_json(_complete)
        _upload(ws_b, rid, 0, 35_000)
        ws_b.send_json(_complete)
        _recv_until(ws_r, lambda x: x["type"] == "phase_change" and x["phase"] == 4)
        ws_r.send_json({"type": "referee_verdict", "round_id": rid, "verdict": 1})
        result = _recv_until(ws_r, lambda x: x["type"] == "round_result")
        assert result["score_a_ms"] == 30_000


# ---------------------------------------------------------------------------
# 验收 5：所有尝试全无效后退出 → 弃权（0 有效成绩路径）
# ---------------------------------------------------------------------------


def test_single_all_invalid_exit_forfeits(world) -> None:  # type: ignore[no-untyped-def]
    client, db, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "CP01")
        rid = rs["round_id"]

        # 两次尝试都带不可原谅标记通关 → 0 有效 → forfeit_signal
        _upload(ws_a, rid, 0, 20_000, invalid=["!CheatCode"])
        _upload(ws_a, rid, 1, 15_000, invalid=["!TimeScale"])
        ws_a.send_json(
            {"type": "forfeit_signal", "round_id": rid, "reason": "single_exit_0_valid"}
        )
        # 进入判定需双方终态：B 正常完成补位
        _upload(ws_b, rid, 0, 35_000)
        ws_b.send_json({"type": "project_complete", "round_id": rid})
        _recv_until(ws_r, lambda x: x["type"] == "phase_change" and x["phase"] == 4)

        record = db.rounds.get(rid)
        assert record is not None
        assert record.state_a.forfeited
        assert all(
            a.status == AttemptStatus.INVALID and a.invalid_reasons
            for a in record.state_a.attempts
        )


# ---------------------------------------------------------------------------
# 验收 6：断线重连补传无效尝试 → 幂等 upsert 保留 INVALID 状态与原因
# ---------------------------------------------------------------------------


def test_reconnect_resync_carries_invalid(world) -> None:  # type: ignore[no-untyped-def]
    client, db, session, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "CP01")
        rid = rs["round_id"]
        _upload(ws_a, rid, 0, 40_000, invalid=["CheckpointSkip"])

    # 断线后重连 + 补传（幂等：同 index 重复上报仍是 INVALID、原因随记录走）
    tok_a = issue_token(db.accounts.get(session.player_a_id), settings)
    with client.websocket_connect(f"/ws/{tok_a}") as ws_a2:
        _drain(ws_a2, 6)
        ws_a2.send_json({"type": "reconnect_resync", "round_id": rid})
        snapshot = _own_status(ws_a2, "PLAYER_A")
        a0 = _attempt_of(snapshot, 0)
        assert a0["status"] == AttemptStatus.INVALID
        assert a0["invalid_reasons"] == ["CheckpointSkip"]
        assert a0["time_ms"] == 40_000

        _upload(ws_a2, rid, 0, 40_000, invalid=["CheckpointSkip"])
        again = _own_status(ws_a2, "PLAYER_A")
        a0b = _attempt_of(again, 0)
        assert a0b["status"] == AttemptStatus.INVALID
        assert a0b["invalid_reasons"] == ["CheckpointSkip"]


# ---------------------------------------------------------------------------
# 验收 7：MULTI 关卡带标记完成 → 原因下发裁判端，总分不变
# ---------------------------------------------------------------------------


def test_multi_level_marks_informational(world) -> None:  # type: ignore[no-untyped-def]
    client, db, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "ML1")
        rid = rs["round_id"]

        _upload(ws_a, rid, 0, 60_000, invalid=["CheckpointSkip"])
        ltu = _recv_until(ws_r, lambda m: m["type"] == "level_time_update")
        assert ltu["invalid_reasons"] == ["CheckpointSkip"]

        status = _own_status(ws_r, "PLAYER_A")
        lv0 = next(
            l for l in status["completed_levels"] if l["level_index"] == 0
        )
        assert lv0["invalid_reasons"] == ["CheckpointSkip"]
        assert lv0["time_ms"] == 60_000

        # 总分不受影响：正常完成判分走 final_total_ms
        ws_a.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": 90_000}
        )
        _upload(ws_b, rid, 0, 50_000)
        ws_b.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": 120_000}
        )
        _recv_until(ws_r, lambda x: x["type"] == "phase_change" and x["phase"] == 4)
        ws_r.send_json({"type": "referee_verdict", "round_id": rid, "verdict": 1})
        result = _recv_until(ws_r, lambda x: x["type"] == "round_result")
        assert result["score_a_ms"] == 90_000
        assert result["score_b_ms"] == 120_000
