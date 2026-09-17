"""Server-owned frame clock: scope, fencing, replay and lifecycle contracts."""

import json
from typing import cast
from unittest.mock import AsyncMock

import pytest

from twilightcupbackend import connection_manager as module
from twilightcupbackend.datatypes import Seat
from twilightcupbackend.stores import Connection


def publish(cm, payload, account="acc", match="match", conn=None):
    return cm._update_director_state(account, match, "frame_align", payload, conn)


def anchor(cm, account="acc", match="match"):
    return cm._director_state_payload(account, match)["frame_align"]


def test_complete_anchor_and_fencing(world, monkeypatch):
    cm = world[0].app.state.connection_manager
    monkeypatch.setattr(module, "_now_ms", lambda: 1760000000000)
    p = {
        "src": "a",
        "t_us": 1234567890000,
        "seq": 12,
        "server_time_ms": 1,
        "server_now_ms": 2,
        "effective_at_ms": 3,
        "custom": "preserved",
    }
    assert publish(cm, p) == (True, True)
    a = anchor(cm)
    assert (
        a.items()
        >= {
            "authority_epoch": 1,
            "epoch": 1,
            "seq": 1,
            "t_us": 1234567890000,
            "rate": 1.0,
            "paused": False,
            "frozen": False,
            "effective_at_ms": 1760000000000,
            "server_time_ms": 1760000000000,
            "server_now_ms": 1760000000000,
            "match_id": "match",
            "account_id": "acc",
            "scene": "",
            "source_id": "a",
            "src": "a",
            "custom": "preserved",
        }.items()
    )
    for extra in (
        {"seq": 12},
        {"seq": 11},
        {"epoch": 0},
        {"authority_epoch": 0},
        {"src": "b"},
        {"t_us": 1},
    ):
        assert publish(cm, {"src": "a", "t_us": a["t_us"], **extra})[0] is False
    assert anchor(cm) == a
    assert publish(cm, {"src": "a", "t_us": a["t_us"], "seq": 13, "epoch": 1})[0]
    assert anchor(cm)["seq"] == 2


@pytest.mark.parametrize(
    "extra",
    [
        {"t_us": True},
        {"t_us": -1},
        {"t_us": 1.2},
        {"t_us": "12"},
        {"t_us": 2**53},
        {"rate": 2},
        {"rate": -1},
        {"rate": float("nan")},
        {"rate": 10**1000},
        {"paused": "false"},
        {"frozen": 1},
        {"match_id": "other"},
        {"account_id": "other"},
        {"src": ""},
        {"seq": True},
        {"epoch": 1, "authority_epoch": 0},
    ],
)
def test_invalid_anchor_cannot_elect(world, extra):
    cm = world[0].app.state.connection_manager
    assert publish(cm, {"src": "a", "t_us": 100, **extra}) == (False, False)
    assert "frame_align" not in cm._director_state_payload("acc", "match")


def test_scope_and_takeover(world, monkeypatch):
    cm = world[0].app.state.connection_manager
    now = 1760000000000
    monkeypatch.setattr(module, "_now_ms", lambda: now)
    for account, match in [("acc", "match"), ("other", "match"), ("acc", "other")]:
        assert publish(cm, {"src": "a", "t_us": 100}, account, match)[0]
    now += 5001
    assert publish(cm, {"src": "b", "t_us": 200}) == (True, True)
    assert anchor(cm)["epoch"] == 2
    assert anchor(cm)["seq"] == 1
    now += 6000
    assert not publish(cm, {"src": "a", "t_us": 300})[0]
    assert anchor(cm, "other")["epoch"] == 1
    assert anchor(cm, match="other")["epoch"] == 1


