"""round_start.pick.single_scoring 下发验收测试（backend-round-start-single-scoring）。

fixture 默认比赛为 FASTEST；AVERAGE 用例按 test_pick_tags 的套路另插一场
scoring_method=AVERAGE 的 RUNNING 比赛（created_at 更新 → 选手/裁判连入优先命中）。
"""

from __future__ import annotations

from twilightcupbackend.auth import issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.datatypes import (
    Match,
    MatchStatus,
    RoundRecord,
    ScoringMethod,
)

PHASE_JUDGING = 4


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 30):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def _select(  # type: ignore[no-untyped-def]
    ws_r, pick: str, tags: list[str] | None = None, retry: int | None = None
) -> None:
    msg: dict = {"type": "referee_select_pick", "pick_code": pick}
    if tags is not None:
        msg["tags"] = tags
    if retry is not None:
        msg["retry_count"] = retry
    ws_r.send_json(msg)


def _drive_to_round_start(  # type: ignore[no-untyped-def]
    ws_r, ws_a, pick: str, tags: list[str] | None = None, retry: int | None = None
):
    ws_r.send_json({"type": "referee_mark_prep"})
    _drain(ws_r, 2)
    _select(ws_r, pick, tags, retry)
    _drain(ws_r, 1)
    ws_r.send_json({"type": "referee_manual_start"})
    return _recv_until(ws_a, lambda m: m["type"] == "round_start")


def _upload(ws, rid: str, index: int, ms: int) -> None:  # type: ignore[no-untyped-def]
    ws.send_json(
        {
            "type": "level_time_upload",
            "round_id": rid,
            "level_index": index,
            "this_level_ms": ms,
        }
    )


def _complete(ws, rid: str, final: int | None = None) -> None:  # type: ignore[no-untyped-def]
    ws.send_json({"type": "project_complete", "round_id": rid, "final_total_ms": final})


