from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from twilightcupbackend.main import create_app


def headers(token):  # type: ignore[no-untyped-def]
    return {"Authorization": f"Bearer {token}"}


def path(match):  # type: ignore[no-untyped-def]
    return f"/me/matches/{match.id}/stream-links"


def body(version=0, **values):  # type: ignore[no-untyped-def]
    return {
        "expected_version": version,
        "hlsA": "",
        "hlsB": "",
        "embedA": "",
        "embedB": "",
    } | values


def test_persistent_stream_links_and_version_conflict(world):  # type: ignore[no-untyped-def]
    client, db, match, tokens = world
    url = path(match)
    initial = client.get(url, headers=headers(tokens["dri"]))
    assert initial.status_code == 200
    assert initial.json() == {
        "match_id": match.id,
        "version": 0,
        "hlsA": "",
        "hlsB": "",
        "embedA": "",
        "embedB": "",
        "updated_at_ms": None,
        "updated_by": None,
    }
    signed = "https://example.test/live?sign=AbC%2Fdef&expires=99"
    saved = client.put(
        url,
        headers=headers(tokens["dri"]),
        json=body(hlsA=f" {signed} ", embedB="123456"),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["hlsA"] == signed
    assert saved.json()["version"] == 1
    assert isinstance(saved.json()["updated_at_ms"], int)
    assert saved.json()["updated_by"] == match.director_id
    assert client.get(url, headers=headers(tokens["ref"])).json() == saved.json()
    assert (
        client.put(url, headers=headers(tokens["dri"]), json=body()).status_code == 409
    )
    with TestClient(create_app(db=db)) as restarted:
        assert restarted.get(url, headers=headers(tokens["dri"])).json() == saved.json()
    assert (
        client.put(url, headers=headers(tokens["dri"]), json=body(1)).json()["version"]
        == 2
    )


@pytest.mark.parametrize("role", ["pa", "pb", "ref"])
def test_only_designated_director_can_write(world, role):  # type: ignore[no-untyped-def]
    client, _, match, tokens = world
    result = client.put(path(match), headers=headers(tokens[role]), json=body())
    assert result.status_code == 403
    assert result.json()["detail"]["code"] == "stream_links_forbidden"


def test_player_cannot_read_and_missing_auth_is_coded(world):  # type: ignore[no-untyped-def]
    client, _, match, tokens = world
    assert client.get(path(match), headers=headers(tokens["pa"])).status_code == 403
    result = client.get(path(match))
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "unauthorized"