async def test_three_connections_identity_and_disconnect(world):
    client, _, match, _ = world
    cm = client.app.state.connection_manager
    store = cm.registry.get_or_create(match)
    conns = [
        Connection(AsyncMock(), "acc", "director", Seat.DIRECTOR, match.id)
        for _ in range(3)
    ]
    store.directors.update(conns)
    source, *followers = conns
    from twilightcupbackend.protocol import ClientDirectorCommand

    async def send(conn, **extra):
        await cm._dispatch(
            conn,
            ClientDirectorCommand(
                action="frame_align", payload={"src": "a", "t_us": 10000000, **extra}
            ),
        )

    await send(source)
    for follower in followers:
        messages = [
            json.loads(c.args[0])
            for c in cast(AsyncMock, follower.websocket.send_text).call_args_list
        ]
        assert [m["action"] for m in messages] == ["align_authority", "frame_align"]
    expected = cast(AsyncMock, followers[0].websocket.send_text).call_args_list
    assert expected == cast(AsyncMock, followers[1].websocket.send_text).call_args_list
    await send(followers[0], t_us=20000000)  # spoofing src is insufficient
    assert cast(AsyncMock, followers[1].websocket.send_text).call_args_list == expected
    await cm.disconnect(source)
    await cm._flush_align_notifications()
    frozen = json.loads(
        cast(AsyncMock, followers[1].websocket.send_text).call_args.args[0]
    )["payload"]
    assert frozen["frozen"] and frozen["stale"]
    assert frozen["seq"] == 2
    assert frozen["t_us"] >= 10000000


async def test_timeout_without_traffic(world, monkeypatch):
    client, _, match, _ = world
    cm = client.app.state.connection_manager
    store = cm.registry.get_or_create(match)
    source = Connection(AsyncMock(), "acc", "director", Seat.DIRECTOR, match.id)
    follower = Connection(AsyncMock(), "acc", "director", Seat.DIRECTOR, match.id)
    store.directors.update([source, follower])
    now = 1760000000000
    monkeypatch.setattr(module, "_now_ms", lambda: now)
    assert publish(cm, {"src": "a", "t_us": 10000000}, match=match.id, conn=source)[0]
    now += 5001
    await cm._expire_align("acc", match.id)
    a = anchor(cm, match=match.id)
    assert a["frozen"] and a["stale"] and a["t_us"] == 11000000
    assert a["seq"] == 2
    assert (
        json.loads(cast(AsyncMock, follower.websocket.send_text).call_args.args[0])[
            "payload"
        ]
        == a
    )
    assert publish(cm, {"src": "b", "t_us": 12000000}, match=match.id, conn=follower)[0]
    assert anchor(cm, match=match.id)["epoch"] == 2
    assert not publish(cm, {"src": "a", "t_us": 13000000}, match=match.id, conn=source)[
        0
    ]


def test_zero_and_pause_replay_age(world, monkeypatch):
    cm = world[0].app.state.connection_manager
    now = 1760000000000
    monkeypatch.setattr(module, "_now_ms", lambda: now)
    assert publish(cm, {"src": "a", "t_us": 0})[0]
    assert anchor(cm)["t_us"] == 0  # legacy unready sentinel, never invented T
    assert publish(cm, {"src": "a", "t_us": 123000, "paused": True})[0]
    now += 700
    a = anchor(cm)
    assert a["effective_at_ms"] == now - 700
    assert a["server_now_ms"] == now and a["t_us"] == 123000
    st = cm._director_state[("acc", "match")]
    cm._freeze_align(st)
    assert anchor(cm)["t_us"] == 123000


async def test_scene_epoch_and_retired_socket_cannot_rename(world, monkeypatch):
    client, _, match, _ = world
    cm = client.app.state.connection_manager
    store = cm.registry.get_or_create(match)
    a, b = [
        Connection(AsyncMock(), "acc", "d", Seat.DIRECTOR, match.id) for _ in range(2)
    ]
    store.directors.update([a, b])
    now = 1760000000000
    monkeypatch.setattr(module, "_now_ms", lambda: now)
    assert publish(
        cm, {"src": "a", "t_us": 100, "scene": "match"}, match=match.id, conn=a
    )[0]
    assert publish(
        cm, {"src": "a", "t_us": 101, "scene": "soon"}, match=match.id, conn=a
    ) == (True, True)
    assert anchor(cm, match=match.id)["epoch"] == 2
    now += 5001
    assert publish(cm, {"src": "b", "t_us": 200}, match=match.id, conn=b)[0]
    now += 5001
    assert not publish(cm, {"src": "new-name", "t_us": 300}, match=match.id, conn=a)[0]


