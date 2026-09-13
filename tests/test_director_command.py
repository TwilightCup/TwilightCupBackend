"""导播控制台 → 舞台定向广播测试（director_command / director_cmd）。

- 同账号第二条导播连接（OBS 舞台）收到原样转发的指令，发送方自身不回执；
- 非导播席位发送 director_command → 403；
- 改派导播后，旧导播的残留连接不收到新导播的指令（每导播只控自己的舞台）；
- state_sync 状态回放：DIRECTOR 连接 auth_ok 后补发最近场景/倒计时/配置。
"""

from __future__ import annotations

import time

from twilightcupbackend.auth import hash_password, issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.datatypes import Account, AccountType


def _drain(ws, n: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(n):
        ws.receive_json()


def _recv_until(ws, predicate, max_msgs: int = 30):  # type: ignore[no-untyped-def]
    for _ in range(max_msgs):
        m = ws.receive_json()
        if predicate(m):
            return m
    raise AssertionError("未在限定消息内匹配到目标")


def test_director_command_relayed_to_stage(world) -> None:  # type: ignore[no-untyped-def]
    """导播控制台发指令 → 同账号另一条导播连接（OBS 舞台）收到 director_cmd。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_console,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage,
    ):
        _drain(ws_pa, 6)
        _drain(ws_console, 5)
        _drain(ws_stage, 5)
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "switch_scene",
                "payload": {"scene": "soon"},
            }
        )
        msg = ws_stage.receive_json()
        assert msg == {
            "type": "director_cmd",
            "action": "switch_scene",
            "payload": {"scene": "soon"},
        }
        # 倒计时操控与 payload 原样转发
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "soon_set_target",
                "payload": {"target_ms": 300000},
            }
        )
        msg2 = ws_stage.receive_json()
        assert msg2 == {
            "type": "director_cmd",
            "action": "soon_set_target",
            "payload": {"target_ms": 300000},
        }
        # 发送方不回执：其队列下一条是随后的全员聊天广播，而非 director_cmd
        ws_pa.send_json({"type": "chat", "text": "after"})
        nxt = ws_console.receive_json()
        assert nxt["type"] == "chat"
        assert nxt["text"] == "after"


def test_director_command_only_director_seat(world) -> None:  # type: ignore[no-untyped-def]
    """非导播席位发 director_command → 403。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}") as ws_ref:
        _drain(ws_ref, 5)
        ws_ref.send_json({"type": "director_command", "action": "soon_start"})
        err = ws_ref.receive_json()
        assert err["type"] == "error"
        assert err["code"] == 403


def test_director_command_not_cross_account(world) -> None:  # type: ignore[no-untyped-def]
    """改派导播后，旧导播残留连接不收到新导播的舞台指令。"""
    client, db, session, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_old:
        _drain(ws_old, 5)
        # 改派导播为新账号；旧连接不主动断开，仍留在 store.directors
        new = Account(
            username="dri2",
            password_hash=hash_password("pw"),
            roles=[AccountType.DIRECTOR],
            display_name="新导播",
        )
        db.accounts.insert(new)
        db.matches.update_fields(session.id, {"director_id": new.id})
        tok_new = issue_token(new, settings)
        with (
            client.websocket_connect(f"/ws/{tok_new}") as ws_new,
            client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
        ):
            _drain(ws_new, 5)
            _drain(ws_pa, 6)
            ws_new.send_json({"type": "director_command", "action": "soon_start"})
            # 用一条全员聊天作序标：旧导播在收到它之前不得出现 director_cmd
            ws_pa.send_json({"type": "chat", "text": "marker"})
            for _ in range(10):
                m = ws_old.receive_json()
                assert m["type"] != "director_cmd"
                if m.get("type") == "chat" and m.get("text") == "marker":
                    break
            else:
                raise AssertionError("旧导播未收到序标聊天")


