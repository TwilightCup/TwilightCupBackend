"""Only explicitly declared consoles can produce frame time."""

from unittest.mock import AsyncMock

import pytest

from tests.test_frame_align_authority import scope  # noqa: F401
from tests.test_frame_align_election import auth
from tests.test_frame_align_lease import bootstrap, lease, report  # noqa: F401
from twilightcupbackend.datatypes import Seat
from twilightcupbackend.protocol import ClientDirectorCommand
from twilightcupbackend.stores import Connection


@pytest.mark.parametrize("query", ["", "?align_client=stage"])
def test_receivers_never_get_initial_authority(world, query):
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}{query}") as ws:
        a = auth(ws)
        assert a["align_role"] == "follower"
        assert a["align_authority_src"] is None


@pytest.mark.parametrize("value", ["", "CONSOLE", "bad"])
def test_invalid_purpose_rejected(world, value):
    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['dri']}?align_client={value}") as ws:
        assert ws.receive_json()["type"] == "auth_error"


async def test_stage_lies_about_capability_cannot_displace_console(lease):  # noqa: F811
    cm, pages, store, clock = lease
    st = await bootstrap(cm, pages, clock)
    owner, epoch = st.align_owner, st.align_epoch
    stage = Connection(
        AsyncMock(),
        pages[0].account_id,
        "s",
        Seat.DIRECTOR,
        store.id,
        align_client="stage",
    )
    cm._add_director(store, stage)
    stage.auth_sent = True
    await report(cm, stage, 1)
    clock[0] += 2001
    await report(cm, stage, 2)
    assert st.align_owner is owner and st.align_epoch == epoch
    for conn in pages:
        cm._remove_connection(store, conn)
    await cm._flush_align_notifications()
    assert st.align_owner is None
    assert st.align_anchor["src"] is None and st.align_anchor["frozen"]
    await cm._dispatch(
        stage,
        ClientDirectorCommand(
            action="frame_align",
            payload={
                "t_us": 90000000,
                "epoch": st.align_epoch,
                "seq": 1,
            },
        ),
    )
    assert st.align_anchor["t_us"] == 10000000


async def test_media_wait_keepalive_preserves_t_and_stops_when_owner_expires(lease):  # noqa: F811
    cm, pages, _, clock = lease
    st = await bootstrap(cm, pages, clock)
    a = pages[0]
    epoch = st.align_epoch
    await report(
        cm,
        a,
        4,
        state="media_wait",
        media_ready=False,
        decode_ready=False,
        active_sides=[],
        waiting_sides=["A", "B"],
    )
    seq = st.align_seq
    for i in range(3):
        clock[0] += 750
        await cm._expire_align(a.account_id, a.match_id)
        assert st.align_owner is a and st.align_epoch == epoch
        assert st.align_seq == seq + i + 1
        assert st.align_anchor["t_us"] == 10000000
        assert st.align_anchor["frozen"] and not st.align_anchor["stale"]
        assert st.align_anchor["reason"] == "media_wait"
    clock[0] += 5001
    await cm._expire_align(a.account_id, a.match_id)
    assert st.align_owner is None and st.align_anchor["src"] is None
    assert st.align_anchor["stale"] and st.align_anchor["t_us"] == 10000000
    seq = st.align_seq
    clock[0] += 1000
    await cm._expire_align(a.account_id, a.match_id)
    assert st.align_seq == seq


async def test_unready_console_is_not_promoted_and_recovery_preserves_floor(lease):  # noqa: F811
    cm, pages, store, clock = lease
    st = await bootstrap(cm, pages[:1], clock)
    a, b, _ = pages
    epoch = st.align_epoch
    await report(cm, b, 1, decode_ready=False)
    assert st.align_owner is a and st.align_epoch == epoch
    cm._remove_connection(store, a)
    await cm._flush_align_notifications()
    assert st.align_owner is None
    await report(cm, b, 2)
    clock[0] += 2001
    await report(cm, b, 3)
    assert st.align_owner is b
    await report(cm, b, 4)
    assert not cm._update_director_state(
        b.account_id,
        b.match_id,
        "frame_align",
        {"t_us": 9999999, "epoch": st.align_epoch, "seq": 1},
        b,
    )[0]


def sync_socket(ws, auth_info):
    ws.send_json({"type": "console_test_barrier"})
    while True:
        m = ws.receive_json()
        if m.get("action") == "align_authority":
            auth_info["authority_epoch"] = m["payload"]["epoch"]
            auth_info["align_role"] = m["payload"]["role"]
        if m.get("type") == "error" and m.get("code") == 400:
            return


