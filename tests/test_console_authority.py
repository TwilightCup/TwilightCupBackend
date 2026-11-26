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


async def test_unready_new_console_takes_over_and_preserves_floor(lease):  # noqa: F811
    cm, pages, store, clock = lease
    st = await bootstrap(cm, pages, clock)
    old = pages[0]
    replacement = Connection(
        AsyncMock(),
        old.account_id,
        "new",
        Seat.DIRECTOR,
        store.id,
        align_client="console",
    )
    epoch = st.align_epoch
    assert cm._add_director(store, replacement) == [old]
    assert st.align_owner is replacement and st.align_epoch > epoch
    await report(cm, replacement, 1, decode_ready=False, state="media_wait")
    cm._remove_connection(store, old)
    await cm._flush_align_notifications()
    assert st.align_owner is replacement
    assert not cm._update_director_state(
        replacement.account_id,
        store.id,
        "frame_align",
        {"t_us": 9999999, "epoch": st.align_epoch, "seq": 1},
        replacement,
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


def test_console_stage_extension_and_paused_anchor_replay(world):
    from tests.test_frame_align_election import event, frame

    client, _, _, tokens = world
    url = f"/ws/{tokens['dri']}"
    with (
        client.websocket_connect(url + "?align_client=console") as console,
        client.websocket_connect(url + "?align_client=stage") as stage,
    ):
        a = auth(console)
        assert a["align_role"] == "publisher" and not a["align_lease_required"]
        auth(stage)
        socket_status(console, a, 1, capability=False, state="media_wait")
        frame(
            console,
            10000000,
            seq=1,
            epoch=a["authority_epoch"],
            extension="retained",
            active_sides=["A"],
            waiting_sides=["B"],
            paused=True,
            frozen=True,
            rate=0,
        )
        anchor = event(stage, "frame_align")
        with client.websocket_connect(url + "?align_client=stage") as late:
            assert auth(late)["align_authority_src"] == a["connection_id"]
            replay = event(late, "state_sync")["frame_align"]
            for key in (
                "epoch",
                "seq",
                "src",
                "extension",
                "t_us",
                "active_sides",
                "waiting_sides",
                "paused",
                "frozen",
                "rate",
            ):
                assert replay[key] == anchor[key]


@pytest.mark.parametrize("purpose", ["stage", None])
async def test_only_receivers_remain_masterless_despite_ready_reports(lease, purpose):  # noqa: F811
    cm, pages, store, clock = lease
    for page in pages:
        cm._remove_connection(store, page)
    await cm._flush_align_notifications()
    for page in pages:
        page.align_client = purpose
        cm._add_director(store, page)
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
