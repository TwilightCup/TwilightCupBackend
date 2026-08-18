"""合集提前下发（pick_announced）与回合开始预载门控验收测试。

fixture 的 start_countdown_delay=2；图池含 ML1（多关/ML，L1+L2）与
CT01（单关/CT，需 retry）。选手带预载能力用 ``?cap=preload1`` 连接。
"""

from __future__ import annotations

from dataclasses import replace

PHASE_PREP = 1
PHASE_COUNTDOWN = 2
PICK_MULTI = 1

CAP = "preload1"


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


def _select(ws_r, pick: str, retry: int | None = None) -> None:  # type: ignore[no-untyped-def]
    msg: dict = {"type": "referee_select_pick", "pick_code": pick}
    if retry is not None:
        msg["retry_count"] = retry
    ws_r.send_json(msg)


def _report(ws, status: str, detail: str | None = None) -> None:  # type: ignore[no-untyped-def]
    msg: dict = {"type": "preload_report", "status": status}
    if detail is not None:
        msg["detail"] = detail
    ws.send_json(msg)


def _ready(ws) -> None:  # type: ignore[no-untyped-def]
    ws.send_json({"type": "chat", "text": "!ready"})


def _phase_is(phase: int):  # type: ignore[no-untyped-def]
    return lambda m: m["type"] == "phase_change" and m["phase"] == phase


def _preload_is(field: str, status: str):  # type: ignore[no-untyped-def]
    return lambda m: m["type"] == "preload_state" and m[field] == status


def _no_countdown_since_ping(ws_r, ws_a) -> None:  # type: ignore[no-untyped-def]
    """裁判发一条聊天作哨兵：到哨兵回声为止未出现 COUNTDOWN 阶段切换。"""
    ws_r.send_json({"type": "chat", "text": "ping"})
    got = _collect_until(ws_a, lambda m: m["type"] == "chat" and m["text"] == "ping")
    assert not any(
        m["type"] == "phase_change" and m["phase"] == PHASE_COUNTDOWN for m in got
    ), f"不应进入倒计时：{[m['type'] for m in got]}"


# ---------------------------------------------------------------------------
# 场景 1/2：pick_announced 提前下发与重选重置
# ---------------------------------------------------------------------------


def test_pick_announced_to_all_seats(world) -> None:  # type: ignore[no-untyped-def]
    """场景 1：MULTI 选图 → 全体收到 pick_announced（展开后 collection）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}?cap={CAP}") as ws_b,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d,
    ):
        for ws in (ws_r, ws_a, ws_b, ws_d):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)  # phase_change + system + ready_state
        _select(ws_r, "ML1")
        # kind="pick" 系统聊天照旧
        sys_msg = _recv_until(
            ws_r, lambda m: m["type"] == "system" and m["kind"] == "pick"
        )
        assert "ML1" in sys_msg["text"]
        for ws, tag in ((ws_r, "ref"), (ws_a, "A"), (ws_b, "B"), (ws_d, "D")):
            m = _recv_until(ws, lambda m: m["type"] == "pick_announced")
            assert m["pick_code"] == "ML1"
            assert m["pick"]["code"] == "ML1"
            assert m["pick"]["type"] == PICK_MULTI
            assert m["pick"]["single_scoring"] == "fastest"
            # collection 与 round_start 同构：关卡 id 已展开为显示名
            assert m["collection"]["raw"]["levels"] == ["L1", "L2"], tag


def test_reselect_reannounces_and_resets(world) -> None:  # type: ignore[no-untyped-def]
    """场景 2：重新应用选图 → 重发 pick_announced；预载状态重置 absent 并广播。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        _recv_until(ws_a, lambda m: m["type"] == "pick_announced")
        _report(ws_a, "done")
        st = _recv_until(ws_a, lambda m: m["type"] == "preload_state")
        assert st["a_status"] == "done"
        # 重新应用同一选图（广播顺序：preload_state 重置 → system → pick_announced）
        _select(ws_r, "ML1")
        st2 = _recv_until(ws_a, lambda m: m["type"] == "preload_state")
        assert st2["a_status"] == "absent"
        assert st2["b_status"] == "absent"
        _recv_until(ws_a, lambda m: m["type"] == "pick_announced")


# ---------------------------------------------------------------------------
# 场景 3/4/6/9：门控等待、放行与豁免
# ---------------------------------------------------------------------------