def socket_status(ws, a, seq, **extra):
    ws.send_json(
        {
            "type": "director_command",
            "action": "frame_align_status",
            "payload": {
                "connection_id": a["connection_id"],
                "account_id": a["account_id"],
                "match_id": a["match_id"],
                "authority_epoch": a["authority_epoch"],
                "seq": seq,
                "capability": True,
                "visibility": "visible",
                "progress_t_us": 10000000,
                "media_ready": True,
                "decode_ready": True,
                "state": "running",
                "active_sides": ["A"],
                "waiting_sides": ["B"],
                **extra,
            },
        }
    )
    sync_socket(ws, a)


def test_console_stage_wire_order_takeover_and_late_replay(world, monkeypatch):
    from tests.test_frame_align_election import event, frame
    from twilightcupbackend import connection_manager as module

    client, _, _, tokens = world
    clock = [100000]
    monkeypatch.setattr(module, "_align_now_ms", lambda: clock[0])
    url = f"/ws/{tokens['dri']}"
    with (
        client.websocket_connect(url + "?align_client=stage") as stage,
        client.websocket_connect(url + "?align_client=stage") as stage2,
        client.websocket_connect(url + "?align_client=console") as first,
    ):
        s, s2, a = auth(stage), auth(stage2), auth(first)
        assert all(x["align_role"] == "follower" for x in (s, s2, a))
        socket_status(first, a, 1)
        clock[0] += 2001
        socket_status(first, a, 2)
        assert a["align_role"] == "publisher"
        socket_status(first, a, 3)
        frame(first, 10000000, seq=1, epoch=a["authority_epoch"], extension="retained")
        x, y = event(stage, "frame_align"), event(stage2, "frame_align")
        assert x == y and x["src"] == a["connection_id"]
        with client.websocket_connect(url + "?align_client=console") as second:
            b = auth(second)
            assert b["align_role"] == "follower"
            assert b["authority_epoch"] == a["authority_epoch"]
            replay = event(second, "state_sync")["frame_align"]
            assert replay["extension"] == "retained"
            assert replay["active_sides"] == ["A"] and replay["waiting_sides"] == ["B"]
            socket_status(second, b, 1)
            clock[0] += 2001
            socket_status(second, b, 2)
            assert b["align_role"] == "follower"
            first.close()
            promotion = event(second, "align_authority")
            assert (
                promotion["role"] == "publisher" and promotion["t_floor_us"] == 10000000
            )
            b["authority_epoch"] = promotion["epoch"]
            frozen = event(second, "frame_align")
            assert frozen["frozen"] and frozen["t_us"] == 10000000
            # Same socket: readiness report must be handled before this frame.
            socket_status(second, b, 3)
            frame(second, 11000000, seq=1, epoch=b["authority_epoch"])
            event(stage, "frame_align")  # takeover frozen anchor
            live = event(stage, "frame_align")
            assert live["t_us"] == 11000000 and live["src"] == b["connection_id"]
            second.close()
            gone = event(stage, "align_authority")
            # Stage may still have the previous promotion queued.
            if gone["src"] is not None:
                gone = event(stage, "align_authority")
            assert gone["src"] is None
            assert event(stage, "frame_align")["stale"]


@pytest.mark.parametrize("purpose", ["stage", None])
async def test_only_receivers_remain_masterless_despite_ready_reports(lease, purpose):  # noqa: F811
    cm, pages, _, clock = lease
    for page in pages:
        page.align_client = purpose
        await report(cm, page, 1)
    clock[0] += 3000
    for page in pages:
        await report(cm, page, 2)
        assert not cm._update_director_state(
            page.account_id,
            page.match_id,
            "frame_align",
            {"t_us": 10000000, "seq": 1},
            page,
        )[0]
    st = cm._director_state[(pages[0].account_id, pages[0].match_id)]
    assert st.align_owner is None and st.align_anchor is None


def test_console_query_does_not_expand_seat_or_match_authorization(world):
    from tests.test_director_command import _recv_until

    client, _, _, tokens = world
    with client.websocket_connect(f"/ws/{tokens['ref']}?align_client=console") as ws:
        a = auth(ws)
        assert a["align_role"] is None
        ws.send_json(
            {
                "type": "director_command",
                "action": "frame_align",
                "payload": {"t_us": 100},
            }
        )
        assert _recv_until(ws, lambda m: m.get("type") == "error")["code"] == 403
    with client.websocket_connect(
        f"/ws/{tokens['dri']}?align_client=console&match=missing"
    ) as ws:
        assert ws.receive_json()["type"] == "auth_error"