def test_config_update_relayed_only_to_stage(world) -> None:  # type: ignore[no-untyped-def]
    """config_update：直播配置实时下发 → 舞台原样收；选手/裁判/发送方收不到。

    payload 结构与其余 action 口径一致：服务端不校验 config 形状，原样透传。
    """
    client, _, _, tokens = world
    config = {
        "rtmpA": "rtmp://a/live",
        "rtmpB": "rtmp://b/live",
        "hlsA": "http://a/hls.m3u8",
        "hlsB": "http://b/hls.m3u8",
        "pbA": "pb-a",
        "pbB": "pb-b",
        "histA": "3胜2负",
        "histB": "2胜3负",
    }
    with (
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_ref,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_console,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage,
    ):
        _drain(ws_pa, 6)
        _drain(ws_ref, 5)
        _drain(ws_console, 5)
        _drain(ws_stage, 5)
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "config_update",
                "payload": {"config": config},
            }
        )
        assert ws_stage.receive_json() == {
            "type": "director_cmd",
            "action": "config_update",
            "payload": {"config": config},
        }
        # 非法 config（非对象）与其余 action 口径一致：不校验、原样透传
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "config_update",
                "payload": {"config": "oops"},
            }
        )
        assert ws_stage.receive_json() == {
            "type": "director_cmd",
            "action": "config_update",
            "payload": {"config": "oops"},
        }
        # 选手/裁判/发送方均收不到：以全员聊天作序标，此前不得出现 director_cmd
        ws_pa.send_json({"type": "chat", "text": "marker"})
        for ws in (ws_pa, ws_ref, ws_console):
            for _ in range(10):
                m = ws.receive_json()
                assert m["type"] != "director_cmd"
                if m.get("type") == "chat" and m.get("text") == "marker":
                    break
            else:
                raise AssertionError("未收到序标聊天")


def _state_sync(ws) -> dict:  # type: ignore[no-untyped-def]
    """读该新连接的 state_sync（此前不应有其他 director_cmd）。"""
    m = _recv_until(ws, lambda x: x.get("type") == "director_cmd")
    assert m["action"] == "state_sync", m
    return m["payload"]


def test_state_sync_replay_on_connect(world) -> None:  # type: ignore[no-untyped-def]
    """控制台发过场景/倒计时/配置后，新开舞台连接 auth_ok 后收一条 state_sync 对齐。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_console,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
    ):
        _drain(ws_console, 5)
        _drain(ws_pa, 5)
        for body in (
            {"action": "switch_scene", "payload": {"scene": "soon"}},
            {"action": "soon_set_target", "payload": {"target_ms": 300000}},
            {"action": "soon_start"},
            {
                "action": "config_update",
                "payload": {"config": {"rtmpA": "rtmp://a/live", "histA": "3胜2负"}},
            },
        ):
            ws_console.send_json({"type": "director_command", **body})
        with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage:
            p = _state_sync(ws_stage)
            assert p["scene"] == "soon"
            assert p["soon"]["target_ms"] == 300000
            assert p["soon"]["started_at"] is not None
            assert p["soon"]["paused_at"] is None
            assert p["soon"]["now_ms"] >= p["soon"]["started_at"]
            assert p["config"] == {"rtmpA": "rtmp://a/live", "histA": "3胜2负"}
        # 选手收不到 state_sync：以序标聊天界定
        ws_pa.send_json({"type": "chat", "text": "marker"})
        for _ in range(10):
            m = ws_pa.receive_json()
            assert m["type"] != "director_cmd"
            if m.get("type") == "chat" and m.get("text") == "marker":
                break
        else:
            raise AssertionError("选手未收到序标聊天")


def test_state_sync_soon_timeline(world) -> None:  # type: ignore[no-untyped-def]
    """倒计时时间线：start→pause 各记点；恢复做暂停补偿；reset 清时间线留 target。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_console:
        _drain(ws_console, 5)
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "soon_set_target",
                "payload": {"target_ms": 300000},
            }
        )
        ws_console.send_json({"type": "director_command", "action": "soon_start"})
        time.sleep(0.08)
        ws_console.send_json({"type": "director_command", "action": "soon_pause"})
        with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage:
            soon = _state_sync(ws_stage)["soon"]
            assert soon["started_at"] is not None and soon["paused_at"] is not None
            ran = soon["paused_at"] - soon["started_at"]  # 暂停前有效进行时长
            assert 60 <= ran <= 5000  # ≈ sleep 时长（补偿基准）
            # 恢复：舞台收到广播（consume），再开新连接验证暂停补偿
            ws_console.send_json({"type": "director_command", "action": "soon_start"})
            _recv_until(ws_stage, lambda m: m.get("type") == "director_cmd")
            with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage2:
                s2 = _state_sync(ws_stage2)["soon"]
                assert s2["started_at"] is not None
                assert s2["paused_at"] is None
                # 有效时长不变：now - started ≈ ran（暂停时长被折算掉）
                assert abs((s2["now_ms"] - s2["started_at"]) - ran) < 2000
            # reset：清 started/paused，保留 target_ms
            ws_console.send_json({"type": "director_command", "action": "soon_reset"})
            _recv_until(ws_stage, lambda m: m.get("type") == "director_cmd")
            with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage3:
                s3 = _state_sync(ws_stage3)["soon"]
                assert s3["started_at"] is None
                assert s3["paused_at"] is None
                assert s3["target_ms"] == 300000