def test_gate_waits_for_last_done(world) -> None:  # type: ignore[no-untyped-def]
    """场景 3：双方 ready、预载未完不进倒计时；最后一份 done 立即自动倒计时。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}?cap={CAP}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        for ws in (ws_a, ws_b):
            _report(ws, "in_progress")
            _ready(ws)
        # 双方 ready + in_progress：不进倒计时
        _recv_until(ws_a, _preload_is("b_status", "in_progress"))
        _no_countdown_since_ping(ws_r, ws_a)
        # A done（B 仍 in_progress）：仍不进倒计时
        _report(ws_a, "done")
        _recv_until(ws_a, _preload_is("a_status", "done"))
        _no_countdown_since_ping(ws_r, ws_a)
        # 最后一份 done：立即自动倒计时
        _report(ws_b, "done")
        ph = _recv_until(ws_a, _phase_is(PHASE_COUNTDOWN))
        assert ph is not None


def test_failed_warns_and_does_not_block(world) -> None:  # type: ignore[no-untyped-def]
    """场景 4：一方 failed → kind=preload 告警；不阻塞（对手 done 后自动开始）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}?cap={CAP}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        _report(ws_a, "failed", detail="workshop not subscribed")
        warn = _recv_until(
            ws_a, lambda m: m["type"] == "system" and m["kind"] == "preload"
        )
        assert "workshop not subscribed" in warn["text"]
        # A failed 不阻塞；B in_progress → 等待；B done → 自动开始
        _ready(ws_a)
        _report(ws_b, "in_progress")
        _ready(ws_b)
        _no_countdown_since_ping(ws_r, ws_a)
        _report(ws_b, "done")
        _recv_until(ws_a, _phase_is(PHASE_COUNTDOWN))


def test_single_na_passes_gate(world) -> None:  # type: ignore[no-untyped-def]
    """场景 6：SINGLE 合集双方 na → 双 ready 即时自动开始（同现状节奏）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}?cap={CAP}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "CT01", retry=1)
        for ws in (ws_a, ws_b):
            _report(ws, "na")
        _ready(ws_a)
        _ready(ws_b)
        _recv_until(ws_a, _phase_is(PHASE_COUNTDOWN))


def test_legacy_client_without_cap_exempt(world) -> None:  # type: ignore[no-untyped-def]
    """场景 9：旧客户端（无 cap、不上报）→ 双 ready 即时自动开始，不拖超时。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        _ready(ws_a)
        _ready(ws_b)
        _recv_until(ws_a, _phase_is(PHASE_COUNTDOWN))


# ---------------------------------------------------------------------------
# 场景 5/13：超时强制开始与手动跳过
# ---------------------------------------------------------------------------


def test_gate_timeout_forces_start(world, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """场景 5：双方 ready、预载卡死 → 超时（1s）强制开始 + 超时系统消息。"""
    client, _, _, tokens = world
    cm = client.app.state.connection_manager
    monkeypatch.setattr(
        cm, "settings", replace(cm.settings, preload_gate_timeout=1)
    )
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}?cap={CAP}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        for ws in (ws_a, ws_b):
            _report(ws, "in_progress")
            _ready(ws)
        _recv_until(ws_a, _preload_is("b_status", "in_progress"))
        # 超时兜底：预载等待超时消息 + 强制自动倒计时
        timeout_msg = _recv_until(
            ws_a, lambda m: m["type"] == "system" and m["kind"] == "preload"
        )
        assert "超时" in timeout_msg["text"] or "timed out" in timeout_msg["text"]
        _recv_until(ws_a, _phase_is(PHASE_COUNTDOWN))


def test_manual_start_skips_wait(world) -> None:  # type: ignore[no-untyped-def]
    """场景 13：门控未过时手动开始 → 立即倒计时 + “跳过预载等待”提示。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}?cap={CAP}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        _report(ws_a, "in_progress")
        _report(ws_b, "done")
        for ws in (ws_a, ws_b):
            _ready(ws)
        ws_r.send_json({"type": "referee_manual_start"})
        skip_msg = _recv_until(
            ws_a, lambda m: m["type"] == "system" and m["kind"] == "preload"
        )
        assert "跳过" in skip_msg["text"] or "skipped" in skip_msg["text"]
        _recv_until(ws_a, _phase_is(PHASE_COUNTDOWN))


# ---------------------------------------------------------------------------
# 场景 7/8：un-ready 重置与断线重连补发
# ---------------------------------------------------------------------------


def test_unready_aborts_and_resets_preload(world) -> None:  # type: ignore[no-untyped-def]
    """场景 7：un-ready 中止倒计时 → 预载重置；重新 ready 重走门控。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
        client.websocket_connect(f"/ws/{tokens['pb']}?cap={CAP}") as ws_b,
    ):
        for ws in (ws_r, ws_a, ws_b):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        for ws in (ws_a, ws_b):
            _report(ws, "done")
            _ready(ws)
        _recv_until(ws_a, _phase_is(PHASE_COUNTDOWN))
        # A 取消准备 → 倒计时中止回 PREP，预载状态重置
        _ready(ws_a)
        _recv_until(
            ws_a, lambda m: m["type"] == "phase_change" and m["phase"] == PHASE_PREP
        )
        st = _recv_until(ws_a, lambda m: m["type"] == "preload_state")
        assert st["a_status"] == "absent"
        assert st["b_status"] == "absent"
        # 重新 ready：预载已重置（absent+有能力）→ 不再立即倒计时
        _ready(ws_a)
        _no_countdown_since_ping(ws_r, ws_a)
        # 重新上报后放行
        for ws in (ws_a, ws_b):
            _report(ws, "done")
        _recv_until(ws_a, _phase_is(PHASE_COUNTDOWN))


