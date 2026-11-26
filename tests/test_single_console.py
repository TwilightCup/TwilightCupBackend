"""Connection lifecycle, not decoder readiness, grants publisher ownership."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from tests.test_frame_align_authority import scope  # noqa: F401
from tests.test_frame_align_election import auth, event, frame
from twilightcupbackend.connection_manager import ConnectionManager
from twilightcupbackend.stores import MatchRegistry


def rows(ws):
    return [json.loads(call.args[0]) for call in ws.send_text.call_args_list]


@pytest.mark.parametrize("exclusive", ["", "&exclusive=1"])
def test_latest_console_replaces_only_console_and_immediately_publishes(
    world, exclusive
):
    client, _, _, tokens = world
    url = f"/ws/{tokens['dri']}?align_client="
    with (
        client.websocket_connect(url + "stage") as stage,
        client.websocket_connect(url + "stage") as stage2,
        client.websocket_connect(url + "console") as first,
    ):
        auth(stage)
        auth(stage2)
        a = auth(first)
        assert a["align_role"] == "publisher"
        assert a["align_authority_src"] == a["connection_id"]
        assert a["align_lease_required"] is False
        initial = event(first, "state_sync")
        assert initial["align_authority_src"] == a["connection_id"]
        assert "frame_align" not in initial
        frame(first, 10000000, epoch=a["authority_epoch"], seq=1)
        assert event(stage, "frame_align")["t_us"] == 10000000
        event(stage2, "frame_align")
        with client.websocket_connect(url + "console" + exclusive) as second:
            b = auth(second)
            assert b["align_role"] == "publisher"
            assert b["authority_epoch"] > a["authority_epoch"]
            replay = event(second, "state_sync")
            assert replay["connection_id"] == b["connection_id"]
            assert replay["align_authority_src"] == b["connection_id"]
            assert replay["frame_align"]["epoch"] == b["authority_epoch"]
            assert replay["frame_align"]["src"] == b["connection_id"]
            # The old console alone is displaced. The stages stay usable.
            while first.receive_json().get("type") != "displaced":
                pass
            assert first.receive()["code"] == 4001
            for receiver in (stage, stage2):
                anchor = event(receiver, "frame_align")
                assert anchor["src"] == b["connection_id"]
                assert anchor["frozen"] and anchor["t_us"] == 10000000
            frame(second, 11000000, epoch=b["authority_epoch"], seq=1)
            for receiver in (stage, stage2):
                assert event(receiver, "frame_align")["t_us"] == 11000000
        # Closing the only console never revives the displaced one.
        while (notice := event(stage, "align_authority"))["src"] is not None:
            pass
        assert notice["role"] == "follower" and notice["lease_required"] is False
        assert event(stage, "frame_align")["src"] is None
        with client.websocket_connect(url + "stage&exclusive=1") as late:
            assert auth(late)["align_authority_src"] is None
            assert event(late, "state_sync")["align_authority_src"] is None


@pytest.mark.parametrize("query", ["?align_client=stage&exclusive=1", "?exclusive=1"])
def test_exclusive_receiver_cannot_displace_console(world, query):
    client, _, _, tokens = world
    url = f"/ws/{tokens['dri']}"
    with client.websocket_connect(url + "?align_client=console") as console:
        a = auth(console)
        with client.websocket_connect(url + query) as stage:
            b = auth(stage)
            assert b["align_role"] == "follower"
            assert b["align_authority_src"] == a["connection_id"]
            frame(console, 10000000, epoch=a["authority_epoch"], seq=1)
            assert event(stage, "frame_align")["src"] == a["connection_id"]


async def test_concurrent_registration_and_late_old_commands(world):
    client, db, match, tokens = world
    cm = ConnectionManager(db, MatchRegistry(), client.app.state.settings)
    cm.match_engine = client.app.state.connection_manager.match_engine
    first_ws, second_ws = AsyncMock(), AsyncMock()
    auth_in_flight, release_auth = asyncio.Event(), asyncio.Event()

    async def slow_auth(raw):
        if json.loads(raw)["type"] == "auth_ok":
            auth_in_flight.set()
            await release_auth.wait()

    first_ws.send_text.side_effect = slow_auth
    task = asyncio.create_task(
        cm.connect(first_ws, tokens["dri"], align_client="console")
    )
    await asyncio.wait_for(auth_in_flight.wait(), 2)
    store = cm.registry.get(match.id)
    assert store is not None
    first = next(iter(store.directors))
    second_task = asyncio.create_task(
        cm.connect(second_ws, tokens["dri"], align_client="console")
    )
    await asyncio.sleep(0)
    release_auth.set()
    first_result, second = await asyncio.wait_for(asyncio.gather(task, second_task), 2)
    # A connect superseded before its final snapshot may return None to the endpoint.
    assert first_result is None or first_result is first
    assert second is not None
    st = cm._director_state[(first.account_id, match.id)]
    store = cm.registry.get(match.id)
    assert store is not None
    assert st.align_owner is second
    assert not store.has_connection(first) and store.has_connection(second)
    assert (
        next(r for r in rows(first_ws) if r["type"] == "auth_ok")["align_role"]
        == "publisher"
    )
    before = (st.align_epoch, st.timeline_version, st.scene, st.align_anchor)
    for action in (
        "frame_align",
        "frame_align_status",
        "frame_align_reset",
        "frame_align_reset_ack",
        "switch_scene",
    ):
        await cm.handle(
            first,
            json.dumps(
                {
                    "type": "director_command",
                    "action": action,
                    "payload": {"scene": "stale", "t_us": 123},
                }
            ),
        )
    await cm.disconnect(first)
    await cm._flush_align_notifications()
    assert st.align_owner is second
    assert before == (st.align_epoch, st.timeline_version, st.scene, st.align_anchor)
    await cm.disconnect(second)
    await cm._flush_align_notifications()
    assert st.align_owner is None


@pytest.mark.parametrize(
    "action",
    [
        "switch_scene",
        "frame_align",
        "frame_align_status",
        "frame_align_reset",
        "frame_align_reset_ack",
    ],
)
async def test_displaced_command_already_waiting_for_lock_is_ignored(
    scope,  # noqa: F811
    world,
    action,
):
    cm, (old, *_), store = scope
    _, _, _, tokens = world
    st = cm._director_state[(old.account_id, old.match_id)]
    await st.align_lock.acquire()
    replacement = asyncio.create_task(
        cm.connect(AsyncMock(), tokens["dri"], align_client="console")
    )
    await asyncio.sleep(0)  # Registration queues ahead of the old command.
    command = asyncio.create_task(
        cm.handle(
            old,
            json.dumps(
                {
                    "type": "director_command",
                    "action": action,
                    "payload": {"scene": "old-command", "t_us": 10000000},
                }
            ),
        )
    )
    await asyncio.sleep(0)
    st.align_lock.release()
    new, _ = await asyncio.wait_for(asyncio.gather(replacement, command), 2)
    assert new is not None and st.align_owner is new
    assert not store.has_connection(old)
    assert st.scene is None and st.align_anchor is None and st.reset_state is None
    assert old.align_lease.status is None
    await cm.disconnect(old)
    assert st.align_owner is new


@pytest.mark.parametrize("fail_at", ["auth_ok", "ready_state", "state_sync"])
async def test_failed_initialization_does_not_resurrect_old_console(
    scope,  # noqa: F811
    world,
    fail_at,
):
    cm, (old, *_), store = scope
    _, _, _, tokens = world
    ws = AsyncMock()

    async def fail_message(raw):
        message = json.loads(raw)
        if message["type"] == fail_at or message.get("action") == fail_at:
            raise RuntimeError("initialization connection lost")

    ws.send_text.side_effect = fail_message
    with pytest.raises(RuntimeError, match="initialization connection lost"):
        await cm.connect(ws, tokens["dri"], align_client="console")
    await cm._flush_align_notifications()
    st = cm._director_state[(old.account_id, old.match_id)]
    assert st.align_owner is None and not store.has_connection(old)
    assert not any(c.align_client == "console" for c in store.directors)
    assert any(r["type"] == "displaced" for r in rows(old.websocket))


def test_takeover_terminates_old_reset_and_new_owner_can_reset_without_status(
    world, monkeypatch
):
    from twilightcupbackend import connection_manager as module

    client, _, _, tokens = world
    monkeypatch.setattr(module, "_now_ms", lambda: 40000)
    url = f"/ws/{tokens['dri']}?align_client="

    def request(ws, info, request_id, target):
        ws.send_json(
            {
                "type": "director_command",
                "action": "frame_align_reset",
                "payload": {
                    "request_id": request_id,
                    "connection_id": info["connection_id"],
                    "account_id": info["account_id"],
                    "match_id": info["match_id"],
                    "authority_epoch": info["authority_epoch"],
                    "timeline_version": info["timeline_version"],
                    "target_t_us": target,
                },
            }
        )

    with (
        client.websocket_connect(url + "stage") as stage,
        client.websocket_connect(url + "console") as first,
    ):
        auth(stage)
        a = auth(first)
        request(first, a, "old-reset", 9000000)
        preparing = event(first, "frame_align_reset_result")
        assert preparing["status"] == "preparing"
        event(stage, "frame_align_reset_result")
        with client.websocket_connect(url + "console") as second:
            b = auth(second)
            assert b["align_role"] == "publisher" and b["timeline_version"] == 1
            assert b["authority_epoch"] > preparing["authority_epoch"]
            replay = event(second, "state_sync")
            assert replay["authority_epoch"] == b["authority_epoch"]
            assert replay["frame_align"]["src"] == b["connection_id"]
            assert replay["frame_align"]["epoch"] == b["authority_epoch"]
            assert replay["reset"]["code"] == "OWNER_LOST"
            lost = event(stage, "frame_align_reset_result")
            assert lost["status"] == "failed" and lost["code"] == "OWNER_LOST"
            request(second, b, "new-reset", 8000000)
            accepted = event(second, "frame_align_reset_result")
            assert accepted["status"] == "preparing"
            second.send_json(
                {
                    "type": "director_command",
                    "action": "frame_align_reset_ack",
                    "payload": {
                        "request_id": "new-reset",
                        "authority_epoch": accepted["authority_epoch"],
                        "timeline_version": accepted["timeline_version"],
                        "outcome": "presented",
                        "presented_t_us": 8000000,
                    },
                }
            )
            assert event(second, "frame_align_reset_result")["status"] == "completed"
