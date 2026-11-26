from __future__ import annotations

import json
from typing import Any, cast

import pytest
from fastapi import WebSocket
from pymongo.errors import OperationFailure

from tests.test_stream_links import body, headers, path
from tests.test_stream_links_contract import account
from twilightcupbackend.datatypes import AccountType, Seat
from twilightcupbackend.stores import Connection


class Socket:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.failed = False

    async def send_text(self, value: str) -> None:
        if self.failed:
            raise RuntimeError("Disconnected")
        self.messages.append(json.loads(value))


async def test_reassignment_between_snapshot_and_compatibility_send(world, monkeypatch):  # type: ignore[no-untyped-def]
    _, db, match, _ = world
    cm, _, pages = connections(world)
    owner, sock = pages[0]
    original = sock.send_text

    async def send_and_reassign(value):
        await original(value)
        db.matches.update_fields(match.id, {"director_id": "new-director"})

    monkeypatch.setattr(sock, "send_text", send_and_reassign)
    await cm._send_stream_links_snapshot(owner, compatibility=True)
    assert [message["type"] for message in sock.messages] == ["stream_links_update"]


def connections(world):  # type: ignore[no-untyped-def]
    client, _db, match, _tokens = world
    cm = client.app.state.connection_manager
    store = cm.registry.get_or_create(match)
    pages = []
    for seat, purpose, account_id in [
        (Seat.DIRECTOR, "console", match.director_id),
        (Seat.DIRECTOR, "stage", match.director_id),
        (Seat.REFEREE, None, match.referee_id),
        (Seat.PLAYER_A, None, match.player_a_id),
    ]:
        sock = Socket()
        conn = Connection(
            websocket=cast(WebSocket, sock),
            account_id=account_id,
            display_name="test",
            seat=seat,
            match_id=match.id,
            align_client=purpose,
            auth_sent=True,
        )
        if seat == Seat.DIRECTOR:
            cm._add_director(store, conn)
        else:
            store.connections[seat] = conn
        pages.append((conn, sock))
    return cm, store, pages


async def config(cm, conn, values):  # type: ignore[no-untyped-def]
    await cm.handle(
        conn,
        json.dumps(
            {
                "type": "director_command",
                "action": "config_update",
                "payload": {"config": values},
            }
        ),
    )


@pytest.mark.parametrize("purpose", ["stage", None])
async def test_receiver_link_write_rejected_but_other_config_preserved(world, purpose):  # type: ignore[no-untyped-def]
    _, db, match, _ = world
    cm, _, pages = connections(world)
    receiver, sock = pages[1]
    receiver.align_client = purpose
    await config(cm, receiver, {"hlsA": "https://wrong.test/", "delay": 100})
    assert sock.messages[0] == {
        "type": "error",
        "code": 403,
        "msg": "stream_links_forbidden",
    }
    assert db.matches.get(match.id).stream_links.version == 0
    state = cm._director_state[(receiver.account_id, match.id)]
    assert state.config == {"delay": 100}
    assert (
        cm._director_state_payload(receiver.account_id, match.id)["config"]["hlsA"]
        == ""
    )


async def test_reassigned_old_ws_cannot_save_or_receive_links(world):  # type: ignore[no-untyped-def]
    client, db, match, _ = world
    cm, store, pages = connections(world)
    old, old_sock = pages[0]
    new, token = account(db, AccountType.DIRECTOR, "replacement-director")
    db.matches.update_fields(match.id, {"director_id": new.id})
    fresh_sock = Socket()
    fresh = Connection(
        websocket=cast(WebSocket, fresh_sock),
        account_id=new.id,
        display_name="new",
        seat=Seat.DIRECTOR,
        match_id=match.id,
        align_client="stage",
        auth_sent=True,
    )
    cm._add_director(store, fresh)
    await config(cm, old, {"embedA": "123"})
    assert old_sock.messages[-1]["code"] == 403
    old_sock.messages.clear()
    saved = client.put(path(match), json=body(embedA="222"), headers=headers(token))
    assert saved.status_code == 200
    assert old_sock.messages == pages[1][1].messages == pages[3][1].messages == []
    assert fresh_sock.messages[0]["payload"] == saved.json()
    assert pages[2][1].messages[0]["payload"] == saved.json()


