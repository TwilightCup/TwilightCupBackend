"""No browser publisher: coverage produces a safe, advancing server clock."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

from twilightcupbackend.media_authority import Coverage, MediaClock, Sample


def fill(coverage, start=1_000_000_000, seconds=60):
    for i in range(seconds * 10 + 1):
        coverage.append(Sample(start + i * 100_000, i, i % 20 == 0), 10.0)


def test_bootstrap_and_monotonic_clock():
    a, b = Coverage(), Coverage()
    clock = MediaClock()
    assert clock.tick(a, b, 10.0) == (0, True, "waiting_coverage")
    fill(a)
    fill(b)
    t, frozen, _ = clock.tick(a, b, 10.0)
    assert t == 1_027_000_000 and not frozen
    t2, frozen, _ = clock.tick(a, b, 10.4)
    assert t2 == t + 400_000 and not frozen
    t3, frozen, _ = clock.tick(a, b, 16.0)
    assert frozen and t3 == t2


def test_gap_and_restart_never_roll_back_or_cross_safety():
    a, b = Coverage(), Coverage()
    fill(a)
    fill(b)
    clock = MediaClock()
    first = clock.tick(a, b, 10.0)[0]
    a.break_input()
    assert clock.tick(a, b, 10.4)[1]
    fill(a, start=900_000_000)
    assert clock.tick(a, b, 10.8)[0] == first
    assert clock.tick(a, b, 11)[1]
    fill(a, start=1_100_000_000)
    fill(b, start=1_100_000_000)
    later, frozen, _ = clock.tick(a, b, 11.2)
    assert a.to_us is not None and b.to_us is not None
    assert not frozen and first < later <= min(a.to_us, b.to_us) - 30_000_000


def test_sei_alone_is_not_continuous_coverage():
    a = Coverage()
    a.append(Sample(10_000_000, 1, False), 0)
    assert a.to_us is None
    a.append(Sample(10_100_000, 2, True), 0)
    assert a.to_us == 10_100_000
    a.append(Sample(10_300_000, 4, False), 0)
    assert a.to_us is None
    a.append(None, 0)
    assert a.to_us is None


def test_managed_task_bootstraps_all_pages_without_frame_align(world, monkeypatch):
    async def scenario():
        from twilightcupbackend import media_authority as media
        from twilightcupbackend.datatypes import Seat
        from twilightcupbackend.protocol import ClientDirectorCommand
        from twilightcupbackend.stores import Connection

        async def observe(self):
            fill(self.coverage)
            # Live observation uses the same monotonic domain as the service clock.
            self.coverage.updated_at = time.monotonic()
            await asyncio.Event().wait()

        monkeypatch.setattr(media.HlsObserver, "run", observe)
        cm = world[0].app.state.connection_manager
        match = world[2]
        store = cm.registry.get_or_create(match)
        sockets = [AsyncMock() for _ in range(3)]
        conns = [
            Connection(ws, match.director_id, "d", Seat.DIRECTOR, match.id)
            for ws in sockets
        ]
        store.directors.update(conns)
        await cm._dispatch(
            conns[0],
            ClientDirectorCommand(
                action="config_update",
                payload={
                    "config": {
                        "hlsA": "https://bsrserver.org.cn:1936/test/index.m3u8",
                        "hlsB": "https://bsrserver.org.cn:1936/test2/index.m3u8",
                    }
                },
            ),
        )
        service = cm._media_authorities[(match.director_id, match.id)]
        try:
            await asyncio.sleep(0.9)
            frames = [
                [
                    json.loads(c.args[0])["payload"]
                    for c in ws.send_text.call_args_list
                    if json.loads(c.args[0]).get("action") == "frame_align"
                    and json.loads(c.args[0])["payload"]["t_us"] > 0
                ]
                for ws in sockets
            ]
            assert all(len(f) >= 2 for f in frames)
            assert frames[0] == frames[1] == frames[2]
            assert frames[0][-1]["seq"] > frames[0][0]["seq"]
            assert frames[0][-1]["t_us"] > frames[0][0]["t_us"]
            replay = cm._director_state_payload(match.director_id, match.id)[
                "frame_align"
            ]
            assert replay["seq"] == frames[0][-1]["seq"]
            epoch = replay["epoch"]
            await cm._dispatch(
                conns[0],
                ClientDirectorCommand(
                    action="frame_align",
                    payload={"src": "browser", "t_us": 2_000_000_000},
                ),
            )
            assert (
                cm._director_state_payload(match.director_id, match.id)["frame_align"][
                    "epoch"
                ]
                == epoch
            )
        finally:
            await service.close()
            for conn in conns:
                store.directors.discard(conn)

    world[0].portal.call(scenario)


def test_late_join_restart_source_switch_and_resource_release(world, monkeypatch):
    from twilightcupbackend import media_authority as media
    from twilightcupbackend.connection_manager import ConnectionManager
    from twilightcupbackend.datatypes import Seat
    from twilightcupbackend.stores import Connection, MatchRegistry

    released = []

    async def observe(self):
        try:
            fill(self.coverage)
            self.coverage.updated_at = time.monotonic()
            self.reason = "observing"
            await asyncio.Event().wait()
        finally:
            released.append(True)
            await self.fetcher.close()

    monkeypatch.setattr(media.HlsObserver, "run", observe)
    client, db, match, _ = world

    async def scenario():
        cm = client.app.state.connection_manager
        store = cm.registry.get_or_create(match)
        conn = Connection(AsyncMock(), match.director_id, "d", Seat.DIRECTOR, match.id)
        store.directors.add(conn)
        from twilightcupbackend.connection_manager import _DirectorState

        st = _DirectorState(
            config={
                "hlsA": "https://bsrserver.org.cn:1936/test/index.m3u8",
                "hlsB": "https://bsrserver.org.cn:1936/test2/index.m3u8",
            }
        )
        key = (conn.account_id, match.id)
        cm._director_state[key] = st
        async with st.align_lock:
            await cm._ensure_media_authority(*key, st)
        await asyncio.sleep(0.5)
        old_epoch, old_t = st.align_epoch, st.frame_align_t_us
        assert old_t is not None
        first = cm._media_authorities[key]
        st.scene = "soon"  # visual scene must not change shared timeline scope
        async with st.align_lock:
            await cm._ensure_media_authority(*key, st)
        assert cm._media_authorities[key] is first
        assert st.align_anchor is not None
        assert st.align_anchor["scene"] == "shared-playback"
        st.config["hlsA"] = "https://bsrserver.org.cn:1936/changed/index.m3u8"
        async with st.align_lock:
            await cm._ensure_media_authority(*key, st)
        assert first.closed and first.task.done()
        assert st.frame_align_t_us is not None
        assert st.align_epoch > old_epoch and st.frame_align_t_us >= old_t
        assert len(released) == 2
        await asyncio.sleep(0.1)
        epoch_before_restart, t_before_restart = st.align_epoch, st.frame_align_t_us
        assert t_before_restart is not None
        store.directors.clear()
        await cm.close_media_authorities()
        # Fresh manager with the same durable DB: no page sends T or even config.
        # Durable fencing must survive even a server wall-clock rollback.
        from twilightcupbackend import connection_manager as cm_module

        monkeypatch.setattr(cm_module, "_now_ms", lambda: 1)
        restarted = ConnectionManager(db, MatchRegistry(), cm.settings)
        fresh_store = restarted.registry.get_or_create(match)
        fresh_store.directors.add(conn)
        try:
            await restarted._restore_media_config(*key)
            restored = restarted._director_state[key]
            assert restored.frame_align_t_us is not None
            assert restored.align_anchor is not None
            assert restored.align_epoch > epoch_before_restart
            assert restored.frame_align_t_us >= t_before_restart
            assert restored.config["hlsA"] == st.config["hlsA"]
            assert restored.align_anchor["epoch"] == restored.align_epoch
            assert restored.align_anchor["src"] == restored.align_authority_src
            await asyncio.sleep(0.5)
            assert restored.align_anchor["t_us"] >= t_before_restart
            fresh_store.directors.clear()
            async with restored.align_lock:
                await restarted._ensure_media_authority(*key, restored)
            assert not restarted._media_authorities
        finally:
            await restarted.close_media_authorities()

    client.portal.call(scenario)
    assert len(released) == 6


def test_actual_three_websockets_bootstrap_and_late_join(world, monkeypatch):
    from tests.test_director_command import _recv_until
    from twilightcupbackend import media_authority as media

    async def observe(self):
        fill(self.coverage)
        self.coverage.updated_at = time.monotonic()
        self.reason = "observing"
        await asyncio.Event().wait()

    monkeypatch.setattr(media.HlsObserver, "run", observe)
    client, _, _, tokens = world
    with (
        client.websocket_connect(f"/ws/{tokens['dri']}") as a,
        client.websocket_connect(f"/ws/{tokens['dri']}") as b,
        client.websocket_connect(f"/ws/{tokens['dri']}") as c,
    ):
        a.send_json(
            {
                "type": "director_command",
                "action": "config_update",
                "payload": {
                    "config": {
                        "hlsA": "https://bsrserver.org.cn:1936/test/index.m3u8",
                        "hlsB": "https://bsrserver.org.cn:1936/test2/index.m3u8",
                    }
                },
            }
        )

        def playing(m):
            return (
                m.get("action") == "frame_align" and m["payload"]["state"] == "playing"
            )

        first = [_recv_until(ws, playing)["payload"] for ws in (a, b, c)]
        assert first[0] == first[1] == first[2]
        # Merely receiving creates no browser messages; the pump still advances.
        second = [_recv_until(ws, playing)["payload"] for ws in (a, b, c)]
        assert second[0] == second[1] == second[2]
        assert (
            second[0]["seq"] > first[0]["seq"] and second[0]["t_us"] > first[0]["t_us"]
        )
        with client.websocket_connect(f"/ws/{tokens['dri']}") as late:
            replay = _recv_until(late, lambda m: m.get("action") == "state_sync")[
                "payload"
            ]["frame_align"]
            assert replay["epoch"] == second[0]["epoch"]
            assert replay["seq"] >= second[0]["seq"] and not replay["frozen"]


def test_managed_scope_isolation_and_stop(world, monkeypatch):
    from twilightcupbackend import media_authority as media
    from twilightcupbackend.connection_manager import _DirectorState
    from twilightcupbackend.datatypes import Seat
    from twilightcupbackend.stores import Connection

    async def observe(self):
        fill(self.coverage)
        self.coverage.updated_at = time.monotonic()
        await asyncio.Event().wait()

    monkeypatch.setattr(media.HlsObserver, "run", observe)
    client, _, match, _ = world

    async def scenario():
        cm = client.app.state.connection_manager
        scopes = [
            (match.director_id, match.id),
            ("other", match.id),
            (match.director_id, "other-match"),
        ]
        sockets = []
        for account, match_id in scopes:
            scoped_match = match.model_copy(update={"id": match_id})
            store = cm.registry.get_or_create(scoped_match)
            ws = AsyncMock()
            store.directors.add(Connection(ws, account, "d", Seat.DIRECTOR, match_id))
            sockets.append(ws)
            st = _DirectorState(
                config={
                    "hlsA": "https://bsrserver.org.cn:1936/test/index.m3u8",
                    "hlsB": "https://bsrserver.org.cn:1936/test2/index.m3u8",
                }
            )
            cm._director_state[(account, match_id)] = st
            async with st.align_lock:
                await cm._ensure_media_authority(account, match_id, st)
        try:
            await asyncio.sleep(0.5)
            assert len(cm._media_authorities) == 3
            for ws, (account, match_id) in zip(sockets, scopes, strict=True):
                rows = [
                    json.loads(c.args[0])["payload"]
                    for c in ws.send_text.call_args_list
                    if json.loads(c.args[0]).get("action") == "frame_align"
                ]
                assert rows and all(
                    p["account_id"] == account and p["match_id"] == match_id
                    for p in rows
                )
        finally:
            for _, match_id in scopes:
                cm.registry.get(match_id).directors.clear()
            await cm.close_media_authorities()

    client.portal.call(scenario)
