"""
Integration tests for ``POST /v1/hosts/{id}/repositories``.

Wires a real host tunnel and REST router pair to a fake host that answers
``host.clone_repository`` frames, mirroring ``test_hosts_create_directory.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.host.frames import (
    CAP_CLONE_REPOSITORY,
    HostCloneRepositoryFrame,
    HostCloneRepositoryResultFrame,
    HostHelloFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.git_clone import CLONE_REPOSITORY_BUDGET_S
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore
from tests._helpers.polling import poll_until_async
from tests.server.helpers import websocket_scope as _websocket_scope

# Same liveness-race flake guard as test_hosts_create_directory.py.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "c1a7e0b0d3f54b0a9d8e2f6a1b3c5d7e"
_ROUTE = f"/v1/hosts/{_HOST_ID}/repositories"


@dataclass
class _FakeHost:
    """Records clone frames the server sends and the reply to return.

    :param reply: Result fields for every clone frame; ``None`` replies with
        success at ``<parent_directory>/<directory_name>``.
    :param frames: Clone frames received, in order.
    :param disconnect_on_clone: Drop the tunnel instead of replying.
    """

    reply: dict[str, Any] | None = None
    frames: list[HostCloneRepositoryFrame] = field(default_factory=list)
    disconnect_on_clone: bool = False


async def _connect_fake_host(
    db_uri: str, capabilities: list[str]
) -> AsyncIterator[tuple[FastAPI, _FakeHost]]:
    """Connect a fake host with ``capabilities`` and answer its clone frames.

    :param db_uri: SQLite URI for the stores.
    :param capabilities: Capabilities advertised in the host hello.
    :returns: Async iterator yielding the app and the fake host's state.
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    app.include_router(
        create_hosts_router(registry, host_store, SqlAlchemyConversationStore(db_uri)),
        prefix="/v1",
    )
    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    hello = HostHelloFrame(
        version="0.1.0-test",
        frame_protocol_version=1,
        name="clone-test-laptop",
        capabilities=capabilities,
    )
    await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(hello)})
    await poll_until_async(lambda: registry.get(_HOST_ID) is not None, timeout_s=5.0)

    fake = _FakeHost()

    async def _drain() -> None:
        """Answer clone frames until teardown cancels the drain."""
        while True:
            output = await comm.receive_output(timeout=None)
            text = output.get("text") if output.get("type") == "websocket.send" else None
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if not isinstance(frame, HostCloneRepositoryFrame):
                continue
            fake.frames.append(frame)
            if fake.disconnect_on_clone:
                await comm.send_input({"type": "websocket.disconnect", "code": 1006})
                return
            reply = fake.reply or {
                "status": "ok",
                "path": f"{frame.parent_directory}/{frame.directory_name}",
            }
            result = HostCloneRepositoryResultFrame(request_id=frame.request_id, **reply)
            await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(result)})

    drain_task = asyncio.create_task(_drain())
    try:
        yield app, fake
    finally:
        drain_task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await drain_task
        finally:
            if not fake.disconnect_on_clone:
                await comm.send_input({"type": "websocket.disconnect", "code": 1000})
            await comm.wait(timeout=5.0)


@pytest.fixture()
async def clone_host(db_uri: str) -> AsyncIterator[tuple[FastAPI, _FakeHost]]:
    """A connected host that supports cloning.

    :param db_uri: SQLite URI fixture.
    :returns: Async iterator yielding the app and fake host.
    """
    async for wired in _connect_fake_host(db_uri, [CAP_CLONE_REPOSITORY]):
        yield wired


