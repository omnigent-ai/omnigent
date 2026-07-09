"""
Integration tests for ``GET /v1/hosts/{id}/worktrees``.

Wires up a real host tunnel + REST router pair, drives a fake host
that auto-replies to ``host.list_worktrees`` frames, and exercises
the endpoint's contract end-to-end. Backs the Web UI's new-session
worktree picker (branch prefill / start-in-existing-worktree).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.host.frames import (
    HostHelloFrame,
    HostListWorktreesFrame,
    HostListWorktreesResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore

# Same liveness-race flake mitigation as test_hosts_filesystem: the
# mock-WS host can be deregistered under parallel CI load, yielding a
# spurious 409. Tests are sub-second; retry masks the race.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "host_wt_test"
_HOST_NAME = "wt-test-laptop"


def _websocket_scope(path: str) -> dict[str, object]:
    """Build a minimal ASGI WebSocket scope.

    :param path: WebSocket path, e.g. ``"/v1/hosts/X/tunnel"``.
    :returns: ASGI scope dict.
    """
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


def _hello_text(name: str = _HOST_NAME) -> str:
    """Encode a hello frame for tests.

    :param name: Host name reported in the hello frame.
    :returns: JSON-encoded hello frame.
    """
    return encode_host_frame(
        HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name=name)
    )


@pytest.fixture()
def wt_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """
    App with host tunnel + REST routes for worktree-list tests.

    :param db_uri: SQLite URI fixture.
    :returns: (app, registry, host_store, conv_store).
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    app.include_router(
        create_hosts_router(registry, host_store, conv_store),
        prefix="/v1",
    )
    return app, registry, host_store, conv_store


@pytest.fixture()
async def wt_setup(
    wt_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> AsyncIterator[
    tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]]
]:
    """
    Connect a mock host and auto-reply to list_worktrees frames.

    Tests register fake replies in ``replies`` (repo_path → reply
    dict) before calling the REST endpoint. The auto-replier decodes
    outbound frames and resolves the matching pending future — the
    same wiring host_tunnel.py does in production.

    :param wt_app: The fixture above.
    :returns: Async iterator yielding the wired-up state.
    """
    app, registry, _hs, _cs = wt_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input({"type": "websocket.receive", "text": _hello_text()})
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    replies: dict[str, dict[str, Any]] = {}
    stop_drain = asyncio.Event()

    async def _drain() -> None:
        """Drain outbound WS frames and feed back the configured reply."""
        while not stop_drain.is_set():
            try:
                output = await comm.receive_output(timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if not isinstance(frame, HostListWorktreesFrame):
                continue
            reply = replies.get(frame.repo_path)
            if reply is None:
                reply_frame = HostListWorktreesResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error="not a git repository",
                )
            else:
                reply_frame = HostListWorktreesResultFrame(
                    request_id=frame.request_id,
                    status=reply.get("status", "ok"),
                    worktrees=reply.get("worktrees"),
                    error=reply.get("error"),
                )
            await comm.send_input(
                {"type": "websocket.receive", "text": encode_host_frame(reply_frame)}
            )

    drain_task = asyncio.create_task(_drain())
    try:
        yield app, registry, comm, replies
    finally:
        stop_drain.set()
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


async def test_list_worktrees_returns_data(
    wt_setup: tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """The endpoint returns ``{"object": "list", "data": [...]}`` from the host."""
    app, _reg, _comm, replies = wt_setup
    replies["/Users/corey/repo"] = {
        "worktrees": [
            {"path": "/Users/corey/repo", "branch": "main", "is_main": True, "detached": False},
            {
                "path": "/Users/corey/repo-worktrees/feature-x",
                "branch": "feature/x",
                "is_main": False,
                "detached": False,
            },
        ],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/worktrees",
            params={"path": "/Users/corey/repo"},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["object"] == "list"
    branches = [w["branch"] for w in payload["data"]]
    assert branches == ["main", "feature/x"]
    assert payload["data"][1]["is_main"] is False


async def test_list_worktrees_non_git_path_400(
    wt_setup: tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """A non-git path (host reports failed) maps to 400 so the picker shows nothing."""
    app, _reg, _comm, _replies = wt_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No reply registered → the drain replies "failed: not a git repository".
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/worktrees",
            params={"path": "/tmp/not-a-repo"},
        )
    assert resp.status_code == 400, resp.text


async def test_list_worktrees_missing_path_param_422(
    wt_setup: tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """The ``path`` query param is required."""
    app, _reg, _comm, _replies = wt_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/worktrees")
    assert resp.status_code == 422, resp.text


async def test_list_worktrees_unknown_host_404(
    wt_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """An unknown host id yields 404 (existence is gated before the offline check)."""
    app, _reg, _hs, _cs = wt_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/worktrees",
            params={"path": "/Users/corey/repo"},
        )
    # No host record was created (no tunnel connected) → 404, not 409.
    assert resp.status_code == 404, resp.text
