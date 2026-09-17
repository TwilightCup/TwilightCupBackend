"""Oldest connected page owns T; takeover is ordered, never a publishing race."""

from tests.test_director_command import _recv_until


def auth(ws):
    return _recv_until(ws, lambda m: m.get("type") == "auth_ok")


def frame(ws, t, **extra):
    ws.send_json(
        {
            "type": "director_command",
            "action": "frame_align",
            "payload": {"src": "untrusted-page-src", "t_us": t, **extra},
        }
    )


def event(ws, action):
    return _recv_until(ws, lambda m: m.get("action") == action)["payload"]


def test_connection_order_elects_before_any_page_publishes(world):
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['dri']}") as first,
        client.websocket_connect(f"/ws/{tokens['dri']}") as second,
        client.websocket_connect(f"/ws/{tokens['dri']}") as third,
    ):
        a, b, c = [auth(ws) for ws in (first, second, third)]
        assert a["align_role"] == "publisher"
        assert b["align_role"] == c["align_role"] == "follower"
        assert {r["align_authority_src"] for r in (a, b, c)} == {a["connection_id"]}
        frame(third, 99999999)
        frame(first, 10000000)
        x, y = [event(ws, "frame_align") for ws in (second, third)]
        assert x == y and x["t_us"] == 10000000
        assert x["src"] == a["connection_id"]
        assert x["epoch"] == a["authority_epoch"]


def test_disconnect_promotes_oldest_remaining_and_reconnect_joins_tail(world):
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as first:
        a = auth(first)
        with (
            client.websocket_connect(f"/ws/{tokens['dri']}") as second,
            client.websocket_connect(f"/ws/{tokens['dri']}") as third,
        ):
            b, c = auth(second), auth(third)
            frame(first, 10000000)
            event(second, "frame_align")
            event(third, "frame_align")
            first.close()
            promotion = event(second, "align_authority")
            other = event(third, "align_authority")
            assert promotion["src"] == other["src"] == b["connection_id"]
            assert promotion["role"] == "publisher" and other["role"] == "follower"
            assert promotion["epoch"] > a["authority_epoch"]
            frozen = event(third, "frame_align")
            assert frozen["frozen"] and frozen["t_us"] == 10000000
            with client.websocket_connect(f"/ws/{tokens['dri']}") as reconnected:
                fresh = auth(reconnected)
                assert fresh["align_role"] == "follower"
                assert fresh["align_authority_src"] == b["connection_id"]
                # Old authority T / epoch cannot re-enter, even with a forged src.
                frame(third, 90000000, epoch=a["authority_epoch"])
                frame(second, 11000000, epoch=promotion["epoch"])
                accepted = event(third, "frame_align")
                assert accepted["t_us"] == 11000000
                second.close()
                assert event(third, "align_authority")["src"] == c["connection_id"]


def test_hls_config_never_starts_media_tasks(world):
    client, _, match, tokens = world
    cm = client.app.state.connection_manager
    with client.websocket_connect(f"/ws/{tokens['dri']}") as ws:
        a = auth(ws)
        ws.send_json(
            {
                "type": "director_command",
                "action": "config_update",
                "payload": {
                    "config": {
                        "hlsA": "https://example.com/a.m3u8",
                        "hlsB": "https://example.com/b.m3u8",
                    }
                },
            }
        )
        frame(ws, 12300000)
        with client.websocket_connect(f"/ws/{tokens['dri']}") as late:
            auth(late)
            p = event(late, "state_sync")
            assert p["frame_align"]["t_us"] == 12300000
            assert p["align_authority_src"] == a["connection_id"]
        assert not hasattr(cm, "_media_authorities")
        assert cm._director_state[(match.director_id, match.id)].align_owner is not None


def test_disconnect_before_first_t_still_promotes_follower(world):
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as first:
        auth(first)
        with client.websocket_connect(f"/ws/{tokens['dri']}") as second:
            b = auth(second)
            first.close()
            promotion = event(second, "align_authority")
            assert promotion["role"] == "publisher"
            assert promotion["src"] == b["connection_id"]
            frame(second, 12300000, epoch=promotion["epoch"])
            with client.websocket_connect(f"/ws/{tokens['dri']}") as late:
                auth(late)
                assert event(late, "state_sync")["frame_align"]["t_us"] == 12300000
