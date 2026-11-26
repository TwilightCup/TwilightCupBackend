"""Anchor validation and lifecycle ownership with one console and many stages."""

import json
from typing import cast
from unittest.mock import AsyncMock

import pytest

from twilightcupbackend import connection_manager as module
from twilightcupbackend.connection_manager import ConnectionManager
from twilightcupbackend.datatypes import Seat
from twilightcupbackend.stores import Connection, MatchRegistry


@pytest.fixture
def scope(world):
    client, db, match, _ = world
    cm = ConnectionManager(db, MatchRegistry(), client.app.state.settings)
    cm.match_engine = client.app.state.connection_manager.match_engine
    store = cm.registry.get_or_create(match)
    pages = [
        Connection(
            AsyncMock(),
            match.director_id,
            "d",
            Seat.DIRECTOR,
            match.id,
            align_client="console" if i == 0 else "stage",
        )
        for i in range(3)
    ]
    for conn in pages:
        cm._add_director(store, conn)
        conn.auth_sent = True
    return cm, pages, store


def publish(cm, conn, **payload):
    st = cm._director_state[(conn.account_id, conn.match_id)]
    payload.setdefault("epoch", st.align_epoch)
    payload.setdefault("seq", (st.align_client_seq or 0) + 1)
    return raw_publish(cm, conn, **payload)


def raw_publish(cm, conn, **payload):
    return cm._update_director_state(
        conn.account_id, conn.match_id, "frame_align", payload, conn
    )


def snapshot(cm, conn):
    return cm._director_state_payload(conn.account_id, conn.match_id)["frame_align"]


def test_complete_anchor_and_fencing(scope, monkeypatch):
    cm, (source, follower, _), _store = scope
    monkeypatch.setattr(module, "_now_ms", lambda: 1760000000000)
    assert publish(
        cm,
        source,
        t_us=1234567890000,
        seq=12,
        server_time_ms=1,
        server_now_ms=2,
        effective_at_ms=3,
        extension="kept",
    )[0]
    a = snapshot(cm, source)
    assert (
        a.items()
        >= {
            "t_us": 1234567890000,
            "rate": 1.0,
            "paused": False,
            "frozen": False,
            "seq": 1,
            "effective_at_ms": 1760000000000,
            "server_now_ms": 1760000000000,
            "server_time_ms": 1760000000000,
            "src": source.connection_id,
            "source_id": source.connection_id,
            "match_id": source.match_id,
            "account_id": source.account_id,
            "extension": "kept",
        }.items()
    )
    assert a["authority_epoch"] == a["epoch"]
    for payload in (
        {"seq": 12},
        {"seq": 11},
        {"seq": 13, "epoch": a["epoch"] - 1},
        {"seq": 13, "authority_epoch": a["epoch"] - 1},
        {"seq": 13, "t_us": 1},
    ):
        assert not publish(cm, source, **{"t_us": a["t_us"], **payload})[0]
    assert not publish(cm, follower, t_us=9999999999999, src=source.connection_id)[0]
    assert snapshot(cm, source) == a
    assert publish(cm, source, t_us=a["t_us"], seq=13, epoch=a["epoch"])[0]
    assert snapshot(cm, source)["seq"] == 2


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
        {"connection_id": "other"},
        {"active_sides": ["A"]},
        {"active_sides": ["A"], "waiting_sides": ["A"]},
        {"active_sides": ["C"], "waiting_sides": ["B"]},
        {"active_sides": "A", "waiting_sides": ["B"]},
        {"active_sides": [{"side": "A"}], "waiting_sides": ["B"]},
        {"seq": True},
        {"epoch": None},
        {"scene": []},
        {"source_id": {}},
    ],
)
def test_invalid_anchor_does_not_change_elected_owner(scope, extra):
    cm, (source, *_), _store = scope
    assert not publish(cm, source, **{"t_us": 100, **extra})[0]
    st = cm._director_state[(source.account_id, source.match_id)]
    assert st.align_owner is source and st.align_anchor is None


async def test_silence_preserves_owner_and_disconnect_never_promotes_stage(
    scope, monkeypatch
):
    cm, (source, follower, _), store = scope
    mono, wall = [100000], [1760000000000]
    monkeypatch.setattr(module, "_now_ms", lambda: wall[0])
    monkeypatch.setattr(module, "_align_now_ms", lambda: mono[0])
    assert publish(cm, source, t_us=10000000)[0]
    st = cm._director_state[(source.account_id, source.match_id)]
    before = dict(st.align_anchor)
    for delta in (86400000, -172800000):
        wall[0] += delta
        mono[0] += 600000
        await cm._expire_align(source.account_id, source.match_id)
        assert st.align_owner is source and st.align_anchor == before
    cm._remove_connection(store, source)
    await cm._flush_align_notifications()
    assert st.align_owner is None
    assert snapshot(cm, source)["src"] is None
    assert snapshot(cm, source)["frozen"] and snapshot(cm, source)["stale"]
    assert not publish(cm, follower, t_us=12000000)[0]
    assert not publish(cm, source, t_us=13000000)[0]


async def test_scope_isolation_and_failed_publisher_notice(scope):
    cm, (source, follower, third), store = scope
    other = Connection(AsyncMock(), "other", "d", Seat.DIRECTOR, store.id)
    cm._add_director(store, other)
    other.auth_sent = True
    other_store = cm.registry.get_or_create(
        store.match.model_copy(update={"id": "other-match"})
    )
    other_match = Connection(
        AsyncMock(), source.account_id, "d", Seat.DIRECTOR, other_store.id
    )
    cm._add_director(other_store, other_match)
    other_match.auth_sent = True
    assert publish(cm, source, t_us=10000000)[0]
    # Failed receivers are cleaned up without acquiring publisher authority.
    cast(AsyncMock, follower.websocket.send_text).side_effect = RuntimeError("closed")
    cm._remove_connection(store, source)
    await cm._flush_align_notifications()
    await cm._flush_align_notifications()
    st = cm._director_state[(source.account_id, source.match_id)]
    assert st.align_owner is None
    assert not store.has_connection(follower)
    assert store.has_connection(third)
    cast(AsyncMock, other.websocket.send_text).assert_not_called()
    cast(AsyncMock, other_match.websocket.send_text).assert_not_called()
    rows = [
        json.loads(c.args[0])
        for c in cast(AsyncMock, third.websocket.send_text).call_args_list
    ]
    assert any(
        m.get("action") == "align_authority"
        and m["payload"]["role"] == "follower"
        and m["payload"]["src"] is None
        for m in rows
    )


def test_scene_epoch_zero_and_replay_age(scope, monkeypatch):
    cm, (source, *_), _ = scope
    now = 1760000000000
    monkeypatch.setattr(module, "_now_ms", lambda: now)
    assert publish(cm, source, t_us=0)[0]
    assert publish(cm, source, t_us=10000000, scene="match", paused=True)[0]
    a = snapshot(cm, source)
    now += 700
    replay = snapshot(cm, source)
    assert (
        replay["t_us"] == a["t_us"]
        and replay["effective_at_ms"] == a["effective_at_ms"]
    )
    assert replay["server_now_ms"] == now
    assert publish(cm, source, t_us=10000000, scene="soon")[0]
    assert snapshot(cm, source)["epoch"] > a["epoch"]
