"""M9 日志与日志接口测试：跑完一回合后经 REST 取 match_log/chat/rounds。"""

from __future__ import annotations

from twilightcupbackend.connection_manager import normalize_draft


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 30):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def _drive_to_round(ws_r, ws_a) -> str:  # type: ignore[no-untyped-def]
    ws_r.send_json({"type": "referee_mark_prep"})
    _drain(ws_r, 2)
    ws_r.send_json({"type": "referee_select_pick", "pick_code": "ML1"})
    _drain(ws_r, 1)
    ws_r.send_json({"type": "referee_manual_start"})
    rs = _recv_until(ws_a, lambda m: m["type"] == "round_start")
    return rs["round_id"]


def test_log_endpoints(world) -> None:  # type: ignore[no-untyped-def]
    client, _, session, tokens = world
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
            {"type": "project_complete", "round_id": rid, "final_total_ms": 2000}
        )
        _recv_until(ws_r, lambda m: m["type"] == "phase_change" and m["phase"] == 4)
        ws_r.send_json({"type": "referee_verdict", "round_id": rid, "verdict": 1})
        _recv_until(ws_r, lambda m: m["type"] == "cumulative_score")

    h = {"Authorization": f"Bearer {tokens['ref']}"}
    base = f"/logs/matches/{session.id}"

    ml = client.get(f"{base}/match_log", headers=h)
    assert ml.status_code == 200
    ml_body = ml.json()
    assert ml_body["match_id"] == session.id
    assert rid in ml_body["round_ids"]
    assert ml_body["initial_info"]["win_threshold"] == 2
    # 本场未用草稿引擎（无 draft_sync）→ draft_snapshot 缺省 None（契约 §6.2）
    assert ml_body.get("draft_snapshot") is None

    chat = client.get(f"{base}/chat", headers=h)
    assert chat.status_code == 200
    chat_body = chat.json()
    assert len(chat_body) > 0
    assert any(m["is_system"] for m in chat_body)

    rd = client.get(f"{base}/rounds/1", headers=h)
    assert rd.status_code == 200
    rd_body = rd.json()
    assert rd_body["round_no"] == 1
    assert rd_body["verdict"] == 1  # A_WIN
    assert rd_body["counted"] is True


def test_logs_require_auth(world) -> None:  # type: ignore[no-untyped-def]
    client, _, session, _ = world
    resp = client.get(f"/logs/matches/{session.id}/match_log")
    assert resp.status_code == 401


def test_normalize_draft_strips_ui_state() -> None:
    """normalize 仅保留展示字段，丢弃 stage/roll/计时器等 UI 态；缺字段兜底。"""
    state = {
        "stage": "PICK",
        "rollA": 88,
        "rollB": 12,
        "actions": [{"by": "A", "code": "ML1", "kind": "ban"}],
        "picks": [{"by": "B", "code": 7}],  # code 非字符串也应被 str 化
        "bannedTags": ["某CT词条"],
        "tagBanBy": {"A": "某CT词条", "B": None},
        "timerFlag": True,  # 应被丢弃
    }
    out = normalize_draft(state)
    assert out == {
        "actions": [{"by": "A", "code": "ML1", "kind": "ban"}],
        "picks": [{"by": "B", "code": "7"}],
        "bannedTags": ["某CT词条"],
        "tagBanBy": {"A": "某CT词条", "B": None},
    }

    # 缺字段不报错
    assert normalize_draft({}) == {
        "actions": [],
        "picks": [],
        "bannedTags": [],
        "tagBanBy": {"A": None, "B": None},
    }


def test_draft_sync_persists_snapshot_via_match_log(world) -> None:  # type: ignore[no-untyped-def]
    """裁判 draft_sync → 落库草稿快照，管理端经 REST match_log 可读到。

    覆盖契约 §6.1（赛后可查）与 §6.3（裁判离线后展示最后一次上报快照）。
    """
    client, _, session, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json(
            {
                "type": "draft_sync",
                "state": {
                    "stage": "PICK",  # UI 态，应被 normalize 丢弃
                    "actions": [
                        {"by": "A", "code": "ML1", "kind": "ban"},
                        {"by": "B", "code": "ML2", "kind": "protect"},
                    ],
                    "picks": [{"by": "A", "code": "ML3"}],
                    "bannedTags": ["某CT词条"],
                    "tagBanBy": {"A": "某CT词条", "B": None},
                },
            }
        )
        ws_r.receive_json()  # draft_state 转发回显

    h = {"Authorization": f"Bearer {tokens['ref']}"}
    ml = client.get(f"/logs/matches/{session.id}/match_log", headers=h)
    assert ml.status_code == 200
    body = ml.json()
    assert body["draft_snapshot"] == {
        "actions": [
            {"by": "A", "code": "ML1", "kind": "ban"},
            {"by": "B", "code": "ML2", "kind": "protect"},
        ],
        "picks": [{"by": "A", "code": "ML3"}],
        "bannedTags": ["某CT词条"],
        "tagBanBy": {"A": "某CT词条", "B": None},
    }
