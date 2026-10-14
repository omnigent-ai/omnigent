"""The comment level is shown only to clients that advertise it."""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from omnigent.server.auth import LEVEL_COMMENT, LEVEL_EDIT, LEVEL_OWNER, LEVEL_READ
from omnigent.server.client_capabilities import (
    PERMISSION_LEVELS_HEADER,
    ClientCapabilitiesMiddleware,
)
from omnigent.server.schemas import PermissionObject, SessionListItem, SessionResponse


def _app(level: int | None) -> Starlette:
    async def sessions(_request: Request) -> JSONResponse:
        row = SessionListItem(
            id="conv_1",
            agent_id="a",
            status="idle",
            created_at=1,
            updated_at=1,
            permission_level=level,
        )
        return JSONResponse({"data": [row.model_dump(mode="json")]})

    async def session(_request: Request) -> JSONResponse:
        payload = SessionResponse(
            id="conv_1", agent_id="a", status="idle", created_at=1, permission_level=level
        ).model_dump(mode="json")
        return JSONResponse(payload)

    app = Starlette(routes=[Route("/session", session), Route("/sessions", sessions)])
    app.add_middleware(ClientCapabilitiesMiddleware)
    return app


async def _level(app: Starlette, headers: dict[str, str]) -> int | None:
    """The level the single-session and list responses report; they must agree."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        single = (await client.get("/session", headers=headers)).json()["permission_level"]
        listed = (await client.get("/sessions", headers=headers)).json()["data"][0]
    assert listed["permission_level"] == single
    return single


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header, expected",
    [
        ({}, LEVEL_READ),
        ({PERMISSION_LEVELS_HEADER: "comment"}, LEVEL_COMMENT),
        ({PERMISSION_LEVELS_HEADER: " Comment , future"}, LEVEL_COMMENT),
        ({PERMISSION_LEVELS_HEADER: "future"}, LEVEL_READ),
    ],
)
async def test_comment_level_needs_the_capability_header(
    header: dict[str, str], expected: int
) -> None:
    assert await _level(_app(LEVEL_COMMENT), header) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [None, LEVEL_READ, LEVEL_EDIT, LEVEL_OWNER])
async def test_other_levels_are_unchanged(level: int | None) -> None:
    assert await _level(_app(level), {}) == level


def test_serializing_outside_a_request_masks_the_comment_level() -> None:
    dumped = SessionResponse(
        id="conv_1", agent_id="a", status="idle", created_at=1, permission_level=LEVEL_COMMENT
    ).model_dump()
    assert dumped["permission_level"] == LEVEL_READ


def _grant_app(level: int) -> Starlette:
    async def grant(_request: Request) -> JSONResponse:
        payload = PermissionObject(
            user_id="bob@example.com", conversation_id="conv_1", level=level
        ).model_dump(mode="json")
        return JSONResponse(payload)

    app = Starlette(routes=[Route("/permission", grant)])
    app.add_middleware(ClientCapabilitiesMiddleware)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level, header, expected",
    [
        (LEVEL_COMMENT, {}, LEVEL_READ),
        (LEVEL_COMMENT, {PERMISSION_LEVELS_HEADER: "comment"}, LEVEL_COMMENT),
        (LEVEL_EDIT, {}, LEVEL_EDIT),
        (LEVEL_OWNER, {}, LEVEL_OWNER),
    ],
)
async def test_grant_objects_follow_the_capability_header(
    level: int, header: dict[str, str], expected: int
) -> None:
    """Older Share dialogs only know read/edit, so a comment grant reads as read."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_grant_app(level)), base_url="http://test"
    ) as client:
        assert (await client.get("/permission", headers=header)).json()["level"] == expected