def test_websocket_watchdog_and_late_frozen_replay(world, monkeypatch):
    from tests.test_director_command import _recv_until, _state_sync

    client, _, _, tokens = world
    monkeypatch.setattr(module, "_ALIGN_AUTHORITY_TIMEOUT_MS", 50)
    with client.websocket_connect(f"/ws/{tokens['dri']}") as source:
        source.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {"src": "a", "t_us": 10000000},
            }
        )
        # The watchdog runs without further messages from any page.
        frozen = _recv_until(source, lambda m: m.get("payload", {}).get("stale"))
        assert frozen["payload"]["frozen"]
        with client.websocket_connect(f"/ws/{tokens['dri']}") as follower:
            replay = _state_sync(follower)["frame_align"]
            assert replay["seq"] == frozen["payload"]["seq"]
            assert replay["t_us"] == frozen["payload"]["t_us"]
            assert replay["epoch"] == 1 and replay["frozen"]


async def test_broadcast_scope_isolation_and_dead_socket(world):
    client, _, match, _ = world
    cm = client.app.state.connection_manager
    store = cm.registry.get_or_create(match)
    source, follower = [
        Connection(AsyncMock(), "acc", "d", Seat.DIRECTOR, match.id) for _ in range(2)
    ]
    other = Connection(AsyncMock(), "other", "d", Seat.DIRECTOR, match.id)
    other_match = match.model_copy(update={"id": "other-match"})
    other_store = cm.registry.get_or_create(other_match)
    other_page = Connection(AsyncMock(), "acc", "d", Seat.DIRECTOR, other_match.id)
    other_store.directors.add(other_page)
    store.directors.update([source, follower, other])
    from twilightcupbackend.protocol import ClientDirectorCommand

    await cm._dispatch(
        source,
        ClientDirectorCommand(
            action="frame_align", payload={"src": "a", "t_us": 10000000}
        ),
    )
    cast(AsyncMock, other.websocket.send_text).assert_not_called()
    cast(AsyncMock, other_page.websocket.send_text).assert_not_called()
    cast(AsyncMock, follower.websocket.send_text).side_effect = RuntimeError("closed")
    await cm.disconnect(source)
    await cm._flush_align_notifications()
    assert follower not in store.directors
    cast(AsyncMock, other.websocket.send_text).assert_not_called()
    cast(AsyncMock, other_page.websocket.send_text).assert_not_called()


def test_websocket_disconnect_freezes_followers(world):
    from tests.test_director_command import _recv_until

    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}") as follower:
        with client.websocket_connect(f"/ws/{tokens['dri']}") as source:
            source.send_json(
                {
                    "type": "director_command",
                    "action": "frame_align",
                    "payload": {"src": "a", "t_us": 10000000},
                }
            )
            a = _recv_until(follower, lambda m: m.get("action") == "frame_align")
        frozen = _recv_until(follower, lambda m: m.get("action") == "frame_align")
        assert frozen["payload"]["frozen"] and frozen["payload"]["stale"]
        assert frozen["payload"]["epoch"] == a["payload"]["epoch"]
        assert frozen["payload"]["seq"] == a["payload"]["seq"] + 1


async def test_takeover_broadcast_orders_authority_before_anchor(world, monkeypatch):
    from twilightcupbackend.protocol import ClientDirectorCommand

    client, _, match, _ = world
    cm = client.app.state.connection_manager
    store = cm.registry.get_or_create(match)
    a, b, follower = [
        Connection(AsyncMock(), "acc", "d", Seat.DIRECTOR, match.id) for _ in range(3)
    ]
    store.directors.update([a, b, follower])
    now = 1760000000000
    monkeypatch.setattr(module, "_now_ms", lambda: now)
    for conn, src, t in [(a, "a", 10000000), (b, "b", 20000000)]:
        await cm._dispatch(
            conn,
            ClientDirectorCommand(
                action="frame_align", payload={"src": src, "t_us": t}
            ),
        )
        now += 5001
    messages = [
        json.loads(c.args[0])
        for c in cast(AsyncMock, follower.websocket.send_text).call_args_list
    ]
    assert [m["action"] for m in messages] == [
        "align_authority",
        "frame_align",
        "align_authority",
        "frame_align",
    ]
    assert messages[-2]["payload"]["src"] == "b"
    assert messages[-1]["payload"]["epoch"] == 2
    assert messages[-1]["payload"]["seq"] == 1
    await cm._dispatch(
        a,
        ClientDirectorCommand(
            action="frame_align", payload={"src": "a", "t_us": 30000000}
        ),
    )
    assert len(cast(AsyncMock, follower.websocket.send_text).call_args_list) == 4