async def test_storage_failure_does_not_relay_or_modify_memory(world, monkeypatch):  # type: ignore[no-untyped-def]
    _, db, match, _ = world
    cm, _, pages = connections(world)
    owner, sock = pages[0]
    state = cm._director_state[(owner.account_id, match.id)]
    state.config = {"delay": 5}

    def fail(*args, **kwargs):
        raise OperationFailure("failed")

    monkeypatch.setattr(db.matches.collection, "find_one_and_update", fail)
    await config(cm, owner, {"hlsB": "https://bad.test/", "delay": 9})
    assert sock.messages == [
        {"type": "error", "code": 503, "msg": "stream_links_unavailable"}
    ]
    assert state.config == {"delay": 5}
    assert all(not socket.messages for _, socket in pages[1:])


async def test_notification_failure_does_not_undo_save_and_clear_notifies(world):  # type: ignore[no-untyped-def]
    client, db, match, tokens = world
    _, _, pages = connections(world)
    pages[1][1].failed = True
    auth = headers(tokens["dri"])
    saved = client.put(
        path(match), json=body(hlsA="https://a.test/", embedA="22"), headers=auth
    )
    assert saved.status_code == 200
    assert client.get(path(match), headers=auth).json() == saved.json()
    assert pages[0][1].messages[0]["payload"] == saved.json()
    assert pages[2][1].messages[0]["payload"] == saved.json()
    count = len(pages[0][1].messages)
    assert (
        client.put(
            path(match), json=body(1, hlsA="https://a.test/", embedA="22"), headers=auth
        ).json()
        == saved.json()
    )
    assert len(pages[0][1].messages) == count
    _, admin_token = account(db, AccountType.ADMIN, "admin-replace")
    player, _ = account(db, AccountType.PLAYER, "new-player")
    assert (
        client.patch(
            f"/admin/matches/{match.id}",
            json={"player_a": player.username},
            headers=headers(admin_token),
        ).status_code
        == 200
    )
    update = next(
        message
        for message in reversed(pages[2][1].messages)
        if message["type"] == "stream_links_update"
    )
    assert update["payload"]["version"] == 2 and update["payload"]["hlsA"] == ""
    assert pages[3][1].messages == []


async def test_canonical_links_override_old_memory_and_legacy_partial_merges(world):  # type: ignore[no-untyped-def]
    _, db, match, _ = world
    cm, _, pages = connections(world)
    owner, sock = pages[0]
    state = cm._director_state[(owner.account_id, match.id)]
    state.config = {"hlsA": "old", "embedB": "old", "theme": "dark"}
    await config(cm, owner, {"hlsA": "https://a.test/", "embedB": "123"})
    await config(cm, owner, {"hlsA": ""})
    links = db.matches.get(match.id).stream_links
    assert links.version == 2 and links.embedB == "123" and links.hlsA == ""
    replay = cm._director_state_payload(owner.account_id, match.id)["config"]
    assert replay == {
        "hlsA": "",
        "hlsB": "",
        "embedA": "",
        "embedB": "123",
        "theme": "dark",
    }
    snapshots = [
        message["payload"]
        for message in sock.messages
        if message["type"] == "stream_links_update"
    ]
    assert [snapshot["version"] for snapshot in snapshots] == [1, 2]
    assert all(
        set(snapshot)
        == {
            "match_id",
            "version",
            "hlsA",
            "hlsB",
            "embedA",
            "embedB",
            "updated_at_ms",
            "updated_by",
        }
        for snapshot in snapshots
    )
