"""
Integration tests for ``GET /v1/hosts/{id}/agents/{agent_id}/skills``.

Wires up a real host tunnel + REST router pair, drives a fake host that
auto-replies to ``host.skills`` frames, and exercises the endpoint's
contract end-to-end. The new-session composer's ``/`` menu knows an
agent's *bundled* skills from ``GET /v1/agents``; the ones on the user's
own machine are visible only to the host, so this route asks it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostHelloFrame,
    HostSkillsFrame,
    HostSkillsResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores import AgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

# Mirrors test_hosts_filesystem: a mock host tunnel can flake with a 409
# ("host is offline") under parallel CI load when the mock WS is starved and
# the connection deregistered. Tests here are sub-second.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "4f3c2b1a09d8e7f6a5b4c3d2e1f00998"
_HOST_NAME = "skills-test-laptop"
_AGENT_ID = "ag_skilled"


def _websocket_scope(path: str) -> dict[str, object]:
    """Build a minimal ASGI WebSocket scope."""
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


def _fake_agent_deps(
    *,
    harness: str = "claude-sdk",
    skills_filter: str | list[str] = "all",
    bundled: tuple[str, ...] = (),
    bundle_location: str | None = "bundle://skilled",
) -> tuple[AgentStore, AgentCache]:
    """
    Minimal agent store + cache doubles for the route's spec lookup.

    The route reads exactly three spec fields — harness (which discovery
    provider), ``skills_filter`` (what may surface), and the bundled names
    (what to drop) — so a namespace carrying those is enough, and keeps the
    test off a materialized bundle on disk.
    """
    agent = SimpleNamespace(id=_AGENT_ID, bundle_location=bundle_location)
    spec = SimpleNamespace(
        executor=SimpleNamespace(harness_kind=harness),
        skills_filter=skills_filter,
        skills=[SimpleNamespace(name=name) for name in bundled],
    )
    store = SimpleNamespace(get=lambda agent_id: agent if agent_id == _AGENT_ID else None)
    cache = SimpleNamespace(load=lambda _id, _loc: SimpleNamespace(spec=spec))
    return cast(AgentStore, store), cast(AgentCache, cache)


def _build_app(
    db_uri: str,
    agent_deps: tuple[AgentStore, AgentCache] | None,
) -> tuple[FastAPI, HostRegistry, HostStore]:
    """App with host tunnel + REST routes for skill-discovery tests."""
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            agent_store=None if agent_deps is None else agent_deps[0],
            agent_cache=None if agent_deps is None else agent_deps[1],
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        """Convert application errors to structured JSON responses."""
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return app, registry, host_store


class _SkillsHost:
    """A connected mock host that auto-replies to ``host.skills`` frames."""

    def __init__(self, comm: ApplicationCommunicator) -> None:
        self._comm = comm
        self._stop = asyncio.Event()
        self.received: list[HostSkillsFrame] = []
        # What to answer with; tests overwrite before calling the endpoint.
        self.reply: dict[str, Any] = {"status": "ok", "skills": []}
        self.task: asyncio.Task[None] | None = None

    async def drain(self) -> None:
        """Forward each outbound skills frame back as a configured result."""
        while not self._stop.is_set():
            try:
                output = await self._comm.receive_output(timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if not isinstance(frame, HostSkillsFrame):
                continue
            self.received.append(frame)
            await self._comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostSkillsResultFrame(
                            request_id=frame.request_id,
                            status=self.reply.get("status", "ok"),
                            skills=self.reply.get("skills", []),
                            error=self.reply.get("error"),
                        )
                    ),
                }
            )

    def stop(self) -> None:
        """Signal the drain loop to finish."""
        self._stop.set()


async def _connect_host(app: FastAPI, registry: HostRegistry) -> _SkillsHost:
    """Connect a mock host tunnel and start its auto-replier."""
    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(
                HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name=_HOST_NAME)
            ),
        }
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)
    host = _SkillsHost(comm)
    host.task = asyncio.create_task(host.drain())
    return host


@pytest.fixture()
async def skills_setup(db_uri: str) -> AsyncIterator[tuple[FastAPI, _SkillsHost]]:
    """App + connected mock host, with a spec declaring one bundled skill."""
    app, registry, _host_store = _build_app(db_uri, _fake_agent_deps(bundled=("review-pr",)))
    host = await _connect_host(app, registry)
    try:
        yield app, host
    finally:
        host.stop()
        if host.task is not None:
            try:
                await asyncio.wait_for(host.task, timeout=1.0)
            except asyncio.TimeoutError:
                host.task.cancel()


async def _get_skills(app: FastAPI, query: str = "") -> Any:
    """Call the endpoint and return the response object."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(f"/v1/hosts/{_HOST_ID}/agents/{_AGENT_ID}/skills{query}")


# ── Happy path ──────────────────────────────────────────


