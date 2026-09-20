"""Explicit reset transactions are the only way to lower the authority T floor."""

import json
from typing import cast
from unittest.mock import AsyncMock

import pytest

from tests.test_frame_align_authority import scope  # noqa: F401
from tests.test_frame_align_lease import bootstrap, lease, publish, report  # noqa: F401
from twilightcupbackend import connection_manager as module
from twilightcupbackend.protocol import ClientDirectorCommand


def messages(conn, action):
    return [
        m["payload"]
        for c in cast(AsyncMock, conn.websocket.send_text).call_args_list
        if (m := json.loads(c.args[0])).get("action") == action
    ]


async def command(cm, conn, action, **payload):
    await cm._dispatch(conn, ClientDirectorCommand(action=action, payload=payload))


async def request(cm, conn, target, request_id="r1", **extra):
    st = cm._director_state[(conn.account_id, conn.match_id)]
    p = {
        "request_id": request_id,
        "connection_id": conn.connection_id,
        "account_id": conn.account_id,
        "match_id": conn.match_id,
        "authority_epoch": st.align_epoch,
        "timeline_version": st.timeline_version,
        "target_t_us": target,
    }
    p.update(extra)
    await command(cm, conn, "frame_align_reset", **p)
    return p


async def ack(cm, conn, request_id="r1", **extra):
    st = cm._director_state[(conn.account_id, conn.match_id)]
    await command(
        cm,
        conn,
        "frame_align_reset_ack",
        request_id=request_id,
        authority_epoch=extra.pop("authority_epoch", st.align_epoch),
        timeline_version=extra.pop("timeline_version", st.timeline_version),
        outcome=extra.pop("outcome", "presented"),
        **extra,
    )


@pytest.mark.parametrize("target", [9000000, 12000000])
async def test_forward_backward_reset_requires_presentation_and_fences_old_packets(
    lease,  # noqa: F811
    monkeypatch,
    target,
):
    cm, pages, _, clock = lease
    monkeypatch.setattr(module, "_now_ms", lambda: 40000)
    st = await bootstrap(cm, pages, clock)
    owner = pages[0]
    assert publish(
        cm,
        owner,
        t_us=10000000,
        seq=2,
        epoch=st.align_epoch,
        scene="shared-playback",
        source_id="media-clock",
    )[0]
    epoch = st.align_epoch
    assert not publish(cm, owner, t_us=9000000, seq=3, epoch=epoch)[0]
    p = await request(cm, owner, target)
    assert st.timeline_version == 1 and st.align_epoch > epoch
    assert st.align_owner is owner and st.frame_align_t_us == target
    assert st.align_anchor["scene"] == "shared-playback"
    assert st.align_anchor["source_id"] == "media-clock"
    assert st.align_anchor["frozen"] and not st.align_anchor["ready_a"]
    assert messages(owner, "frame_align_reset_result")[-1]["status"] == "preparing"
    assert not publish(cm, owner, t_us=20000000, seq=3, epoch=epoch)[0]
    await report(cm, owner, 50, authority_epoch=epoch)
    await ack(
        cm, owner, presented_t_us=target, authority_epoch=epoch, timeline_version=0
    )
    assert st.reset_state["status"] == "preparing"
    assert not publish(
        cm, owner, t_us=target, seq=1, epoch=st.align_epoch, timeline_version=1
    )[0]
    await report(cm, owner, 4, timeline_version=1, progress_t_us=target)
    await ack(cm, owner, presented_t_us=target)
    assert st.reset_state["status"] == "completed"
    assert not st.align_anchor["frozen"] and st.align_anchor["ready_a"]
    assert not publish(
        cm, owner, t_us=target - 1, seq=1, epoch=st.align_epoch, timeline_version=1
    )[0]
    assert not publish(cm, owner, t_us=target, seq=1, epoch=st.align_epoch)[0]
    assert publish(
        cm, owner, t_us=target, seq=1, epoch=st.align_epoch, timeline_version=1
    )[0]
    version = st.timeline_version
    await command(cm, owner, "frame_align_reset", **p)
    assert st.timeline_version == version
    assert messages(owner, "frame_align_reset_result")[-1]["status"] == "completed"
    for follower in pages[1:]:
        notice = messages(follower, "frame_align_reset_result")[-1]
        assert notice["status"] == "completed" and notice["timeline_version"] == 1
    replay = cm._director_state_payload(owner.account_id, owner.match_id)
    assert replay["timeline_version"] == 1 and replay["reset"]["status"] == "completed"


