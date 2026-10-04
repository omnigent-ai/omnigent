"""The skill content route is owner-only and fails fast for older hosts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    CAP_SKILL_CONTENT,
    HostHelloFrame,
    HostSkillContentFrame,
    HostSkillContentResultFrame,
    decode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes.skill_content import (
    create_skill_content_router,
    request_host_skill_content,
)
from omnigent.stores.host_store import HostStore

_HOST_ID = "a828988dc0b441fb8d04dad3761773b9"
_URL = f"/v1/hosts/{_HOST_ID}/harnesses/claude-native/skills/toolkit%3Alint"
_SKILL = {
    "name": "toolkit:lint",
    "description": "Lint",
    "content": "# Instructions",
    "truncated": False,
}


class _Auth:
    def get_user_id(self, request: Request) -> str | None:
        return request.headers.get("x-test-user")


def _register(registry: HostRegistry, *capabilities: str) -> HostConnection:
    return registry.register(
        _HOST_ID,
        AsyncMock(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="laptop",
            capabilities=list(capabilities),
        ),
        owner="owner",
    )


@pytest.fixture
def content_app(db_uri: str) -> tuple[FastAPI, HostRegistry, HostStore]:
    registry = HostRegistry()
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(_HOST_ID, "laptop", "owner")
    app = FastAPI()
    app.include_router(
        create_skill_content_router(registry, hosts, auth_provider=_Auth()), prefix="/v1"
    )

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    return app, registry, hosts


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    "result,status",
    [
        (HostSkillContentResultFrame("", "ok", skill=_SKILL), 200),
        (HostSkillContentResultFrame("", "failed", error="lookup failed"), 502),
        # A malformed reply is a host failure.
        (
            HostSkillContentResultFrame("", "ok", skill={**_SKILL, "content": 5}),
            502,
        ),
    ],
)
async def test_owner_gets_skill_content(
    content_app, result: HostSkillContentResultFrame, status: int
) -> None:
    app, registry, _ = content_app
    conn = _register(registry, CAP_SKILL_CONTENT)
    async with _client(app) as client:
        task = asyncio.create_task(client.get(_URL, headers={"x-test-user": "owner"}))
        frame = decode_host_frame(await asyncio.wait_for(conn.outbound_queue.get(), 2))
        assert isinstance(frame, HostSkillContentFrame)
        assert frame.harness == "claude-native"
        assert frame.name == "toolkit:lint"
        result.request_id = frame.request_id
        conn.pending_skill_content[frame.request_id].set_result(result)
        response = await task
    assert response.status_code == status
    if status == 200:
        assert response.json() == _SKILL
        assert response.headers["cache-control"] == "no-store"
    assert not conn.pending_skill_content


@pytest.mark.parametrize("user,status", [("stranger", 403), (None, 401)])
async def test_non_owner_does_not_reach_the_host(
    content_app, user: str | None, status: int
) -> None:
    app, registry, _ = content_app
    conn = _register(registry, CAP_SKILL_CONTENT)
    async with _client(app) as client:
        response = await client.get(_URL, headers={"x-test-user": user} if user else {})
    assert response.status_code == status
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize("harness", ["pi-native", "antigravity-native", "opencode-native"])
async def test_unsupported_harness_does_not_reach_the_host(content_app, harness: str) -> None:
    app, registry, _ = content_app
    conn = _register(registry, CAP_SKILL_CONTENT)
    async with _client(app) as client:
        response = await client.get(
            f"/v1/hosts/{_HOST_ID}/harnesses/{harness}/skills/toolkit%3Alint",
            headers={"x-test-user": "owner"},
        )
    assert response.status_code == 404
    assert conn.outbound_queue.empty()


async def test_offline_host_is_a_conflict(content_app) -> None:
    app, _, hosts = content_app
    hosts.set_offline(_HOST_ID)
    async with _client(app) as client:
        response = await client.get(_URL, headers={"x-test-user": "owner"})
    assert response.status_code == 409


async def test_older_host_fails_fast_without_a_frame(content_app) -> None:
    app, registry, _ = content_app
    conn = _register(registry)
    async with _client(app) as client:
        response = await client.get(_URL, headers={"x-test-user": "owner"})
    assert response.status_code == 501
    assert "update the host" in response.json()["detail"]
    assert conn.outbound_queue.empty()


@pytest.mark.parametrize("outcome,status", [("timeout", 504), ("stale", 502)])
async def test_proxy_cleans_up_unanswered_requests(
    monkeypatch: pytest.MonkeyPatch, outcome: str, status: int
) -> None:
    registry = HostRegistry()
    conn = _register(registry, CAP_SKILL_CONTENT)
    if outcome == "timeout":
        monkeypatch.setattr("omnigent.server.routes.skill_content._SKILL_CONTENT_TIMEOUT_S", 0.01)
    else:
        registry.deregister(conn.host_id)
    with pytest.raises(HTTPException) as exc_info:
        await request_host_skill_content(
            host_registry=registry, host_conn=conn, harness="claude-native", name="toolkit:lint"
        )
    assert exc_info.value.status_code == status
    assert conn.pending_skill_content == {}


async def test_replacing_the_connection_after_sending_fails_fast() -> None:
    """A host reconnecting mid-request fails the old request (502) at once."""
    registry = HostRegistry()
    conn = _register(registry, CAP_SKILL_CONTENT)
    task = asyncio.create_task(
        request_host_skill_content(
            host_registry=registry, host_conn=conn, harness="claude", name="toolkit:lint"
        )
    )
    await asyncio.wait_for(conn.outbound_queue.get(), 2)  # the request went out
    _register(registry, CAP_SKILL_CONTENT)  # the same host reconnects
    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(task, 2)
    assert exc_info.value.status_code == 502
    assert conn.pending_skill_content == {}


async def test_disconnect_after_sending_fails_fast() -> None:
    """A host dropping mid-request is a lost connection (502) now, not a 504 later."""
    registry = HostRegistry()
    conn = _register(registry, CAP_SKILL_CONTENT)
    task = asyncio.create_task(
        request_host_skill_content(
            host_registry=registry, host_conn=conn, harness="claude", name="toolkit:lint"
        )
    )
    await asyncio.wait_for(conn.outbound_queue.get(), 2)  # the request went out
    registry.deregister(conn.host_id)
    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(task, 2)  # well under the 15s timeout
    assert exc_info.value.status_code == 502
    assert conn.pending_skill_content == {}
