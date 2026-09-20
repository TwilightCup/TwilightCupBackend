"""Lease election uses server receive time and playback readiness, not focus."""

import json
from typing import cast
from unittest.mock import AsyncMock

import pytest

from tests.test_frame_align_authority import raw_publish as publish
from tests.test_frame_align_authority import scope, snapshot  # noqa: F401
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
    cm, pages, store = scope
    from twilightcupbackend.stores import FrameAlignLease

    st = cm._director_state[(pages[0].account_id, pages[0].match_id)]
    st.align_owner = None
    st.align_authority_src = None
    for page in pages:
        page.align_lease = FrameAlignLease()
    clock = [100000]
    monkeypatch.setattr(module, "_align_now_ms", lambda: clock[0])
    return cm, pages, store, clock


async def bootstrap(cm, pages, clock, **extra):
    for conn in pages:
        await report(cm, conn, 1, **extra)
    clock[0] += 2001
    for conn in pages:
        await report(cm, conn, 2, **extra)
    st = cm._director_state[(pages[0].account_id, pages[0].match_id)]
    assert st.align_owner is pages[0]
    await report(cm, pages[0], 3, **extra)
    assert publish(cm, pages[0], t_us=10000000, seq=1, epoch=st.align_epoch)[0]
    return st


async def test_live_socket_stalls_promotes_follower_and_fences_old_owner(lease):
    cm, pages, store, clock = lease
    a, b, c = pages
    st = await bootstrap(cm, pages, clock)
    old_epoch = st.align_epoch
    clock[0] += 4000
    await report(cm, b, 4)
    await report(cm, c, 4)
    clock[0] += 1001
    await cm._expire_align(a.account_id, a.match_id)
    assert store.has_connection(a) and st.align_owner is b
    assert st.align_epoch > old_epoch
    anchor = snapshot(cm, b)
    assert anchor["frozen"] and anchor["t_us"] == 10000000
    assert anchor["active_sides"] == ["A", "B"] and anchor["waiting_sides"] == []
    assert not publish(cm, a, t_us=20000000, epoch=old_epoch)[0]
    assert not publish(cm, b, t_us=11000000, seq=1, epoch=st.align_epoch)[0]
    await report(cm, b, 5, progress_t_us=11000000)
    assert publish(cm, b, t_us=11000000, seq=1, epoch=st.align_epoch)[0]
    await report(cm, a, 9, authority_epoch=old_epoch)
    assert st.align_owner is b


async def test_hidden_progress_and_media_wait_do_not_churn(lease):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock, visibility="hidden")
    owner, epoch = st.align_owner, st.align_epoch
    for seq in range(4, 10):
        clock[0] += 2000
        for conn in pages:
            await report(cm, conn, seq, visibility="hidden")
        await cm._expire_align(pages[0].account_id, pages[0].match_id)
        assert st.align_owner is owner and st.align_epoch == epoch
    for seq in range(10, 14):
        clock[0] += 2000
        for conn in pages:
            await report(
                cm,
                conn,
                seq,
                state="media_wait",
                media_ready=False,
                decode_ready=False,
                active_sides=[],
                waiting_sides=["A", "B"],
            )
        assert st.align_owner is owner and st.align_epoch == epoch
    assert snapshot(cm, pages[0])["frozen"]
    assert snapshot(cm, pages[0])["reason"] == "media_wait"


async def test_all_suspended_freezes_once_and_incapable_page_cannot_own(lease):
    cm, pages, _, clock = lease
    a, b, _ = pages
    await report(cm, a, 1, capability=False)
    await report(cm, b, 1)
    clock[0] += 2001
    await report(cm, b, 2)
    st = cm._director_state[(a.account_id, a.match_id)]
    assert st.align_owner is b
    await report(cm, b, 3)
    assert publish(cm, b, t_us=10000000, seq=1, epoch=st.align_epoch)[0]
    clock[0] += 5001
    await cm._expire_align(a.account_id, a.match_id)
    assert st.align_owner is None and snapshot(cm, b)["frozen"]
    epoch = st.align_epoch
    await cm._expire_align(a.account_id, a.match_id)
    assert st.align_epoch == epoch


async def test_takeover_deadline_and_explicit_decline(lease):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    a, b, c = pages
    await report(cm, a, 4, state="relinquish")
    assert st.align_owner is b
    clock[0] += 3001
    await report(cm, c, 4)
    await cm._expire_align(a.account_id, a.match_id)
    assert st.align_owner is c
    await report(cm, c, 5, capability=False)
    assert st.align_owner is None