async def test_failure_busy_idempotency_and_deadline(lease, monkeypatch):  # noqa: F811
    cm, pages, _, clock = lease
    monkeypatch.setattr(module, "_now_ms", lambda: 40000)
    st = await bootstrap(cm, pages, clock)
    a = pages[0]
    p = await request(cm, a, 9000000)
    await command(cm, a, "frame_align_reset", **p)
    assert st.timeline_version == 1
    await request(cm, a, 8000000, "r2")
    assert messages(a, "frame_align_reset_result")[-1]["code"] == "RESET_BUSY"
    await ack(cm, a, outcome="failed", reason="media_unavailable")
    assert st.reset_state["status"] == "failed" and st.align_anchor["frozen"]
    await request(cm, a, 8000000, "r3")
    assert st.timeline_version == 2
    # Keep the lease alive but never confirm presentation.
    for seq in range(4, 10):
        clock[0] += 2000
        await report(
            cm,
            a,
            seq,
            timeline_version=2,
            state="media_wait",
            media_ready=False,
            decode_ready=False,
            progress_t_us=0,
        )
        await cm._expire_align(a.account_id, a.match_id)
    assert st.reset_state["code"] == "PREPARE_TIMEOUT"
    assert st.frame_align_t_us == 8000000


@pytest.mark.parametrize(
    "extra,code",
    [
        ({"target_t_us": True}, "INVALID_REQUEST"),
        ({"target_t_us": -1}, "INVALID_REQUEST"),
        ({"target_t_us": 2**53}, "INVALID_REQUEST"),
        ({"target_t_us": 1.5}, "INVALID_REQUEST"),
        ({"target_t_us": 100000000}, "TARGET_OUT_OF_RANGE"),
        ({"account_id": "other"}, "SCOPE_MISMATCH"),
        ({"match_id": "other"}, "SCOPE_MISMATCH"),
        ({"authority_epoch": 0}, "STALE_VERSION"),
        ({"timeline_version": 9}, "STALE_VERSION"),
        ({"client_now_ms": 40000}, "INVALID_REQUEST"),
    ],
)
async def test_invalid_reset_does_not_change_playback(lease, monkeypatch, extra, code):  # noqa: F811
    cm, pages, _, clock = lease
    monkeypatch.setattr(module, "_now_ms", lambda: 40000)
    st = await bootstrap(cm, pages, clock)
    before = dict(st.align_anchor)
    await request(
        cm,
        pages[0],
        extra.get("target_t_us", 9000000),
        **{k: v for k, v in extra.items() if k != "target_t_us"},
    )
    assert st.align_anchor == before and st.timeline_version == 0
    assert messages(pages[0], "frame_align_reset_result")[-1]["code"] == code


async def test_permissions_disconnect_and_replacement_after_reset(lease, monkeypatch):  # noqa: F811
    cm, pages, store, clock = lease
    monkeypatch.setattr(module, "_now_ms", lambda: 40000)
    st = await bootstrap(cm, pages, clock)
    a, b, c = pages
    for conn in (b, c):
        await request(cm, conn, 9000000)
        assert messages(conn, "frame_align_reset_result")[-1]["code"] == "NOT_AUTHORITY"
    c.align_client = "stage"
    await request(cm, c, 9000000)
    assert st.timeline_version == 0
    await request(cm, a, 9000000)
    await report(cm, b, 4, timeline_version=1, progress_t_us=9000000)
    clock[0] += 2001
    await report(cm, b, 5, timeline_version=1, progress_t_us=9000000)
    cm._remove_connection(store, a)
    await cm._flush_align_notifications()
    assert st.reset_state["code"] == "OWNER_LOST" and st.align_owner is b
    await ack(cm, a, presented_t_us=9000000)
    assert messages(a, "frame_align_reset_result")[-1]["code"] == "NOT_AUTHORITY"
    await report(cm, b, 6, timeline_version=1, progress_t_us=9000000)
    assert publish(
        cm, b, t_us=9000000, seq=1, epoch=st.align_epoch, timeline_version=1
    )[0]
    assert st.frame_align_t_us == 9000000


