"""Shared WebSocket helpers for authority election integration tests."""

from tests.test_director_command import _recv_until


def auth(ws):
    return _recv_until(ws, lambda m: m.get("type") == "auth_ok")


def frame(ws, t, **extra):
    ws.send_json(
        {
            "type": "director_command",
            "action": "frame_align",
            "payload": {"src": "untrusted-page-src", "t_us": t, **extra},
        }
    )


def event(ws, action):
    return _recv_until(ws, lambda m: m.get("action") == action)["payload"]
