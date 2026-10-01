from __future__ import annotations

import time
import unicodedata
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from .auth import get_current_account
from .datatypes import Account, AccountType, Match, MatchStatus, StreamLinks

if TYPE_CHECKING:
    from .controllers import DBController

LINK_FIELDS = frozenset({"hlsA", "hlsB", "embedA", "embedB"})


def stream_error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def normalize_link(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Invalid stream link")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError("Invalid stream link")
    value = value.strip()
    if not value:
        return ""
    if (
        field.startswith("embed")
        and value.isascii()
        and value.isdigit()
        and int(value) > 0
    ):
        return value
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Invalid stream link")
        _ = parts.port
        if any(char.isspace() for char in value):
            raise ValueError("Invalid stream link")
        if ("/bilibili/live/" in parts.path or "/youtube/live/" in parts.path) and any(
            key.lower() == "token" for key, _ in parse_qsl(parts.query)
        ):
            raise ValueError("Playback proxy links must not be saved")
    except ValueError:
        raise ValueError("Invalid stream link") from None
    return value


class StreamLinksPut(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=0, le=2**53 - 1)
    hlsA: str = Field(max_length=4096)
    hlsB: str = Field(max_length=4096)
    embedA: str = Field(max_length=4096)
    embedB: str = Field(max_length=4096)

    @field_validator("hlsA", "hlsB", "embedA", "embedB")
    @classmethod
    def validate_link(cls, value: str, info: Any) -> str:
        return normalize_link(value, info.field_name)


class StreamLinksOut(StreamLinks):
    match_id: str


class StreamLinksErrorDetail(BaseModel):
    code: str
    message: str
    current_version: int | None = None


class StreamLinksError(BaseModel):
    detail: StreamLinksErrorDetail


STREAM_LINK_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": StreamLinksError} for code in (401, 403, 404, 409, 422, 503)
}


def can_read_links(match: Match, account: Account) -> bool:
    return (
        AccountType.ADMIN in account.roles
        or (match.director_id == account.id and AccountType.DIRECTOR in account.roles)
        or (match.referee_id == account.id and AccountType.REFEREE in account.roles)
    )


async def stream_links_account(request: Request) -> Account:
    try:
        return await get_current_account(
            request, request.app.state.db, request.app.state.settings
        )
    except HTTPException as exc:
        raise stream_error(401, "unauthorized", "Authentication required") from exc
    except PyMongoError:
        raise stream_error(
            503, "stream_links_unavailable", "Storage unavailable"
        ) from None


class StreamLinksService:
    def __init__(self, db: DBController) -> None:
        self.db = db

    def read(self, match_id: str, account: Account) -> StreamLinksOut:
        try:
            match = self.db.matches.get(match_id)
            if match is None:
                raise stream_error(404, "match_not_found", "Match not found")
            current = self.db.accounts.get(account.id)
            if current is None or not can_read_links(match, current):
                raise stream_error(
                    403, "stream_links_forbidden", "Stream links forbidden"
                )
            return StreamLinksOut(match_id=match_id, **match.stream_links.model_dump())
        except PyMongoError:
            raise stream_error(
                503, "stream_links_unavailable", "Storage unavailable"
            ) from None

    def save(
        self,
        match_id: str,
        account: Account,
        values: dict[str, str],
        expected_version: int | None,
    ) -> tuple[StreamLinksOut, bool]:
        try:
            for _ in range(32):
                match = self.db.matches.get(match_id)
                if match is None:
                    raise stream_error(404, "match_not_found", "Match not found")
                current = self.db.accounts.get(account.id)
                if (
                    current is None
                    or current.id != match.director_id
                    or AccountType.DIRECTOR not in current.roles
                ):
                    raise stream_error(
                        403,
                        "stream_links_forbidden",
                        "Only the designated director can save",
                    )
                if match.status == MatchStatus.ENDED or match.archived_at is not None:
                    raise stream_error(409, "match_read_only", "Match is read only")
                links = match.stream_links
                if expected_version is not None and expected_version != links.version:
                    raise stream_error(
                        409,
                        "stream_links_version_conflict",
                        "Reload stream links",
                        current_version=links.version,
                    )
                changed = any(
                    getattr(links, key) != value for key, value in values.items()
                )
                if changed and links.version == 2**53 - 1:
                    raise stream_error(
                        409,
                        "stream_links_version_conflict",
                        "Version exhausted",
                        current_version=links.version,
                    )
                replacement = links.model_dump() | values
                if changed:
                    replacement.update(
                        version=links.version + 1,
                        updated_at_ms=time.time_ns() // 1_000_000,
                        updated_by=account.id,
                    )
                version_filter: dict[str, Any] = {"stream_links.version": links.version}
                if links.version == 0:
                    version_filter = {
                        "$or": [
                            {"stream_links.version": 0},
                            {"stream_links.version": {"$exists": False}},
                        ]
                    }
                query = {
                    "_id": match_id,
                    "director_id": account.id,
                    "status": {
                        "$in": [
                            MatchStatus.CREATED,
                            MatchStatus.RUNNING,
                            MatchStatus.PAUSED,
                        ]
                    },
                    "archived_at": None,
                    "player_a_id": match.player_a_id,
                    "player_b_id": match.player_b_id,
                    **version_filter,
                }
                doc = self.db.matches.collection.find_one_and_update(
                    query,
                    {"$set": {"stream_links": replacement}},
                    return_document=ReturnDocument.AFTER,
                )
                if doc is not None:
                    return StreamLinksOut(
                        match_id=match_id, **doc["stream_links"]
                    ), changed
            raise stream_error(
                409,
                "stream_links_version_conflict",
                "Concurrent update; reload stream links",
            )
        except PyMongoError:
            raise stream_error(
                503, "stream_links_unavailable", "Storage unavailable"
            ) from None
