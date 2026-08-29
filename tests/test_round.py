"""M7 回合计时 + 计分集成测试（经 WS 走完整回合流程）。

fixture 的 start_countdown_delay=2，每个回合开始约耗时 2 秒。
"""

from __future__ import annotations

from twilightcupbackend.auth import issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.datatypes import Match, MatchStatus, ScoringMethod

PHASE = {
    "IDLE": 0,
    "PREP": 1,
    "COUNTDOWN": 2,
    "IN_ROUND": 3,
    "ROUND_JUDGING": 4,
    "ROUND_END": 5,
    "MATCH_END": 6,
}


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


def test_full_round_a_wins(world) -> None:  # type: ignore[no-untyped-def]
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
                "this_level_ms": 1000,
                "total_ms": 1000,
            }
        )
        ws_a.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": 1000}
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
        ws_b.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": 2000}
        )
        _recv_until(
            ws_r,
            lambda m: (
                m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_JUDGING"]
            ),
        )
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 1}  # A_WIN
        )
        cum = _recv_until(ws_r, lambda m: m["type"] == "cumulative_score")
        assert cum["wins_a"] == 1 and cum["wins_b"] == 0
        _recv_until(
            ws_r,
            lambda m: m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_END"],
        )


def test_forfeit(world) -> None:  # type: ignore[no-untyped-def]
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
            {"type": "forfeit_signal", "round_id": rid, "reason": "multi_exit"}
        )
        ws_b.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": 5000}
        )
        _recv_until(
            ws_r,
            lambda m: (
                m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_JUDGING"]
            ),
        )
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 2}  # B_WIN
        )
        cum = _recv_until(ws_r, lambda m: m["type"] == "cumulative_score")
        assert cum["wins_b"] == 1
        # 比分同步的全场系统消息：Twilight 前缀，选手席与裁判席逐字一致；
        # 格式为「用户名 比分 : 比分 用户名」（登录名而非展示名）
        score_r = _recv_until(
            ws_r, lambda m: m["type"] == "system" and m["kind"] == "score"
        )
        assert score_r["sender"] == "Twilight"
        assert score_r["text"] == "pa 0 : 1 pb"
        score_b = _recv_until(
            ws_b, lambda m: m["type"] == "system" and m["kind"] == "score"
        )
        assert score_b["text"] == score_r["text"]


def test_rematch(world) -> None:  # type: ignore[no-untyped-def]
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
            {"type": "project_complete", "round_id": rid, "final_total_ms": 1000}
        )
        ws_b.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": 1000}
        )
        _recv_until(
            ws_r,
            lambda m: (
                m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_JUDGING"]
            ),
        )
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 3}  # TIE_REMATCH
        )
        # 平局 → 重赛：回到 PREP，且产生新回合
        prep = _recv_until(
            ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == PHASE["PREP"]
        )
        assert prep["round_id"] is not None and prep["round_id"] != rid


def test_match_end_threshold_one_waits_for_manual_end(world) -> None:  # type: ignore[no-untyped-def]
    client, db, session, _ = world
    # 新建一个 win_threshold=1 的比赛（created_at 更新 → WS 会优先选中它）
    th1 = Match(
        name="速决",
        bo_format=1,
        win_threshold=1,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=session.mappool,
        player_a_id=session.player_a_id,
        player_b_id=session.player_b_id,
        referee_id=session.referee_id,
        director_id=session.director_id,
        status=MatchStatus.RUNNING,  # 选手连入需 RUNNING；已是最新，故成为其当前场
    )
    db.matches.insert(th1)
    tok = {k: issue_token(_acct(db, k), settings) for k in ("pa", "pb", "ref")}

    with (
        client.websocket_connect(f"/ws/{tok['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tok['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tok['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        ws_a.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": 1000}
        )
        ws_b.send_json(
            {"type": "project_complete", "round_id": rid, "final_total_ms": 2000}
        )
        _recv_until(
            ws_r,
            lambda m: (
                m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_JUDGING"]
            ),
        )
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 1}  # A_WIN
        )
        # 达到取胜分数后不再自动结束：停在 ROUND_END，等待裁判手动收尾
        _recv_until(
            ws_r,
            lambda m: m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_END"],
        )
        assert db.matches.get(th1.id).status == MatchStatus.RUNNING
        assert db.matches.get(th1.id).winner is None
        # 裁判手动结束
        ws_r.send_json({"type": "referee_end_match"})
        sys_end = _recv_until(
            ws_r,
            lambda m: m["type"] == "system" and m["kind"] == "match_end",
        )
        assert "A" in sys_end["text"]
        end = _recv_until(ws_r, lambda m: m["type"] == "match_end")
        assert end["winner"] == "A"
        _recv_until(
            ws_r,
            lambda m: m["type"] == "phase_change" and m["phase"] == PHASE["MATCH_END"],
        )
        assert db.matches.get(th1.id).status == MatchStatus.ENDED
        assert db.matches.get(th1.id).winner == "A"
        # 手动结束后踢出双方选手（排空收尾广播后触发断开）
        from starlette.websockets import WebSocketDisconnect

        for ws in (ws_a, ws_b):
            disconnected = False
            try:
                for _ in range(50):
                    ws.receive_json()
            except WebSocketDisconnect:
                disconnected = True
            assert disconnected