async def test_returns_host_skills_and_drops_bundled_duplicates(
    skills_setup: tuple[FastAPI, _SkillsHost],
) -> None:
    """Host skills reach the composer; a name the bundle already claims doesn't.

    The menu concatenates this onto the agent's bundled skills, so a name
    present in both would render twice — and the bundle's own description is
    the one the picker already shows.
    """
    app, host = skills_setup
    host.reply = {
        "status": "ok",
        "skills": [
            {"name": "dev-productivity:deslop", "description": "Remove slop"},
            {"name": "my-own-skill", "description": "Something local"},
            # Already bundled (see the fixture's spec) — must not come back.
            {"name": "review-pr", "description": "A host copy"},
        ],
    }

    resp = await _get_skills(app)

    assert resp.status_code == 200
    assert resp.json() == {
        "skills": [
            {"name": "dev-productivity:deslop", "description": "Remove slop"},
            {"name": "my-own-skill", "description": "Something local"},
        ]
    }


async def test_frame_carries_the_specs_harness_filter_and_the_workspace(
    db_uri: str,
) -> None:
    """Discovery is scoped by the agent's own spec, not by defaults.

    The harness selects the per-vendor provider and the filter decides what
    may surface at all, so both must ride the frame — a spec pinning
    ``skills: [deslop]`` must not have the user's whole library offered.
    """
    app, registry, _hs = _build_app(
        db_uri,
        _fake_agent_deps(harness="codex-native", skills_filter=["deslop"]),
    )
    host = await _connect_host(app, registry)
    try:
        resp = await _get_skills(app, "?path=/Users/corey/proj")
    finally:
        host.stop()
        if host.task is not None:
            await asyncio.wait_for(host.task, timeout=1.0)

    assert resp.status_code == 200
    assert len(host.received) == 1
    sent = host.received[0]
    assert sent.harness == "codex-native"
    assert sent.skills_filter == ["deslop"]
    assert sent.path == "/Users/corey/proj"


async def test_omitting_the_path_asks_for_home_scope_only(
    skills_setup: tuple[FastAPI, _SkillsHost],
) -> None:
    """No workspace chosen yet still answers, with no directory to walk."""
    app, host = skills_setup

    resp = await _get_skills(app)

    assert resp.status_code == 200
    assert host.received[0].path is None


async def test_without_an_agent_store_the_generic_walk_is_requested(
    db_uri: str,
) -> None:
    """A server with no agent deps degrades to the harness-agnostic walk.

    The host reads an empty harness as "no vendor mechanism I can enumerate"
    and falls back to ``discover_host_skills``, which is the honest answer
    when the spec can't be consulted.
    """
    app, registry, _hs = _build_app(db_uri, None)
    host = await _connect_host(app, registry)
    try:
        resp = await _get_skills(app)
    finally:
        host.stop()
        if host.task is not None:
            await asyncio.wait_for(host.task, timeout=1.0)

    assert resp.status_code == 200
    assert host.received[0].harness == ""
    assert host.received[0].skills_filter == "all"


# ── Failure modes ───────────────────────────────────────


async def test_host_failure_is_a_502(
    skills_setup: tuple[FastAPI, _SkillsHost],
) -> None:
    """A failed discovery surfaces the host's reason, not an empty success."""
    app, host = skills_setup
    host.reply = {"status": "failed", "error": "skill discovery crashed for 'claude-sdk'"}

    resp = await _get_skills(app)

    assert resp.status_code == 502
    assert "crashed" in resp.json()["detail"]


async def test_unknown_agent_returns_404(
    skills_setup: tuple[FastAPI, _SkillsHost],
) -> None:
    """An agent id the store doesn't know is a client error, not an empty list."""
    app, _host = skills_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/agents/ag_nope/skills")
    assert resp.status_code == 404


async def test_unknown_host_returns_404(db_uri: str) -> None:
    """An unregistered host is 404 — it must not confirm other hosts exist."""
    app, _reg, _hs = _build_app(db_uri, _fake_agent_deps())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/agents/{_AGENT_ID}/skills")
    assert resp.status_code == 404


async def test_offline_host_returns_409(db_uri: str) -> None:
    """Discovery needs a live tunnel, so an offline host is a conflict."""
    app, _reg, host_store = _build_app(db_uri, _fake_agent_deps())
    host_store.upsert_on_connect(host_id=_HOST_ID, name="offline-host", user_id="local")
    host_store.set_offline(_HOST_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/agents/{_AGENT_ID}/skills")
    assert resp.status_code == 409


async def test_unloadable_spec_still_answers(db_uri: str) -> None:
    """A spec that won't parse must not break the menu.

    The composer falls back to bundled skills alone if this 500s; degrading to
    the generic walk keeps the request useful and the dialog usable.
    """
    agent = SimpleNamespace(id=_AGENT_ID, bundle_location="bundle://broken")

    def _raise(_id: str, _loc: str) -> Any:
        raise OmnigentError("bad spec")

    store = cast(
        AgentStore,
        SimpleNamespace(get=lambda agent_id: agent if agent_id == _AGENT_ID else None),
    )
    cache = cast(AgentCache, SimpleNamespace(load=_raise))
    app, registry, _hs = _build_app(db_uri, (store, cache))
    host = await _connect_host(app, registry)
    try:
        resp = await _get_skills(app)
    finally:
        host.stop()
        if host.task is not None:
            await asyncio.wait_for(host.task, timeout=1.0)

    assert resp.status_code == 200
    assert host.received[0].harness == ""
