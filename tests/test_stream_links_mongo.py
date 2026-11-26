from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, local
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pymongo import MongoClient

from twilightcupbackend.config import settings
from twilightcupbackend.controllers import DBController
from twilightcupbackend.stream_links import StreamLinksService


@pytest.mark.skipif(
    not os.getenv("STREAM_LINKS_TEST_MONGO_URI"), reason="Requires disposable MongoDB"
)
@pytest.mark.parametrize("version", [0, 1])
def test_real_mongo_atomic_cas(world, version, monkeypatch):  # type: ignore[no-untyped-def]
    _, source, match, _ = world
    client = MongoClient(
        os.environ["STREAM_LINKS_TEST_MONGO_URI"], serverSelectionTimeoutMS=5000
    )
    db = DBController(
        replace(settings, db_name=f"twc_stream_test_{uuid4().hex}"), client=client
    )
    try:
        for value in source.accounts.find():
            db.accounts.insert(value)
        db.matches.insert(match)
        director = db.accounts.get(match.director_id)
        assert director is not None
        service = StreamLinksService(db)
        if version:
            service.save(match.id, director, {"embedA": "100"}, 0)
        original = db.matches.get
        barrier = Barrier(2)
        thread_state = local()

        def synchronized_read(match_id):
            value = original(match_id)
            if not getattr(thread_state, "read", False):
                thread_state.read = True
                barrier.wait(timeout=5)
            return value

        monkeypatch.setattr(db.matches, "get", synchronized_read)

        def save(room):
            try:
                saved, _ = service.save(match.id, director, {"embedA": room}, version)
                return saved.version
            except HTTPException as exc:
                assert exc.status_code == 409
                detail = cast(dict[str, Any], exc.detail)
                assert isinstance(detail, dict)
                assert detail["code"] == "stream_links_version_conflict"
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, ["101", "102"]))
        assert results.count(version + 1) == 1 and results.count(None) == 1
        stored = original(match.id)
        assert stored is not None and stored.stream_links.version == version + 1
        assert db.matches.count() == 1
    finally:
        client.drop_database(db.settings.db_name)
        client.close()
