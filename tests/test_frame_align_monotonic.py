"""Wall-clock corrections cannot alter frame elapsed time or anchor age."""

import pytest

from tests.test_frame_align_authority import publish, scope, snapshot  # noqa: F401
from twilightcupbackend import connection_manager as module


@pytest.fixture
def clocks(scope, monkeypatch):  # noqa: F811
    cm, pages, store = scope
    wall = [1760000000000]
    mono = [module._align_now_ms()]
    monkeypatch.setattr(module, "_now_ms", lambda: wall[0])
    monkeypatch.setattr(module, "_align_now_ms", lambda: mono[0])
    return cm, pages, store, wall, mono


@pytest.mark.parametrize("jump", [3600000, -3600000])
async def test_publisher_silence_uses_only_monotonic(clocks, jump):
    cm, (owner, *_), _, wall, mono = clocks
    assert publish(cm, owner, t_us=1760000000000000)[0]
    st = cm._director_state[(owner.account_id, owner.match_id)]
    epoch = st.align_epoch
    wall[0] += jump
    await cm._expire_align(owner.account_id, owner.match_id)
    assert not st.align_anchor["frozen"]
    assert st.align_owner is owner and st.align_epoch == epoch
    # The console still executes and renews its lease, but stops sending frames.
    # Advancing only monotonic must expire frame silence even after wall rollback.
    mono[0] += 5000
    owner.align_lease.received_ms = mono[0]
    await cm._expire_align(owner.account_id, owner.match_id)
    assert not st.align_anchor["frozen"]
    mono[0] += 1
    await cm._expire_align(owner.account_id, owner.match_id)
    assert st.align_anchor["frozen"] and st.align_anchor["reason"] == "publisher_silent"
    assert st.align_anchor["t_us"] == 1760000000000000
    assert st.align_owner is owner and st.align_epoch == epoch


@pytest.mark.parametrize("jump", [3600000, -3600000])
def test_snapshot_age_and_utc_fields_use_separate_clocks(clocks, jump):
    cm, (owner, *_), _, wall, mono = clocks
    effective = wall[0]
    assert publish(cm, owner, t_us=1760000000000000, anchor_age_ms=99999999)[0]
    assert snapshot(cm, owner)["anchor_age_ms"] == 0
    mono[0] += 700
    before = snapshot(cm, owner)
    wall[0] += jump
    after = snapshot(cm, owner)
    assert before["anchor_age_ms"] == after["anchor_age_ms"] == 700
    assert after["effective_at_ms"] == effective
    assert after["server_now_ms"] == after["server_time_ms"] == wall[0]
    assert before["seq"] == after["seq"] and before["t_us"] == after["t_us"]
    # Reading/replaying snapshots must not reset the age.
    mono[0] += 100
    assert snapshot(cm, owner)["anchor_age_ms"] == 800


async def test_new_anchor_freeze_and_takeover_start_new_age(clocks):
    cm, (owner, follower, _), store, _wall, mono = clocks
    assert publish(cm, owner, t_us=1760000000000000)[0]
    mono[0] += 300
    assert publish(cm, owner, t_us=1760000000001000)[0]
    assert snapshot(cm, owner)["anchor_age_ms"] == 0
    mono[0] += 750
    st = cm._director_state[(owner.account_id, owner.match_id)]
    cm._freeze_align(st)
    assert snapshot(cm, owner)["anchor_age_ms"] == 0
    mono[0] += 300
    cm._remove_connection(store, owner)
    await cm._flush_align_notifications()
    assert st.align_owner is follower and snapshot(cm, follower)["anchor_age_ms"] == 0
    mono[0] += 250
    assert snapshot(cm, follower)["anchor_age_ms"] == 250


@pytest.mark.parametrize("jump", [3600000, -3600000])
async def test_reset_deadline_remains_monotonic(clocks, jump):
    from tests.test_frame_align_reset import request

    cm, (owner, *_), _, wall, mono = clocks
    target = (wall[0] - 30000) * 1000
    assert publish(cm, owner, t_us=wall[0] * 1000)[0]
    await request(cm, owner, target)
    st = cm._director_state[(owner.account_id, owner.match_id)]
    deadline = st.reset_deadline_ms
    wall[0] += jump
    await cm._expire_align(owner.account_id, owner.match_id)
    assert st.reset_state["status"] == "preparing"
    assert st.reset_deadline_ms == deadline
    for elapsed in (4000, 4000, 2000):
        mono[0] += elapsed
        owner.align_lease.received_ms = mono[0]
        await cm._expire_align(owner.account_id, owner.match_id)
        assert st.reset_state["status"] == "preparing"
    mono[0] += 1
    await cm._expire_align(owner.account_id, owner.match_id)
    assert st.reset_state["code"] == "PREPARE_TIMEOUT"
    assert st.frame_align_t_us == target


@pytest.mark.parametrize("outcome", ["presented", "failed"])
async def test_reset_start_and_finish_rebase_snapshot_age(clocks, outcome):
    from tests.test_frame_align_lease import report
    from tests.test_frame_align_reset import ack, request

    cm, (owner, *_), _, wall, mono = clocks
    target = (wall[0] - 30000) * 1000
    assert publish(cm, owner, t_us=wall[0] * 1000)[0]
    mono[0] += 100
    await request(cm, owner, target)
    assert snapshot(cm, owner)["anchor_age_ms"] == 0
    mono[0] += 250
    assert snapshot(cm, owner)["anchor_age_ms"] == 250
    if outcome == "presented":
        await report(cm, owner, 1, timeline_version=1, progress_t_us=target)
        await ack(cm, owner, presented_t_us=target)
    else:
        await ack(cm, owner, outcome="failed", reason="prepare_failed")
    assert snapshot(cm, owner)["anchor_age_ms"] == 0
    mono[0] += 100
    assert snapshot(cm, owner)["anchor_age_ms"] == 100
    if outcome == "presented":
        # Confirmation counts as a real publisher update, in monotonic time too.
        wall[0] += 3600000
        await cm._expire_align(owner.account_id, owner.match_id)
        assert not snapshot(cm, owner)["frozen"]


async def test_media_wait_keepalive_has_fresh_age_without_advancing_t(clocks):
    from tests.test_frame_align_lease import report

    cm, (owner, *_), _, wall, mono = clocks
    t = wall[0] * 1000
    assert publish(cm, owner, t_us=t)[0]
    await report(
        cm,
        owner,
        1,
        state="media_wait",
        media_ready=False,
        decode_ready=False,
        active_sides=[],
        waiting_sides=["A", "B"],
    )
    before = snapshot(cm, owner)
    mono[0] += 250
    wall[0] -= 3600000
    await cm._expire_align(owner.account_id, owner.match_id)
    assert snapshot(cm, owner)["anchor_age_ms"] == 250
    mono[0] += 500
    await cm._expire_align(owner.account_id, owner.match_id)
    after = snapshot(cm, owner)
    assert after["anchor_age_ms"] == 0 and after["seq"] > before["seq"]
    assert after["t_us"] == before["t_us"] == t and after["frozen"]