async def test_expired_lease_cannot_reset_and_id_conflict(lease, monkeypatch):  # noqa: F811
    cm, pages, _, clock = lease
    monkeypatch.setattr(module, "_now_ms", lambda: 40000)
    st = await bootstrap(cm, pages, clock)
    a = pages[0]
    p = await request(cm, a, 9000000)
    await command(cm, a, "frame_align_reset", **{**p, "target_t_us": 8000000})
    assert messages(a, "frame_align_reset_result")[-1]["code"] == "REQUEST_ID_CONFLICT"
    clock[0] += 5001
    await request(cm, a, 8000000, "r2")
    assert messages(a, "frame_align_reset_result")[-1]["code"] == "LEASE_INVALID"
    assert st.timeline_version == 1


async def test_ready_without_ack_stays_frozen_and_missing_version_cannot_renew(
    lease,  # noqa: F811
    monkeypatch,
):
    cm, pages, _, clock = lease
    monkeypatch.setattr(module, "_now_ms", lambda: 40000)
    st = await bootstrap(cm, pages, clock)
    a = pages[0]
    await request(cm, a, 9000000)
    await report(cm, a, 4, timeline_version=1, progress_t_us=9000000)
    clock[0] += 1000
    await cm._expire_align(a.account_id, a.match_id)
    assert st.align_anchor["frozen"] and not st.align_anchor["ready_a"]
    assert st.frame_align_t_us == 9000000
    clock[0] += 4001
    await report(cm, a, 100)  # missing timeline_version is legacy version zero
    await cm._expire_align(a.account_id, a.match_id)
    assert st.align_owner is None and st.reset_state["code"] == "OWNER_LOST"


def test_reset_wire_broadcast_and_late_join_snapshot(world, monkeypatch):
    from tests.test_console_authority import socket_status
    from tests.test_frame_align_election import auth, event, frame

    client, _, _, tokens = world
    clock = [100000]
    monkeypatch.setattr(module, "_align_now_ms", lambda: clock[0])
    monkeypatch.setattr(module, "_now_ms", lambda: 40000)
    url = f"/ws/{tokens['dri']}"
    with (
        client.websocket_connect(url + "?align_client=console") as ws,
        client.websocket_connect(url + "?align_client=stage") as stage1,
        client.websocket_connect(url + "?align_client=stage") as stage2,
    ):
        a = auth(ws)
        auth(stage1)
        auth(stage2)
        socket_status(ws, a, 1)
        clock[0] += 2001
        socket_status(ws, a, 2)
        socket_status(ws, a, 3)
        frame(ws, 10000000, seq=1, epoch=a["authority_epoch"])
        event(stage1, "frame_align")
        event(stage2, "frame_align")
        ws.send_json(
            {
                "type": "director_command",
                "action": "frame_align_reset",
                "payload": {
                    "request_id": "wire1",
                    "connection_id": a["connection_id"],
                    "account_id": a["account_id"],
                    "match_id": a["match_id"],
                    "authority_epoch": a["authority_epoch"],
                    "timeline_version": 0,
                    "target_t_us": 9000000,
                },
            }
        )
        notices = [
            event(sock, "frame_align_reset_result") for sock in (ws, stage1, stage2)
        ]
        assert notices[0] == notices[1] == notices[2]
        assert notices[0]["status"] == "preparing"
        a["authority_epoch"] = notices[0]["authority_epoch"]
        with client.websocket_connect(url + "?align_client=stage") as late:
            assert auth(late)["timeline_version"] == 1
            snapshot = event(late, "state_sync")
            assert snapshot["timeline_version"] == 1
            assert snapshot["reset"]["status"] == "preparing"
            assert snapshot["frame_align"]["t_us"] == 9000000
        socket_status(ws, a, 4, timeline_version=1, progress_t_us=9000000)
        ws.send_json(
            {
                "type": "director_command",
                "action": "frame_align_reset_ack",
                "payload": {
                    "request_id": "wire1",
                    "authority_epoch": a["authority_epoch"],
                    "timeline_version": 1,
                    "outcome": "presented",
                    "presented_t_us": 9000000,
                },
            }
        )
        for sock in (ws, stage1, stage2):
            while event(sock, "frame_align_reset_result")["status"] != "completed":
                pass
