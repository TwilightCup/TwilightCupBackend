"""Old lease-shaped status packets are optional diagnostics, never elections."""

from typing import cast
from unittest.mock import AsyncMock

import pytest

from tests.test_frame_align_authority import raw_publish as publish
from tests.test_frame_align_authority import scope  # noqa: F401
from twilightcupbackend import connection_manager as module
from twilightcupbackend.protocol import ClientDirectorCommand


async def report(cm, conn, seq, **extra):
    st = cm._director_state[(conn.account_id, conn.match_id)]
    payload = {
        "connection_id": conn.connection_id,
        "account_id": conn.account_id,
        "match_id": conn.match_id,
        "authority_epoch": st.align_epoch,
        "seq": seq,
        "capability": True,
        "visibility": "visible",
        "progress_t_us": 10000000 + seq,
        "media_ready": True,
        "decode_ready": True,
        "state": "running",
        "active_sides": ["A", "B"],
        "waiting_sides": [],
        **extra,
    }
    await cm._dispatch(
        conn, ClientDirectorCommand(action="frame_align_status", payload=payload)
    )


@pytest.fixture
def lease(scope, monkeypatch):  # noqa: F811
    """Legacy fixture name retained for reset tests; no lease is installed."""
    cm, pages, store = scope
    clock = [100000]
    monkeypatch.setattr(module, "_align_now_ms", lambda: clock[0])
    return cm, pages, store, clock


async def bootstrap(cm, pages, clock, **extra):
    st = cm._director_state[(pages[0].account_id, pages[0].match_id)]
    assert st.align_owner is pages[0] and pages[0].align_lease.status is None
    assert publish(
        cm,
        pages[0],
        t_us=10000000,
        seq=1,
        epoch=st.align_epoch,
        active_sides=["A", "B"],
        waiting_sides=[],
    )[0]
    return st


@pytest.mark.parametrize("state", ["media_wait", "paused", "relinquish", "running"])
async def test_status_and_visibility_never_revoke_or_freeze(lease, state):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    owner = pages[0]
    before = dict(st.align_anchor)
    for seq in range(1, 5):
        clock[0] += 60000
        await report(
            cm,
            owner,
            seq,
            state=state,
            capability=False,
            media_ready=False,
            decode_ready=False,
            visibility="hidden",
            progress_t_us=0,
            active_sides=[],
            waiting_sides=["A", "B"],
        )
        await cm._expire_align(owner.account_id, owner.match_id)
        assert st.align_owner is owner and st.align_anchor == before
    for page in pages:
        cast(AsyncMock, page.websocket.send_text).assert_not_called()
    assert publish(
        cm,
        owner,
        t_us=11000000,
        seq=2,
        epoch=st.align_epoch,
        rate=0,
        paused=True,
        frozen=True,
    )[0]
    assert st.align_anchor["paused"] and st.align_anchor["frozen"]


@pytest.mark.parametrize(
    "extra",
    [
        {"seq": 1},
        {"connection_id": "forged"},
        {"match_id": "other"},
        {"account_id": "other"},
        {"authority_epoch": 0},
        {"timeline_version": 99},
        {"seq": True},
        {"progress_t_us": "123"},
        {"server_time_ms": 1},
        {"active_sides": ["A"], "waiting_sides": ["A"]},
    ],
)
async def test_invalid_reports_do_not_replace_diagnostics_or_owner(lease, extra):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    owner = pages[0]
    await report(cm, owner, 1)
    before = owner.align_lease.status
    clock[0] += 60000
    await report(cm, owner, **{"seq": 2, **extra})
    await cm._expire_align(owner.account_id, owner.match_id)
    assert owner.align_lease.status is before
    assert st.align_owner is owner


async def test_status_sequence_does_not_reset_with_authority_epoch(lease):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    owner = pages[0]
    await report(cm, owner, 10)
    assert publish(cm, owner, t_us=11000000, seq=2, epoch=st.align_epoch, scene="new")[
        0
    ]
    await report(cm, owner, 9)
    assert owner.align_lease.status.seq == 10
    await report(cm, owner, 11)
    assert owner.align_lease.status.authority_epoch == st.align_epoch


async def test_missing_or_old_frame_sequence_still_rejected(lease):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    owner = pages[0]
    assert not publish(cm, owner, t_us=11000000, epoch=st.align_epoch)[0]
    assert not publish(cm, owner, t_us=11000000, epoch=st.align_epoch, seq=1)[0]
    assert not publish(cm, owner, t_us=11000000, seq=2)[0]


async def test_console_scope_isolation_and_takeover(lease):
    from twilightcupbackend.datatypes import Seat
    from twilightcupbackend.stores import Connection

    cm, pages, store, clock = lease
    st = await bootstrap(cm, pages, clock)
    other_store = cm.registry.get_or_create(
        store.match.model_copy(update={"id": "other"})
    )
    peers = [
        Connection(
            AsyncMock(), "other", "d", Seat.DIRECTOR, store.id, align_client="console"
        ),
        Connection(
            AsyncMock(),
            pages[0].account_id,
            "d",
            Seat.DIRECTOR,
            other_store.id,
            align_client="console",
        ),
    ]
    for conn, target in zip(peers, (store, other_store), strict=True):
        assert cm._add_director(target, conn) == []
        conn.auth_sent = True
        assert cm._director_state[(conn.account_id, conn.match_id)].align_owner is conn
    replacement = Connection(
        AsyncMock(),
        pages[0].account_id,
        "d",
        Seat.DIRECTOR,
        store.id,
        align_client="console",
    )
    assert cm._add_director(store, replacement) == [pages[0]]
    replacement.auth_sent = True
    await cm._announce_authority(replacement.account_id, replacement.match_id, st)
    await cm._broadcast_align_scope(replacement.account_id, replacement.match_id)
    assert not publish(cm, replacement, t_us=1, seq=1, epoch=st.align_epoch)[0]
    assert publish(cm, replacement, t_us=11000000, seq=1, epoch=st.align_epoch)[0]
    for peer in peers:
        assert cm._director_state[(peer.account_id, peer.match_id)].align_owner is peer
        cast(AsyncMock, peer.websocket.send_text).assert_not_called()