async def _post(app: FastAPI, body: dict[str, Any], route: str = _ROUTE) -> Any:
    """POST ``body`` to the clone route.

    :param app: App under test.
    :param body: JSON request body.
    :param route: Route to call.
    :returns: The httpx response.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(route, json=body)


@pytest.mark.parametrize("reused_existing", [False, True])
async def test_clone_forwards_parsed_repository_and_returns_checkout(
    clone_host: tuple[FastAPI, _FakeHost], reused_existing: bool
) -> None:
    """The host receives the parsed name and budget; the route returns its result."""
    app, fake = clone_host
    fake.reply = {"status": "ok", "path": "~/code/widgets", "reused_existing": reused_existing}
    response = await _post(
        app, {"url": " git@github.com:acme/widgets.git ", "parent_directory": "~/code"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "object": "repository_clone",
        "path": "~/code/widgets",
        "name": "widgets",
        "reused_existing": reused_existing,
    }
    [frame] = fake.frames
    assert frame.url == "git@github.com:acme/widgets.git"
    assert frame.parent_directory == "~/code"
    assert frame.directory_name == "widgets"
    assert frame.timeout_s == CLONE_REPOSITORY_BUDGET_S


@pytest.mark.parametrize(
    ("reply", "status_code", "detail"),
    [
        (
            {"status": "conflict", "error": "/work/widgets already exists"},
            409,
            "/work/widgets already exists",
        ),
        (
            {"status": "failed", "error": "Permission denied (publickey)."},
            422,
            "Couldn't clone the repository: Permission denied (publickey).",
        ),
    ],
)
async def test_clone_maps_host_failures(
    clone_host: tuple[FastAPI, _FakeHost],
    reply: dict[str, Any],
    status_code: int,
    detail: str,
) -> None:
    """Destination conflicts and git failures surface the host's message."""
    app, fake = clone_host
    fake.reply = reply
    response = await _post(
        app, {"url": "https://github.com/acme/widgets", "parent_directory": "/work"}
    )

    assert response.status_code == status_code
    assert response.json()["detail"] == detail


async def test_clone_fails_fast_when_host_disconnects(
    clone_host: tuple[FastAPI, _FakeHost],
) -> None:
    """A tunnel drop mid-clone answers 502 instead of waiting out the clone budget."""
    app, fake = clone_host
    fake.disconnect_on_clone = True
    response = await asyncio.wait_for(
        _post(app, {"url": "https://github.com/acme/widgets", "parent_directory": "/work"}),
        timeout=10.0,
    )

    assert response.status_code == 502
    assert "connection lost" in response.json()["detail"]


@pytest.mark.parametrize(
    ("body", "detail_fragment"),
    [
        ({"url": "ftp://example.com/repo", "parent_directory": "/work"}, "https://"),
        (
            {"url": "https://user:token@github.com/acme/widgets", "parent_directory": "/work"},
            "must not embed credentials",
        ),
        ({"url": "https://github.com/acme/widgets#main", "parent_directory": "/work"}, "'#'"),
        ({"url": "git@foo/bar:baz", "parent_directory": "/work"}, "local path"),
        ({"url": "https://github.com/acme/widgets", "parent_directory": "code"}, "absolute"),
        ({"url": "https://github.com/acme/widgets", "parent_directory": "  "}, "empty"),
    ],
)
async def test_clone_rejects_invalid_input_before_reaching_host(
    clone_host: tuple[FastAPI, _FakeHost],
    body: dict[str, Any],
    detail_fragment: str,
) -> None:
    """Bad URLs and relative parents 400 without sending a frame."""
    app, fake = clone_host
    response = await _post(app, body)

    assert response.status_code == 400, response.text
    assert detail_fragment in response.json()["detail"]
    assert fake.frames == []


async def test_clone_on_host_without_capability_asks_for_update(db_uri: str) -> None:
    """A host that predates cloning gets an actionable 409, not a hang."""
    async for app, fake in _connect_fake_host(db_uri, []):
        response = await _post(
            app, {"url": "https://github.com/acme/widgets", "parent_directory": "/work"}
        )
        assert response.status_code == 409
        assert "Update Omnigent on the host" in response.json()["detail"]
        assert fake.frames == []


async def test_clone_unknown_host_returns_404(clone_host: tuple[FastAPI, _FakeHost]) -> None:
    """An unknown host id is a 404."""
    app, _fake = clone_host
    response = await _post(
        app,
        {"url": "https://github.com/acme/widgets", "parent_directory": "/work"},
        route="/v1/hosts/7139b7e896ef9478abca6480107d1677/repositories",
    )

    assert response.status_code == 404
    # The web client tells an unknown host from a missing route by this detail.
    assert response.json()["detail"] == "host not found"