def test_reconnect_replays_pick_announced(world) -> None:  # type: ignore[no-untyped-def]
    """场景 8：PREP 断线重连 → 握手补发 pick_announced + preload_state 快照。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r:
        _drain(ws_r, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        with client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a:
            # 握手 7 条：auth/ready/phase/pick_announced/preload_state/seat×2
            _drain(ws_a, 7)
            _report(ws_a, "in_progress")
            _recv_until(ws_a, _preload_is("a_status", "in_progress"))
        # with 退出 = 断线；重连后 phase_change 之外应补发 pick_announced 与快照
        with client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a2:
            seen_phase = False

            def _after_phase(m: dict) -> bool:
                nonlocal seen_phase
                if m["type"] == "phase_change" and m["phase"] == PHASE_PREP:
                    seen_phase = True
                return seen_phase and m["type"] == "pick_announced"

            nxt = _recv_until(ws_a2, _after_phase)
            assert nxt["pick_code"] == "ML1"
            assert nxt["collection"]["raw"]["levels"] == ["L1", "L2"]

            def _preload_after(m: dict) -> bool:
                return seen_phase and m["type"] == "preload_state"

            nxt2 = _recv_until(ws_a2, _preload_after)
            assert nxt2["a_status"] == "in_progress"
            assert nxt2["b_status"] == "absent"


# ---------------------------------------------------------------------------
# 场景 10/11 + R3.1：守卫与限制
# ---------------------------------------------------------------------------


def test_select_pick_rejected_in_countdown(world) -> None:  # type: ignore[no-untyped-def]
    """场景 10：COUNTDOWN 中改图 → 拒绝并回 error（预载门控 R3.2）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        _drain(ws_r, 3)  # preload_state + system + pick_announced
        ws_r.send_json({"type": "referee_manual_start"})
        _recv_until(ws_r, _phase_is(PHASE_COUNTDOWN))
        _select(ws_r, "CT01", retry=1)
        err = _recv_until(ws_r, lambda m: m["type"] == "error" and m["code"] == 400)
        assert "phase" in err["msg"].lower()


def test_preload_report_non_player_rejected(world) -> None:  # type: ignore[no-untyped-def]
    """场景 11：非选手席位发 preload_report → 回 error，状态不变。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_d,
        client.websocket_connect(f"/ws/{tokens['pa']}?cap={CAP}") as ws_a,
    ):
        for ws in (ws_r, ws_d, ws_a):
            _drain(ws, 5)
        ws_r.send_json({"type": "referee_mark_prep"})
        _drain(ws_r, 3)
        _select(ws_r, "ML1")
        # 裁判上报 → 403；导播（只读）→ 403
        _report(ws_r, "done")
        err = _recv_until(ws_r, lambda m: m["type"] == "error" and m["code"] == 403)
        assert err is not None
        _report(ws_d, "done")
        err_d = _recv_until(ws_d, lambda m: m["type"] == "error" and m["code"] == 403)
        assert err_d is not None
        # 状态未被污染：选手 A 上报后 b 仍 absent
        _report(ws_a, "done")
        st = _recv_until(ws_a, _preload_is("a_status", "done"))
        assert st["b_status"] == "absent"


def test_referee_actions_guarded_for_players(world) -> None:  # type: ignore[no-untyped-def]
    """R3.1：选手席位伪造 mark_prep / select_pick / manual_start → 403。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_r,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_a,
    ):
        for ws in (ws_r, ws_a):
            _drain(ws, 5)
        ws_a.send_json({"type": "referee_mark_prep"})
        err1 = _recv_until(ws_a, lambda m: m["type"] == "error" and m["code"] == 403)
        assert err1 is not None
        ws_a.send_json(
            {"type": "referee_select_pick", "pick_code": "ML1"}
        )
        err2 = _recv_until(ws_a, lambda m: m["type"] == "error" and m["code"] == 403)
        assert err2 is not None
        ws_a.send_json({"type": "referee_manual_start"})
        err3 = _recv_until(ws_a, lambda m: m["type"] == "error" and m["code"] == 403)
        assert err3 is not None
        # 状态未被污染：阶段仍是 IDLE，后续裁判正常 mark_prep
        ws_r.send_json({"type": "referee_mark_prep"})
        ph = _recv_until(ws_r, _phase_is(PHASE_PREP))
        assert ph is not None