def _match_with_scoring(db, session, method: ScoringMethod) -> Match:  # type: ignore[no-untyped-def]
    m = Match(
        name=f"scoring-{method.name.lower()}",
        bo_format=3,
        win_threshold=2,
        scoring_method=method,
        start_countdown_delay=2,
        mappool=session.mappool,
        player_a_id=session.player_a_id,
        player_b_id=session.player_b_id,
        referee_id=session.referee_id,
        director_id=session.director_id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(m)
    return m


# ---------------------------------------------------------------------------
# A1：AVERAGE 赛制 SINGLE 回合下发 "average"，且与服务端判分口径一致
# ---------------------------------------------------------------------------


def test_average_match_single_round(world) -> None:  # type: ignore[no-untyped-def]
    client, db, session, _ = world
    _match_with_scoring(db, session, ScoringMethod.AVERAGE)
    tok = {
        k: issue_token(db.accounts.get(v), settings)
        for k, v in {
            "pa": session.player_a_id,
            "pb": session.player_b_id,
            "ref": session.referee_id,
        }.items()
    }
    with (
        client.websocket_connect(f"/ws/{tok['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tok['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tok['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "CT01", retry=3)
        rid = rs["round_id"]
        assert rs["pick"]["single_scoring"] == "average"
        # A：两次有效（1000/2000）+ 跳过一次 → 平均 1500（跳过剔除）
        _upload(ws_a, rid, 0, 1000)
        _upload(ws_a, rid, 1, 2000)
        ws_a.send_json({"type": "attempt_skip", "round_id": rid, "attempt_index": 2})
        _complete(ws_a, rid)
        # B：单次有效 1000 → 平均 1000
        _upload(ws_b, rid, 0, 1000)
        _complete(ws_b, rid)
        _recv_until(ws_r, lambda x: x["type"] == "phase_change" and x["phase"] == 4)
        ws_r.send_json({"type": "referee_verdict", "round_id": rid, "verdict": 2})
        result = _recv_until(ws_r, lambda x: x["type"] == "round_result")
        # 服务端判分（round_result）与下发口径一致：AVERAGE=算术平均
        assert result["score_a_ms"] == 1500
        assert result["score_b_ms"] == 1000
    # 快照冻结在回合记录上（重连/审计不依赖重发）；图池原件恒为 None
    record = db.rounds.get(rid)
    assert record is not None
    assert record.pick_snapshot.single_scoring == "average"
    assert session.mappool.get_pick("CT01").single_scoring is None


# ---------------------------------------------------------------------------
# A2：FASTEST 赛制行为与现状一致（下发 "fastest"，判分取最小值）
# ---------------------------------------------------------------------------


def test_fastest_match_single_round(world) -> None:  # type: ignore[no-untyped-def]
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
        assert rs["pick"]["single_scoring"] == "fastest"
        _upload(ws_a, rid, 0, 3000)
        _upload(ws_a, rid, 1, 1000)
        _complete(ws_a, rid)
        _upload(ws_b, rid, 0, 1200)
        _complete(ws_b, rid)
        _recv_until(ws_r, lambda x: x["type"] == "phase_change" and x["phase"] == 4)
        ws_r.send_json({"type": "referee_verdict", "round_id": rid, "verdict": 1})
        result = _recv_until(ws_r, lambda x: x["type"] == "round_result")
        assert result["score_a_ms"] == 1000  # FASTEST=最小值，现状不变
        assert result["score_b_ms"] == 1200


# ---------------------------------------------------------------------------
# A3：MULTI 回合统一下发该字段，但不影响多关判分
# ---------------------------------------------------------------------------


def test_multi_round_unaffected(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "ML1")
        rid = rs["round_id"]
        # 字段统一下发（客户端忽略）；多关判分不受影响
        assert rs["pick"]["single_scoring"] == "fastest"
        _upload(ws_a, rid, 0, 600)
        _complete(ws_a, rid, final=1000)
        _upload(ws_b, rid, 0, 500)
        _complete(ws_b, rid, final=2000)
        _recv_until(ws_r, lambda x: x["type"] == "phase_change" and x["phase"] == 4)
        ws_r.send_json({"type": "referee_verdict", "round_id": rid, "verdict": 1})
        result = _recv_until(ws_r, lambda x: x["type"] == "round_result")
        assert result["score_a_ms"] == 1000  # 多关取 final_total_ms
        assert result["score_b_ms"] == 2000


# ---------------------------------------------------------------------------
# A4：重赛沿用冻结快照 → 新 round_start 仍带 single_scoring
# ---------------------------------------------------------------------------


def test_rematch_preserves_single_scoring(world) -> None:  # type: ignore[no-untyped-def]
    client, db, session, _ = world
    _match_with_scoring(db, session, ScoringMethod.AVERAGE)
    tok = {
        k: issue_token(db.accounts.get(v), settings)
        for k, v in {
            "pa": session.player_a_id,
            "ref": session.referee_id,
        }.items()
    }
    with (
        client.websocket_connect(f"/ws/{tok['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tok['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "CP01")
        rid = rs["round_id"]
        assert rs["pick"]["single_scoring"] == "average"
        _complete(ws_a, rid)
        # 仅 A 在场：用 B 令牌补一条连接完成回合，凑齐双方终态进入判定
        tok_b = issue_token(db.accounts.get(session.player_b_id), settings)
        with client.websocket_connect(f"/ws/{tok_b}") as ws_b:
            _drain(ws_b, 5)
            _complete(ws_b, rid)
            _recv_until(ws_r, lambda x: x["type"] == "phase_change" and x["phase"] == 4)
            ws_r.send_json({"type": "referee_verdict", "round_id": rid, "verdict": 3})
            _recv_until(ws_r, lambda x: x["type"] == "phase_change" and x["phase"] == 1)
        # 重赛：沿用冻结的 pick 快照，直接手动开局
        ws_r.send_json({"type": "referee_manual_start"})
        rs2 = _recv_until(ws_a, lambda m: m["type"] == "round_start", max_msgs=30)
        assert rs2["pick"]["single_scoring"] == "average"


# ---------------------------------------------------------------------------
# A5：旧回合记录缺字段 → 反序列化为 None（持久层兼容）
# ---------------------------------------------------------------------------


def test_legacy_round_record_defaults_to_none() -> None:
    old = RoundRecord.model_validate(
        {
            "match_id": "m1",
            "round_no": 1,
            "pick_code": "CT01",
            "pick_snapshot": {
                "code": "CT01",
                "name": "词条单关",
                "type": 2,
                "retry_count": 3,
                "collection": {"raw": {"level": "lv1"}},
                "tags": [],
            },
            "collection_snapshot": {"raw": {"level": "lv1"}},
            "state_a": {"account_id": "a"},
            "state_b": {"account_id": "b"},
        }
    )
    assert old.pick_snapshot.single_scoring is None
