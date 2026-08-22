"""导播控制台 → 舞台定向广播测试（director_command / director_cmd）。

- 同账号第二条导播连接（OBS 舞台）收到原样转发的指令，发送方自身不回执；
- 非导播席位发送 director_command → 403；
- 改派导播后，旧导播的残留连接不收到新导播的指令（每导播只控自己的舞台）。
"""

from __future__ import annotations

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
