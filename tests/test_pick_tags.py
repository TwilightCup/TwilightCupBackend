"""CT 词条提交与 round_start.pick.tags（backend-ct-pick-tags）验收测试。

fixture 的 start_countdown_delay=2；图池含 ML1（多关/ML）与 CT01（单关/CT）。
"""

from __future__ import annotations

from twilightcupbackend.auth import issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.datatypes import Match, MatchStatus, ScoringMethod

PHASE_PREP = 1


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


# ---------------------------------------------------------------------------
# R1 / R5：开局后 round_start.pick.tags 与广播 tags
# ---------------------------------------------------------------------------


def test_round_start_carries_tags(world) -> None:  # type: ignore[no-untyped-def]
    """R1：CT 单关提交 tags 并开局 → round_start.pick.tags 原样下发。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d,
    ):
        for ws in (ws_r, ws_a, ws_d):
            _drain(ws, 5)
        rs = _drive_to_round_start(
            ws_r, ws_a, "CT01", ["Checkpoint", "Jumpless"], retry=2
        )
        assert rs["pick"]["code"] == "CT01"
        assert rs["pick"]["tags"] == ["Checkpoint", "Jumpless"]
        assert rs["pick"]["retry_count"] == 2
        # §2.4：导播广播也带 tags
        bcast = _recv_until(ws_d, lambda m: m["type"] == "round_started_broadcast")
        assert bcast["tags"] == ["Checkpoint", "Jumpless"]


def test_round_start_without_tags(world) -> None:  # type: ignore[no-untyped-def]
    """R5/R8：不带 tags 键的旧版裁判端 → tags 为空数组，照常开局（retry 仍需指定）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "CT01", retry=1)
        assert rs["pick"]["tags"] == []
        # 非结束本回合无法再开局：双方完成 + 判 B 胜（0:1 未达阈值 2），
        # 停在 ROUND_END 后再来一回合
        rid = rs["round_id"]
        for ws in (ws_a, ws_b):
            ws.send_json(
                {"type": "project_complete", "round_id": rid, "final_total_ms": 1000}
            )
        _recv_until(ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 4)
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 2}  # B_WIN
        )
        _recv_until(ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 5)
        # 非 CT（ML1）不带 tags：图池原件恒为空词条
        rs2 = _drive_to_round_start(ws_r, ws_a, "ML1")
        assert rs2["pick"]["tags"] == []


# ---------------------------------------------------------------------------
# R2 / R3 / R4：服务端校验（客户端已拦截但不可信任）
# ---------------------------------------------------------------------------


def _expect_select_error(  # type: ignore[no-untyped-def]
    world, pick: str, tags: list[str], retry: int | None = 1, want: str = "tag"
) -> None:
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        # select_pick 的词条校验与阶段无关（IDLE/PREP/ROUND_END 均可触发），不走
        # mark_prep：同一 world 里第二次调用时 phase 已是 PREP，mark_prep 会被拒
        # （只回 1 条系统消息），固定 drain 会挂死。（COUNTDOWN/IN_ROUND 会被
        # 阶段守卫直接拒绝，见 tests/test_preload.py。）
        _select(ws_r, pick, tags, retry)
        err = _recv_until(
            ws_r,
            lambda m: m["type"] == "error" and m["code"] == 400,
        )
        assert want in err["msg"].lower()


def test_too_many_tags_rejected(world) -> None:  # type: ignore[no-untyped-def]
    """R2：tags 数量 > ct_tag_count（默认 2）→ error。"""
    _expect_select_error(world, "CT01", ["Glitchless", "Pinch", "Jumpless"])


def test_conflicting_tags_rejected(world) -> None:  # type: ignore[no-untyped-def]
    """R3：Checkpoint + No Checkpoint 互斥 → error。"""
    _expect_select_error(world, "CT01", ["Checkpoint", "No Checkpoint"])


def test_non_ct_tags_rejected(world) -> None:  # type: ignore[no-untyped-def]
    """R4：非词条类别（ML）带非空 tags → error（可携带词条的仅 CT/EX/CP）。"""
    _expect_select_error(world, "ML1", ["Glitchless"], retry=None)


def test_invalid_and_single_only_tags_rejected(world) -> None:  # type: ignore[no-untyped-def]
    """枚举外词条与 MULTI-only 限制：越界值 error；Achievement 仅单关可用。"""
    _expect_select_error(world, "CT01", ["Speedrun"])  # 枚举外


# ---------------------------------------------------------------------------
# R7：重赛沿用原回合词条
# ---------------------------------------------------------------------------


def test_rematch_inherits_tags(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round_start(
            ws_r, ws_a, "CT01", ["Glitchless", "No EC"], retry=3
        )["round_id"]
        ws_a.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": None}
        )
        ws_b.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": None}
        )
        _recv_until(ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 4)
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 3}  # TIE_REMATCH
        )
        _recv_until(
            ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == PHASE_PREP
        )
        # 重赛沿用原 pending pick（不重发 referee_select_pick），裁判直接手动开局
        ws_r.send_json({"type": "referee_manual_start"})
        rs2 = _recv_until(ws_a, lambda m: m["type"] == "round_start", max_msgs=30)
        assert rs2["pick"]["tags"] == ["Glitchless", "No EC"]
        # 重赛沿用裁判指定的重试次数
        assert rs2["pick"]["retry_count"] == 3


