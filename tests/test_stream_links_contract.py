from __future__ import annotations

from dataclasses import replace

import pytest
from pymongo.errors import OperationFailure

from tests.test_stream_links import body, headers, path
from twilightcupbackend.auth import issue_token
from twilightcupbackend.config import settings
from twilightcupbackend.datatypes import Account, AccountType, MatchStatus, now_ts


@pytest.mark.parametrize(
    "changes",
    [
        {"hlsA": "javascript:alert(1)"},
        {"hlsA": "rtmp://live.test/a"},
        {"hlsA": "123"},
        {"hlsA": "//live.test/a"},
        {"hlsA": "https:///a"},
        {"hlsA": "https://host.test:bad/a"},
        {"hlsB": "https://host.test/a\n"},
        {"embedA": "0"},
        {"embedA": "data:text/html,hi"},
        {"embedB": "file:///a"},
        {"hlsA": 42},
        {"embedB": None},
        {"expected_version": True},
        {"expected_version": -1},
        {"expected_version": 2**53},
        {"hlsA": "https://host.test/" + "x" * 4096},
        {"refreshA": 1},
        {"hlsA": "https://host.test/api/youtube/live/file?token=secret&url=a"},
        {"embedA": "https://host.test/api/bilibili/live/stream?token=secret"},
    ],
)
def test_invalid_stream_links_are_coded_and_not_saved(world, changes):  # type: ignore[no-untyped-def]
    client, _, match, tokens = world
    response = client.put(
        path(match), json=body(**changes), headers=headers(tokens["dri"])
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "stream_links_invalid"
    assert "secret" not in response.text
    assert (
        client.get(path(match), headers=headers(tokens["dri"])).json()["version"] == 0
    )


def test_required_fields_and_generic_embed_url(world):  # type: ignore[no-untyped-def]
    client, _, match, tokens = world
    for field in body():
        values = body()
        del values[field]
        assert (
            client.put(
                path(match), json=values, headers=headers(tokens["dri"])
            ).status_code
            == 422
        )
    response = client.put(
        path(match),
        json=body(
            hlsB="https://stream.test/signed?x=1", embedA="https://any.test/embed?a=2"
        ),
        headers=headers(tokens["dri"]),
    )
    assert response.status_code == 200
    assert response.json()["hlsA"] == response.json()["embedB"] == ""


def account(db, role, name):  # type: ignore[no-untyped-def]
    value = Account(
        username=name, password_hash="unused", display_name=name, roles=[role]
    )
    db.accounts.insert(value)
    return value, issue_token(value, settings)


def test_permissions_deleted_account_and_director_reassignment(world):  # type: ignore[no-untyped-def]
    client, db, match, tokens = world
    _, admin_token = account(db, AccountType.ADMIN, "audit")
    new, new_token = account(db, AccountType.DIRECTOR, "other")
    for token in (admin_token, new_token):
        assert (
            client.put(path(match), json=body(), headers=headers(token)).status_code
            == 403
        )
    assert client.get(path(match), headers=headers(admin_token)).status_code == 200
    assert client.get(path(match), headers=headers(new_token)).status_code == 403
    assert (
        client.put(
            path(match), json=body(embedB="99"), headers=headers(tokens["dri"])
        ).status_code
        == 200
    )
    db.matches.update_fields(match.id, {"director_id": new.id})
    assert client.get(path(match), headers=headers(new_token)).json()["embedB"] == "99"
    assert client.get(path(match), headers=headers(tokens["dri"])).status_code == 403
    assert (
        client.put(
            path(match), json=body(1), headers=headers(tokens["dri"])
        ).status_code
        == 403
    )
    db.accounts.delete(new.id)
    assert client.get(path(match), headers=headers(new_token)).status_code == 401
    assert (
        client.get(path(match), headers=headers("expired.invalid.token")).status_code
        == 401
    )
    assert (
        client.get(
            path(match.model_copy(update={"id": "missing"})),
            headers=headers(admin_token),
        ).json()["detail"]["code"]
        == "match_not_found"
    )


@pytest.mark.parametrize(
    "state", [MatchStatus.CREATED, MatchStatus.PAUSED, MatchStatus.ENDED, "archived"]
)
def test_stream_links_lifecycle(world, state):  # type: ignore[no-untyped-def]
    client, db, match, tokens = world
    if state == "archived":
        match.archived_at = now_ts()
    else:
        match.status = state
    db.matches.replace(match)
    response = client.put(
        path(match), json=body(embedA="11"), headers=headers(tokens["dri"])
    )
    assert response.status_code == (
        409 if state in (MatchStatus.ENDED, "archived") else 200
    )
    if response.status_code == 409:
        assert response.json()["detail"]["code"] == "match_read_only"
    assert client.get(path(match), headers=headers(tokens["ref"])).status_code == 200
    db.matches.delete(match.id)
    assert client.get(path(match), headers=headers(tokens["dri"])).status_code == 404


def test_matches_replace_preserves_links_and_clears_only_replaced_player(world):  # type: ignore[no-untyped-def]
    client, db, match, tokens = world
    auth = headers(tokens["dri"])
    saved = client.put(
        path(match),
        json=body(
            hlsA="https://a.test/", hlsB="https://b.test/", embedA="12", embedB="34"
        ),
        headers=auth,
    ).json()
    match.name = "renamed"
    match.status = MatchStatus.PAUSED
    db.matches.replace(match)
    assert client.get(path(match), headers=auth).json() == saved
    other = match.model_copy(deep=True, update={"id": "other-match"})
    db.matches.insert(other)
    assert client.get(path(other), headers=auth).json()["version"] == 0
    new, _ = account(db, AccountType.PLAYER, "replacement")
    _, admin_token = account(db, AccountType.ADMIN, "admin")
    replaced = client.patch(
        f"/admin/matches/{match.id}",
        json={"player_a": new.username},
        headers=headers(admin_token),
    )
    assert replaced.status_code == 200
    links = client.get(path(match), headers=auth).json()
    assert links["version"] == 2
    assert links["hlsA"] == links["embedA"] == ""
    assert links["hlsB"] == saved["hlsB"] and links["embedB"] == "34"


@pytest.mark.parametrize(
    "mutation,code",
    [
        ({"director_id": "reassigned"}, "stream_links_forbidden"),
        ({"status": MatchStatus.ENDED}, "match_read_only"),
        ({"archived_at": now_ts()}, "match_read_only"),
    ],
)
def test_cas_rechecks_assignment_and_lifecycle(world, monkeypatch, mutation, code):  # type: ignore[no-untyped-def]
    client, db, match, tokens = world
    collection = db.matches.collection
    original = collection.find_one_and_update

    def race(*args, **kwargs):
        collection.update_one({"_id": match.id}, {"$set": mutation})
        return original(*args, **kwargs)

    monkeypatch.setattr(collection, "find_one_and_update", race)
    response = client.put(
        path(match), json=body(embedA="55"), headers=headers(tokens["dri"])
    )
    assert response.json()["detail"]["code"] == code
    assert db.matches.get(match.id).stream_links.version == 0


def test_storage_failure_and_identical_save(world, monkeypatch):  # type: ignore[no-untyped-def]
    client, db, match, tokens = world
    auth = headers(tokens["dri"])
    first = client.put(path(match), json=body(embedA="66"), headers=auth).json()
    same = client.put(path(match), json=body(1, embedA="66"), headers=auth)
    assert same.json() == first

    def fail(*args, **kwargs):
        raise OperationFailure("failed")

    monkeypatch.setattr(db.matches.collection, "find_one_and_update", fail)
    response = client.put(path(match), json=body(1), headers=auth)
    assert response.status_code == 503
    assert client.get(path(match), headers=auth).json() == first


def test_old_missing_fields_and_multi_role_authorization(world):  # type: ignore[no-untyped-def]
    client, db, match, tokens = world
    db.matches.collection.update_one(
        {"_id": match.id}, {"$unset": {"stream_links": ""}}
    )
    assert (
        client.get(path(match), headers=headers(tokens["dri"])).json()["version"] == 0
    )
    db.matches.update_fields(match.id, {"director_id": match.referee_id})
    assert client.get(path(match), headers=headers(tokens["ref"])).status_code == 200
    assert (
        client.put(
            path(match), headers=headers(tokens["ref"]), json=body(embedB="9")
        ).status_code
        == 403
    )
    referee = db.accounts.get(match.referee_id)
    referee.roles.append(AccountType.DIRECTOR)
    db.accounts.replace(referee)
    saved = client.put(
        path(match), headers=headers(tokens["ref"]), json=body(embedB="9")
    )
    assert saved.status_code == 200 and saved.json()["version"] == 1
    expired = issue_token(
        referee,
        replace(settings, access_token_expire_seconds=-1),
    )
    assert (
        client.get(path(match), headers=headers(expired)).json()["detail"]["code"]
        == "unauthorized"
    )


def receive(ws, kind):  # type: ignore[no-untyped-def]
    for _ in range(30):
        message = ws.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError(f"Missing {kind}")


def test_ws_rest_notifications_legacy_partial_write_and_reconnect(world):  # type: ignore[no-untyped-def]
    client, _, match, tokens = world
    with (
        client.websocket_connect(
            f"/ws/{tokens['dri']}?align_client=console"
        ) as console,
        client.websocket_connect(f"/ws/{tokens['dri']}?align_client=stage") as stage,
        client.websocket_connect(f"/ws/{tokens['ref']}") as referee,
        client.websocket_connect(f"/ws/{tokens['pa']}") as player,
    ):
        for ws in (console, stage, referee):
            assert receive(ws, "stream_links_update")["payload"]["version"] == 0
        saved = client.put(
            path(match),
            json=body(hlsB="https://b.test/", embedA="123"),
            headers=headers(tokens["dri"]),
        ).json()
        for ws in (console, stage, referee):
            assert receive(ws, "stream_links_update")["payload"] == saved
        console.send_json(
            {
                "type": "director_command",
                "action": "config_update",
                "payload": {"config": {"hlsA": " https://a.test/signed?X=AbC%2f "}},
            }
        )
        for ws in (console, stage, referee):
            payload = receive(ws, "stream_links_update")["payload"]
            assert payload["version"] == 2
            assert payload["hlsB"] == "https://b.test/" and payload["embedA"] == "123"
            assert payload["hlsA"] == "https://a.test/signed?X=AbC%2f"
        stage.send_json(
            {
                "type": "director_command",
                "action": "config_update",
                "payload": {"config": {"hlsA": "https://bad.test/"}},
            }
        )
        assert receive(stage, "error")["code"] == 403
        player.send_json({"type": "chat", "text": "marker"})
        for _ in range(30):
            message = player.receive_json()
            assert message["type"] != "stream_links_update"
            if message.get("text") == "marker":
                break
        else:
            raise AssertionError("No player chat marker")
    with client.websocket_connect(f"/ws/{tokens['dri']}?align_client=stage") as stage:
        assert receive(stage, "stream_links_update")["payload"]["version"] == 2
        sync = receive(stage, "director_cmd")
        assert sync["action"] == "state_sync"
        assert sync["payload"]["config"]["hlsA"] == "https://a.test/signed?X=AbC%2f"