def test_end_match_rejected_before_threshold(world) -> None:  # type: ignore[no-untyped-def]
    """未达阈值时 referee_end_match 被拒；判 1:0 后停在 ROUND_END
    （conftest threshold=2）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        for ws, ms in ((ws_a, 1000), (ws_b, 2000)):
            ws.send_json(
                {"type": "project_complete", "round_id": rid, "final_total_ms": ms}
            )
        _recv_until(
            ws_r,
            lambda m: (
                m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_JUDGING"]
            ),
        )
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 1}  # A_WIN → 1:0
        )
        # 1:0 未达阈值 2 → 不自动结束，停在 ROUND_END
        _recv_until(
            ws_r,
            lambda m: m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_END"],
        )
        ws_r.send_json({"type": "referee_end_match"})
        err = _recv_until(ws_r, lambda m: m["type"] == "error")
        assert err["code"] == 400 and "Winner not decided" in err["msg"]


def test_auto_end_when_all_players_and_director_disconnect(
    world,
) -> None:  # type: ignore[no-untyped-def]
    """胜负已定后不自动结束；双方选手与导播全部断开时自动收尾。"""
    client, db, session, tokens = world
    th1 = Match(
        name="断线自动收尾",
        bo_format=3,
        win_threshold=1,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=session.mappool,
        player_a_id=session.player_a_id,
        player_b_id=session.player_b_id,
        referee_id=session.referee_id,
        director_id=session.director_id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(th1)

    with client.websocket_connect(
        f"/ws/{tokens['ref']}?match={th1.id}"
    ) as ws_r:
        _drain(ws_r, 5)
        with (
            client.websocket_connect(
                f"/ws/{tokens['pa']}?match={th1.id}"
            ) as ws_a,
            client.websocket_connect(
                f"/ws/{tokens['pb']}?match={th1.id}"
            ) as ws_b,
            client.websocket_connect(
                f"/ws/{tokens['dri']}?match={th1.id}"
            ) as ws_d,
        ):
            for ws in (ws_a, ws_b, ws_d):
                _drain(ws, 5)
            rid = _drive_to_round(ws_r, ws_a)
            ws_a.send_json(
                {"type": "project_complete", "round_id": rid, "final_total_ms": 1000}
            )
            ws_b.send_json(
                {"type": "project_complete", "round_id": rid, "final_total_ms": 2000}
            )
            _recv_until(
                ws_r,
                lambda m: (
                    m["type"] == "phase_change"
                    and m["phase"] == PHASE["ROUND_JUDGING"]
                ),
            )
            ws_r.send_json(
                {"type": "referee_verdict", "round_id": rid, "verdict": 1}
            )
            _recv_until(
                ws_r,
                lambda m: (
                    m["type"] == "phase_change"
                    and m["phase"] == PHASE["ROUND_END"]
                ),
            )
            assert db.matches.get(th1.id).status == MatchStatus.RUNNING
        # 双方选手 + 导播全部断开后自动结束
        end = _recv_until(ws_r, lambda m: m["type"] == "match_end")
        assert end["winner"] == "A"
        assert db.matches.get(th1.id).status == MatchStatus.ENDED
        assert db.matches.get(th1.id).winner == "A"


def test_prep_blocked_after_manual_end(world) -> None:  # type: ignore[no-untyped-def]
    """裁判手动结束后：所有操作均被只读守卫拒绝，比赛不可复活。"""
    client, db, session, _ = world
    # 新建 threshold=1 的比赛便于一轮达阈（并成为选手当前场）
    th1 = Match(
        name="手动结束",
        bo_format=3,
        win_threshold=1,
        scoring_method=ScoringMethod.FASTEST,
        start_countdown_delay=2,
        mappool=session.mappool,
        player_a_id=session.player_a_id,
        player_b_id=session.player_b_id,
        referee_id=session.referee_id,
        director_id=session.director_id,
        status=MatchStatus.RUNNING,
    )
    db.matches.insert(th1)
    tok = {k: issue_token(_acct(db, k), settings) for k in ("pa", "pb", "ref")}
    with (
        client.websocket_connect(f"/ws/{tok['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tok['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tok['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        rid = _drive_to_round(ws_r, ws_a)
        for ws, ms in ((ws_a, 1000), (ws_b, 2000)):
            ws.send_json(
                {"type": "project_complete", "round_id": rid, "final_total_ms": ms}
            )
        _recv_until(
            ws_r,
            lambda m: (
                m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_JUDGING"]
            ),
        )
        ws_r.send_json(
            {"type": "referee_verdict", "round_id": rid, "verdict": 1}  # A_WIN → 1:0
        )
        # 达阈值后不自动结束，先停 ROUND_END；裁判手动结束
        _recv_until(
            ws_r,
            lambda m: m["type"] == "phase_change" and m["phase"] == PHASE["ROUND_END"],
        )
        ws_r.send_json({"type": "referee_end_match"})
        _recv_until(
            ws_r,
            lambda m: m["type"] == "phase_change" and m["phase"] == PHASE["MATCH_END"],
        )
        # 已结束：只读守卫拦截所有操作（error 403，而非继续进入状态机）
        ws_r.send_json({"type": "referee_mark_prep"})
        err1 = _recv_until(ws_r, lambda m: m["type"] == "error")
        assert err1["code"] == 403
        ws_r.send_json({"type": "referee_end_match"})
        err2 = _recv_until(ws_r, lambda m: m["type"] == "error")
        assert err2["code"] == 403
        ws_r.send_json(
            {"type": "referee_edit_verdict", "round_id": rid, "new_verdict": 3}
        )
        err3 = _recv_until(ws_r, lambda m: m["type"] == "error")
        assert err3["code"] == 403
        assert db.matches.get(th1.id).status == MatchStatus.ENDED


def test_end_match_requires_referee_seat(world) -> None:  # type: ignore[no-untyped-def]
    """非裁判（选手连接）发 referee_end_match → 403。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        ws_a.send_json({"type": "referee_end_match"})
        err = _recv_until(ws_a, lambda m: m["type"] == "error")
        assert err["code"] == 403


def _acct(db, uname: str):  # type: ignore[no-untyped-def]
    return db.accounts.get_by_username(uname)