async def test_invalid_reports_do_not_refresh_lease(lease):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    a = pages[0]
    expires_at = clock[0] + 5001
    for extra in (
        {"seq": 3},
        {"connection_id": "forged"},
        {"match_id": "other"},
        {"account_id": "other"},
        {"authority_epoch": st.align_epoch - 1},
        {"seq": True},
        {"progress_t_us": "123"},
        {"server_time_ms": 9999999999999},
        {"active_sides": ["A"], "waiting_sides": ["A"]},
    ):
        clock[0] += 500
        await report(cm, a, **{"seq": 10, **extra})
    clock[0] = expires_at
    await cm._expire_align(a.account_id, a.match_id)
    assert st.align_owner is None
    rows = [
        json.loads(c.args[0])
        for c in cast(AsyncMock, a.websocket.send_text).call_args_list
    ]
    assert any(row.get("type") == "error" for row in rows)


async def test_stability_visibility_preference_and_degraded_replay(lease):
    cm, pages, _, clock = lease
    a, b, c = pages
    st = await bootstrap(cm, pages, clock)
    await report(cm, b, 4, visibility="hidden")
    await report(cm, c, 4, active_sides=["A"], waiting_sides=["B"])
    await report(cm, a, 4, state="relinquish")
    assert st.align_owner is c  # Visible and progressing beats older hidden page.
    await report(cm, c, 5, active_sides=["A"], waiting_sides=["B"])
    assert publish(
        cm, c, t_us=11000000, seq=1, epoch=st.align_epoch, extension={"x": 1}
    )[0]
    await report(cm, c, 6, state="relinquish")
    replay = snapshot(cm, b)
    assert replay["active_sides"] == ["A"] and replay["waiting_sides"] == ["B"]
    assert replay["extension"] == {"x": 1} and replay["t_us"] == 11000000
    assert st.align_owner is b
    await report(cm, b, 5, progress_t_us=11000000)
    assert publish(cm, b, t_us=11000000, seq=1, epoch=st.align_epoch)[0]
    # A short visibility change does not preempt a healthy owner.
    await report(cm, a, 5)
    clock[0] += 2001
    await report(cm, a, 6)
    assert st.align_owner is b


async def test_late_renewal_cannot_beat_expiry_and_frame_requires_sequence(lease):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    a, b, _ = pages
    assert not publish(cm, a, t_us=11000000, epoch=st.align_epoch)[0]
    assert not publish(cm, a, t_us=11000000, epoch=st.align_epoch, seq=1)[0]
    clock[0] += 4000
    await report(cm, b, 4)
    clock[0] += 1001
    old_epoch = st.align_epoch
    await report(cm, a, 10, authority_epoch=old_epoch)
    assert st.align_owner is b and st.align_epoch > old_epoch


async def test_lease_mode_does_not_cross_account_or_match(lease):
    from twilightcupbackend.datatypes import Seat
    from twilightcupbackend.stores import Connection

    cm, pages, store, clock = lease
    other_store = cm.registry.get_or_create(
        store.match.model_copy(update={"id": "other"})
    )
    other_account = Connection(
        AsyncMock(), "other", "d", Seat.DIRECTOR, store.id, align_client="console"
    )
    other_match = Connection(
        AsyncMock(),
        pages[0].account_id,
        "d",
        Seat.DIRECTOR,
        other_store.id,
        align_client="console",
    )
    for conn, target in ((other_account, store), (other_match, other_store)):
        cm._add_director(target, conn)
        conn.auth_sent = True
    await bootstrap(cm, pages, clock)
    for conn in (other_account, other_match):
        st = cm._director_state[(conn.account_id, conn.match_id)]
        assert st.lease_mode and st.align_owner is None
        cast(AsyncMock, conn.websocket.send_text).assert_not_called()
        await report(cm, conn, 1)
    clock[0] += 2001
    for conn in (other_account, other_match):
        await report(cm, conn, 2)
        st = cm._director_state[(conn.account_id, conn.match_id)]
        assert st.align_owner is conn
    assert (
        cm._director_state[(pages[0].account_id, pages[0].match_id)].align_owner
        is pages[0]
    )


async def test_takeover_confirmation_must_reach_t_floor(lease):
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    a, b, _ = pages
    await report(cm, a, 4, state="relinquish")
    assert st.align_owner is b
    await report(cm, b, 4, progress_t_us=1)
    assert not publish(cm, b, t_us=11000000, seq=1, epoch=st.align_epoch)[0]
    await report(cm, b, 5, progress_t_us=10000000)
    assert publish(cm, b, t_us=11000000, seq=1, epoch=st.align_epoch)[0]
