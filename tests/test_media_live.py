"""Opt-in real HLS + three WebSocket pages; never print credentials or URLs.

Set AUTHORITY_LIVE_TEST=1 and AUTHORITY_HLS_BEARER, then run:
pytest -q -s tests/test_media_live.py
Use a secret environment store, not a command-line literal, for the bearer.
"""

import os
import time
from dataclasses import replace
from itertools import pairwise

import pytest

from tests.test_director_command import _recv_until

pytestmark = pytest.mark.skipif(
    os.getenv("AUTHORITY_LIVE_TEST") != "1", reason="opt-in real media integration"
)


def test_real_media_produces_shared_clock(world):
    client, _, match, tokens = world
    cm = client.app.state.connection_manager
    cm.settings = replace(
        cm.settings, authority_hls_bearer=os.getenv("AUTHORITY_HLS_BEARER", "")
    )
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

        def anchor(m):
            return m.get("action") == "frame_align"

        started = time.monotonic()
        anchors = []
        # Even a failed media reader publishes waiting anchors, bounding this test
        # to 60 seconds without dumping any underlying URL or exception details.
        for _ in range(150):
            rows = [_recv_until(ws, anchor)["payload"] for ws in (a, b, c)]
            assert rows[0] == rows[1] == rows[2]
            p = rows[0]
            if p["state"] == "playing":
                anchors.append(p)
                safe = min(x["continuous_to_us"] for x in p["coverage"]) - 30_000_000
                assert 0 < p["t_us"] <= safe - 1_000_000
                if len(anchors) >= 12:
                    break
        assert len(anchors) >= 12, "Live streams did not reach continuous coverage"
        assert anchors[-1]["t_us"] > anchors[0]["t_us"]
        assert all(y["seq"] > x["seq"] for x, y in pairwise(anchors))
        session = cm._media_authorities[(match.director_id, match.id)]
        print(
            {
                "three_pages_equal": True,
                "playing_anchors": len(anchors),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "advance_us": anchors[-1]["t_us"] - anchors[0]["t_us"],
                "samples": [o.samples for o in session.observers],
                "sei_samples": [o.sei_samples for o in session.observers],
                "idr_keys": [o.keys for o in session.observers],
            }
        )
        with client.websocket_connect(f"/ws/{tokens['dri']}") as late:
            p = _recv_until(late, lambda m: m.get("action") == "state_sync")["payload"][
                "frame_align"
            ]
            assert (
                p["epoch"] == anchors[-1]["epoch"] and p["t_us"] >= anchors[-1]["t_us"]
            )