def test_state_sync_absent_without_history(world) -> None:  # type: ignore[no-untyped-def]
    """无任何指令历史 → 新 DIRECTOR/选手/裁判连接均收不到 state_sync。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_dri,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
        client.websocket_connect(f"/ws/{tokens['ref']}") as ws_ref,
    ):
        _drain(ws_dri, 5)
        _drain(ws_pa, 5)
        _drain(ws_ref, 5)
        ws_pa.send_json({"type": "chat", "text": "marker"})
        for ws in (ws_dri, ws_pa, ws_ref):
            for _ in range(10):
                m = ws.receive_json()
                assert m["type"] != "director_cmd"
                if m.get("type") == "chat" and m.get("text") == "marker":
                    break
            else:
                raise AssertionError("未收到序标聊天")


def test_director_state_keyed_by_account_and_match(world) -> None:  # type: ignore[no-untyped-def]
    """状态暂存按 (account_id, match_id) 隔离，不同导播/比赛互不串。"""
    client, _, _, _ = world
    cm = client.app.state.connection_manager
    cm._update_director_state("acc1", "m1", "switch_scene", {"scene": "a"})
    cm._update_director_state("acc2", "m1", "switch_scene", {"scene": "b"})
    cm._update_director_state("acc1", "m2", "switch_scene", {"scene": "c"})
    cases = (("acc1", "m1", "a"), ("acc2", "m1", "b"), ("acc1", "m2", "c"))
    for acc, mid, scene in cases:
        p = cm._director_state_payload(acc, mid)
        assert p is not None and p["scene"] == scene
    assert cm._director_state_payload("accX", "m1") is None


def test_frame_align_relayed_and_replayed_on_connect(world) -> None:  # type: ignore[no-untyped-def]
    """ "frame_align 扇出给舞台 + 新连接 state_sync 补发权威 T 与就绪状态。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_console,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
    ):
        _drain(ws_console, 5)
        _drain(ws_stage, 5)
        _drain(ws_pa, 5)
        # 控制台刷新 frame_align → 舞台扇出原样、发送方不回执
        # （pa 连入在 stage 队列里前置了一条 seat_state 在线广播，需跨过）
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {
                    "t_us": 1710000000000000,
                    "ready_a": True,
                    "ready_b": False,
                    "src": "docA",
                },
            }
        )
        r = _recv_until(ws_stage, lambda m: m.get("action") == "frame_align")
        assert r == {
            "type": "director_cmd",
            "action": "frame_align",
            "payload": {
                "t_us": 1710000000000000,
                "ready_a": True,
                "ready_b": False,
                "src": "docA",
            },
        }
        # 新开舞台连接 auth_ok 后，state_sync 补发含 frame_align（独立键）
        # 与 align_authority_src（唯一权威 src）
        with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage2:
            p = _state_sync(ws_stage2)
            assert p["frame_align"] == {
                "t_us": 1710000000000000,
                "ready_a": True,
                "ready_b": False,
            }
            assert p["align_authority_src"] == "docA"
        # 发送方不回执：以全员聊天作序标，此前不得出现 director_cmd
        ws_pa.send_json({"type": "chat", "text": "after"})
        for _ in range(10):
            nxt = ws_console.receive_json()
            assert nxt["type"] != "director_cmd"
            if nxt.get("type") == "chat" and nxt.get("text") == "after":
                break
        else:
            raise AssertionError("控制台未收到序标聊天")