# ---------------------------------------------------------------------------
# R9 / R10：ct_tag_count 比赛级配置
# ---------------------------------------------------------------------------


def _match_with_ct_tag_count(db, session, n: int) -> Match:  # type: ignore[no-untyped-def]
    m = Match(
        name=f"ct{n}",
        bo_format=3,
        win_threshold=2,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        ct_tag_count=n,
        mappool=session.mappool,
        player_a_id=session.player_a_id,
        player_b_id=session.player_b_id,
        referee_id=session.referee_id,
        director_id=session.director_id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(m)
    return m


def test_ct_tag_count_zero_disables_tags(world) -> None:  # type: ignore[no-untyped-def]
    """R9：ct_tag_count=0 → 非空 tags 报 error。"""
    client, db, session, _ = world
    _match_with_ct_tag_count(db, session, 0)
    tok = issue_token(db.accounts.get(session.referee_id), settings)
    with client.websocket_connect(f"/ws/{tok}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 2)
        _select(ws_r, "CT01", ["Glitchless"], retry=1)
        err = _recv_until(ws_r, lambda m: m["type"] == "error" and m["code"] == 400)
        assert "max 0" in err["msg"]


def test_ct_tag_count_three_allows_three(world) -> None:  # type: ignore[no-untyped-def]
    """R10：ct_tag_count=3 → 3 个词条开局原样下发。"""
    client, db, session, _ = world
    _match_with_ct_tag_count(db, session, 3)
    tok_a = issue_token(db.accounts.get(session.player_a_id), settings)
    tok_r = issue_token(db.accounts.get(session.referee_id), settings)
    with (
        client.websocket_connect(f"/ws/{tok_r}") as ws_r,
        client.websocket_connect(f"/ws/{tok_a}") as ws_a,
    ):
        _drain(ws_r, 5)
        _drain(ws_a, 6)
        rs = _drive_to_round_start(
            ws_r, ws_a, "CT01", ["Glitchless", "Pinch", "No EC"], retry=1
        )
        assert rs["pick"]["tags"] == ["Glitchless", "Pinch", "No EC"]


def test_ct_tag_count_returned_by_rest(world) -> None:  # type: ignore[no-untyped-def]
    """ct_tag_count 在 MatchOut 回传（默认 2 / 显式 3）。"""
    client, db, session, tokens = world
    # 管理员账号：借用 REST admin 接口需管理员令牌，这里直接查 me 接口回传
    resp = client.get(
        f"/me/matches/{session.id}",
        headers={"Authorization": f"Bearer {tokens['ref']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["ct_tag_count"] == 2  # 缺省 = 2
    m3 = _match_with_ct_tag_count(db, session, 3)
    resp3 = client.get(
        f"/me/matches/{m3.id}", headers={"Authorization": f"Bearer {tokens['ref']}"}
    )
    assert resp3.json()["ct_tag_count"] == 3


# ---------------------------------------------------------------------------
# 新机制：CT/EX 单关重试必填；EX 可带词条；CP 自动 Checkpoint；其余沿用图池预设
# ---------------------------------------------------------------------------


def test_ct_single_retry_required(world) -> None:  # type: ignore[no-untyped-def]
    """CT/EX 单关未指定 retry_count → error。"""
    _expect_select_error(world, "CT01", [], retry=None, want="retry")
    _expect_select_error(world, "EX01", [], retry=None, want="retry")


def test_retry_below_one_rejected(world) -> None:  # type: ignore[no-untyped-def]
    """CT/EX 单关 retry_count < 1 → error。"""
    _expect_select_error(world, "CT01", [], retry=0, want="retry")


def test_non_referee_retry_pick_rejects_override(world) -> None:  # type: ignore[no-untyped-def]
    """非 CT/EX 单关（CP 沿用图池预设）传入 retry_count → error。"""
    _expect_select_error(world, "CP01", [], retry=5, want="retry")


def test_ex_pick_can_carry_tags_and_retry(world) -> None:  # type: ignore[no-untyped-def]
    """EX 单关可带词条 + 重试，开局原样冻结进快照。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "EX01", ["No EC"], retry=4)
        assert rs["pick"]["tags"] == ["No EC"]
        assert rs["pick"]["retry_count"] == 4


def test_cp_pick_carries_checkpoint_and_mappool_retry(world) -> None:  # type: ignore[no-untyped-def]
    """CP 选图自动 Checkpoint，重试沿用图池预设（3）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        rs = _drive_to_round_start(ws_r, ws_a, "CP01", ["Checkpoint"])
        assert rs["pick"]["tags"] == ["Checkpoint"]
        assert rs["pick"]["retry_count"] == 3