def test_frame_align_last_write_wins(world) -> None:  # type: ignore[no-untyped-def]
    """frame_align 覆盖暂存最近一条：t_us/ready 全 0/false 时仍补发（前端兜底）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_console,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
    ):
        _drain(ws_console, 5)
        _drain(ws_pa, 5)
        # 同一权威 src=docA：每条都被采纳（authority 匹配），最近一条胜出
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {
                    "t_us": 123,
                    "ready_a": True,
                    "ready_b": True,
                    "src": "docA",
                },
            }
        )
        # 舞台未连（无双人），下一条 frame_align 覆盖暂存
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {
                    "t_us": 0,
                    "ready_a": False,
                    "ready_b": False,
                    "src": "docA",
                },
            }
        )
        # 缺省 ready 也按 False：第三条只带 t_us
        ws_console.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {"t_us": 456, "src": "docA"},
            }
        )
        with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage:
            p = _state_sync(ws_stage)
            assert p["frame_align"] == {"t_us": 456, "ready_a": False, "ready_b": False}
            assert p["align_authority_src"] == "docA"


def test_frame_align_absent_without_history(world) -> None:  # type: ignore[no-untyped-def]
    """该场从未收到 frame_align → 即使是 state_sync 也不含 frame_align 键。"""
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_dri:
        _drain(ws_dri, 5)
        ws_dri.send_json(
            {
                "type": "director_command",
                "action": "config_update",
                "payload": {"config": {"rtmpA": "rtmp://a/live"}},
            }
        )
        ws_dri.send_json(
            {
                "type": "director_command",
                "action": "soon_set_target",
                "payload": {"target_ms": 300000},
            }
        )
        with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage:
            p = _state_sync(ws_stage)
            assert "frame_align" not in p
            assert "align_authority_src" not in p
            assert p["config"] == {"rtmpA": "rtmp://a/live"}


def test_frame_align_authority_single_adoption(world) -> None:  # type: ignore[no-untyped-def]
    """唯一权威：首个 src 接任，其后同 src 持续采纳更新；异 src 在任期内被忽略。"""
    client, _, _, _ = world
    cm = client.app.state.connection_manager
    key = ("acc", "m")
    # 首条 → 接任并采纳
    relayed, changed = cm._update_director_state(
        "acc",
        "m",
        "frame_align",
        {"t_us": 100, "ready_a": True, "ready_b": True, "src": "docA"},
    )
    assert (relayed, changed) == (True, True)
    st = cm._director_state[key]
    assert st.align_authority_src == "docA"
    assert st.frame_align_t_us == 100
    # 同 src 继续 → 采纳更新（authority 匹配，无变更）
    relayed, changed = cm._update_director_state(
        "acc",
        "m",
        "frame_align",
        {"t_us": 200, "ready_a": False, "ready_b": False, "src": "docA"},
    )
    assert (relayed, changed) == (True, False)
    assert st.frame_align_t_us == 200
    # 异 src 在任期内 → 忽略：不采纳、不扇出、状态不变
    relayed, changed = cm._update_director_state(
        "acc",
        "m",
        "frame_align",
        {"t_us": 999, "ready_a": True, "ready_b": True, "src": "docB"},
    )
    assert (relayed, changed) == (False, False)
    assert st.align_authority_src == "docA"
    assert st.frame_align_t_us == 200  # 仍为权威 docA 的 200，未被 999 覆盖


def test_frame_align_authority_takeover_after_timeout(world) -> None:  # type: ignore[no-untyped-def]
    """权威静默超时（>5s，断线/倒台）→ 新 src 接任并被采纳。"""
    client, _, _, _ = world
    cm = client.app.state.connection_manager
    key = ("acc", "m")
    cm._update_director_state("acc", "m", "frame_align", {"t_us": 100, "src": "docA"})
    st = cm._director_state[key]
    # 伪造权威最后心跳为 6s 前 → 超时（权威已断线）
    st.align_authority_last_ms = int(time.time() * 1000) - 6000
    relayed, changed = cm._update_director_state(
        "acc", "m", "frame_align", {"t_us": 777, "src": "docB"}
    )
    assert (relayed, changed) == (True, True)
    assert st.align_authority_src == "docB"
    assert st.frame_align_t_us == 777  # 新权威 T 已采纳
    # 旧权威 docA 再发言也已超期前的权威被 B 取代 → 此时其为非权威，被忽略
    relayed, _ = cm._update_director_state(
        "acc", "m", "frame_align", {"t_us": 888, "src": "docA"}
    )
    assert relayed is False
    assert st.frame_align_t_us == 777


def test_frame_align_without_src_ignored(world) -> None:  # type: ignore[no-untyped-def]
    """frame_align 缺 src → 无权威归属，一律忽略（不存储、不扇出）。"""
    client, _, _, _ = world
    cm = client.app.state.connection_manager
    relayed, changed = cm._update_director_state(
        "acc", "m", "frame_align", {"t_us": 1, "ready_a": True, "ready_b": True}
    )
    st = cm._director_state[("acc", "m")]
    assert (relayed, changed) == (False, False)
    assert st.align_authority_src is None
    assert st.frame_align_t_us is None


def test_frame_align_non_authority_not_relayed(world) -> None:  # type: ignore[no-untyped-def]
    """多舞台并存：非权威的 frame_align 不扇出给其他连接（观众不见第二套 T）。"""
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_console,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage_b,
        client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage_a,
        client.websocket_connect(f"/ws/{tokens['pa']}") as ws_pa,
    ):
        _drain(ws_console, 5)
        _drain(ws_stage_b, 5)
        _drain(ws_stage_a, 5)
        _drain(ws_pa, 5)
        # A 先发言 → 成为权威，扇出给 console
        ws_stage_a.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {
                    "t_us": 1000,
                    "ready_a": True,
                    "ready_b": False,
                    "src": "docA",
                },
            }
        )
        r = _recv_until(ws_console, lambda m: m.get("action") == "frame_align")
        assert r["payload"]["src"] == "docA" and r["payload"]["t_us"] == 1000
        # B 发言（生产期内，非权威）→ 不扇出；A 仍权威继续采纳
        ws_stage_b.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {
                    "t_us": 99,
                    "ready_a": True,
                    "ready_b": True,
                    "src": "docB",
                },
            }
        )
        ws_stage_a.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {
                    "t_us": 2000,
                    "ready_a": True,
                    "ready_b": False,
                    "src": "docA",
                },
            }
        )
        # 以全员聊天作序标：console 在收到它前不得出现 src=docB 的 frame_align
        ws_pa.send_json({"type": "chat", "text": "marker"})
        for _ in range(20):
            m = ws_console.receive_json()
            assert not (
                m.get("type") == "director_cmd"
                and m.get("action") == "frame_align"
                and m.get("payload", {}).get("src") == "docB"
            )
            if m.get("type") == "chat" and m.get("text") == "marker":
                break
        else:
            raise AssertionError("控制台未收到序标聊天")
        # 新连接视角：权威仍为 docA、T=2000（B 的 99 从未被采纳/存储）
        with client.websocket_connect(f"/ws/{tokens['dri']}") as ws_stage_c:
            p = _state_sync(ws_stage_c)
            assert p["align_authority_src"] == "docA"
            assert p["frame_align"]["t_us"] == 2000
