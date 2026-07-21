"""
Integration tests for ``GET /v1/hosts/{id}/filesystem`` and
``GET /v1/hosts/{id}/filesystem/{path}``.

Wires up a real host tunnel + REST router pair, drives a fake host
that auto-replies to ``host.list_dir`` frames, and exercises the
endpoint's contract end-to-end. The REST shape mirrors the existing
session-scoped filesystem endpoint
(``GET /v1/sessions/{id}/resources/environments/default/filesystem``)
so the Web UI's existing tree component can be reused with a different
URL prefix.
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
    HostListDirFrame,
    HostListDirResultFrame,
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

# Interim: any test using the ``fs_setup`` mock host tunnel can flake
# with a 409 "host is offline" under parallel CI load (mock-WS starved
# and deregistered). Module-wide because a worksteal change spread the
# flake beyond ``root_forwards_tilde``. Tests are
# sub-second; remove once the liveness race is fixed.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "9ab0645ef9c07bb922a404d4ec2466a9"
_HOST_NAME = "fs-test-laptop"


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
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
        )
    )


@pytest.fixture()
def fs_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """
    App with host tunnel + REST routes for filesystem-browse tests.

    :param db_uri: SQLite URI fixture.
    :returns: (app, registry, host_store, conv_store).
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(registry, host_store),
        prefix="/v1",
    )
    app.include_router(
        create_hosts_router(registry, host_store, conv_store),
        prefix="/v1",
    )
    return app, registry, host_store, conv_store


@pytest.fixture()
async def fs_setup(
    fs_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> AsyncIterator[
    tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ]
]:
    """
    Connect a mock host and start an auto-replier for list_dir frames.

    Tests register fake replies in ``replies`` (path → reply dict)
    before calling the REST endpoint. The auto-replier consumes
    frames the route layer pushes through the registry, decodes
    them, and resolves the matching pending future — mirroring
    what host_tunnel.py does in production.

    :param fs_app: The fixture above.
    :returns: Async iterator yielding the wired-up state.
    """
    app, registry, _hs, _cs = fs_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input({"type": "websocket.receive", "text": _hello_text()})
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    conn = registry.get(_HOST_ID)
    assert conn is not None
    replies: dict[str, dict[str, Any]] = {}
    stop_drain = asyncio.Event()

    async def _drain() -> None:
        """Drain outbound WS frames from the communicator and reply.

        The route enqueues frames on ``conn.outbound_queue``; the
        host tunnel's ``_sender_loop`` then forwards them to the
        WebSocket as ``websocket.send`` events. We pop those
        events from the communicator's output queue, decode each
        list_dir frame, and feed the configured reply back as a
        ``websocket.receive`` event — which the route's receive
        loop turns into a resolved future.

        :returns: None when ``stop_drain`` is set or no events
            arrive within the per-iteration timeout.
        """
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
            if not isinstance(frame, HostListDirFrame):
                continue
            reply = replies.get(frame.path)
            if reply is None:
                # Default reply: path missing. Tests must register
                # a reply for every path they expect to be queried.
                reply_frame = HostListDirResultFrame(
                    request_id=frame.request_id,
                    status="ok",
                    error="path does not exist",
                )
            else:
                reply_frame = HostListDirResultFrame(
                    request_id=frame.request_id,
                    status=reply.get("status", "ok"),
                    entries=reply.get("entries", []),
                    has_more=reply.get("has_more", False),
                    error=reply.get("error"),
                )
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(reply_frame),
                }
            )

    drain_task = asyncio.create_task(_drain())
    try:
        yield app, registry, comm, replies, drain_task
    finally:
        stop_drain.set()
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


# ── Happy path ──────────────────────────────────────────


async def test_list_filesystem_returns_paginated_entries(
    fs_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Verify the endpoint returns the runner-compatible response shape:
    ``{"object": "list", "data": [...], "has_more": bool}``.

    Without this match, the Web UI's existing
    ``fetchWorkspaceDirectory`` hook would fail to parse the
    response (different field names) and the picker would render
    no entries.
    """
    from omnigent.host.frames import HostListDirEntry

    app, _reg, _comm, replies, _drain = fs_setup
    replies["/Users/corey/projects"] = {
        "entries": [
            HostListDirEntry(
                name="src",
                path="/Users/corey/projects/src",
                type="directory",
                bytes=None,
                modified_at=1779980000,
            ),
            HostListDirEntry(
                name="README.md",
                path="/Users/corey/projects/README.md",
                type="file",
                bytes=42,
                modified_at=1779980100,
            ),
        ],
        "has_more": False,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/filesystem/Users/corey/projects")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["object"] == "list"
    assert payload["has_more"] is False
    names = [entry["name"] for entry in payload["data"]]
    assert names == ["src", "README.md"]
    # Type field present and correct so the Web UI can pick the
    # right icon.
    types = [entry["type"] for entry in payload["data"]]
    assert types == ["directory", "file"]


async def test_list_filesystem_root_forwards_tilde(
    fs_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Verify that the empty-path endpoint forwards ``~`` to the host.

    Per ``designs/SESSION_WORKSPACE_SELECTION.md``: the host is the
    source of truth for ``~``; the server passes tildes through
    unchanged. The fixture's reply table is keyed by the path the
    server sent, so a ``~`` key matching a successful response
    proves the forward.
    """
    from omnigent.host.frames import HostListDirEntry

    app, _reg, _comm, replies, _drain = fs_setup
    replies["~"] = {
        "entries": [
            HostListDirEntry(
                name="projects",
                path="/Users/corey/projects",
                type="directory",
                bytes=None,
                modified_at=1779980200,
            ),
        ],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No trailing path → server forwards ~.
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/filesystem")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["name"] == "projects"


async def test_list_filesystem_tilde_path_forwards_unchanged(
    fs_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Verify that ``~/projects`` in the URL reaches the host as
    ``~/projects`` (not URL-decoded into something else, not
    server-expanded).

    Pairs with the previous test to fully pin the tilde-forwarding
    contract — covering both the empty-path-defaults-to-~ case
    and the explicit-tilde-in-path case.
    """
    from omnigent.host.frames import HostListDirEntry

    app, _reg, _comm, replies, _drain = fs_setup
    replies["~/projects"] = {
        "entries": [
            HostListDirEntry(
                name="myapp",
                path="/Users/corey/projects/myapp",
                type="directory",
                bytes=None,
                modified_at=1779980300,
            ),
        ],
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/filesystem/~/projects")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"][0]["name"] == "myapp"


# ── Error paths ─────────────────────────────────────────


async def test_list_filesystem_unknown_host_returns_404(
    fs_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify a request for a host that doesn't exist returns 404.

    The route must look the host up in the host_store first; an
    unauthenticated probe for a non-existent host shouldn't reveal
    other hosts' existence either.
    """
    app, _reg, _hs, _cs = fs_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts/7139b7e896ef9478abca6480107d1677/filesystem")
    assert resp.status_code == 404


async def test_list_filesystem_offline_host_returns_409(
    fs_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify a request for a host whose tunnel is closed returns 409.

    The host record exists in the DB but is not in the registry
    — list_dir requires a live tunnel so the only sensible
    response is "host is offline" (409 Conflict, mirroring the
    launch endpoint).
    """
    app, _reg, host_store, _cs = fs_app
    # Persist the host record but never register a tunnel.
    host_store.upsert_on_connect(
        host_id="3d9665477127e41f42de3f4109418173",
        name="offline-host",
        user_id="local",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts/3d9665477127e41f42de3f4109418173/filesystem")
    assert resp.status_code == 409


async def test_list_filesystem_missing_path_returns_404(
    fs_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Verify that browsing a non-existent path on the host returns 404.

    The host returns ``status: "ok", error: "path does not exist"``;
    the route must distinguish that from a successful empty
    listing and surface it as 404 so the Web UI can render a
    "not found" state.
    """
    app, _reg, _comm, _replies, _drain = fs_setup
    # No reply registered for this path → fixture returns the
    # default "path does not exist" reply.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/filesystem/Users/corey/missing")
    assert resp.status_code == 404
    assert "path does not exist" in resp.text


async def test_list_filesystem_host_io_failure_returns_502(
    fs_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Verify ``status: "failed"`` from the host surfaces as 502.

    Distinguishes legitimate "missing path" (404) from
    server-side I/O errors (502 Bad Gateway). Without this, the
    Web UI couldn't tell whether to retry or surface a permanent
    error.
    """
    app, _reg, _comm, replies, _drain = fs_setup
    replies["/Users/corey/io_fail"] = {
        "status": "failed",
        "error": "scandir failed: I/O error",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/filesystem/Users/corey/io_fail")
    assert resp.status_code == 502


async def test_list_filesystem_nul_byte_in_path_returns_400(
    fs_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Verify NUL byte in path is rejected with 400 before reaching
    the host.

    A NUL byte would never make it through ``os.scandir`` cleanly
    on the host, but rejecting it at the route layer is cheaper
    and surfaces a clearer error than a generic OSError.
    """
    app, _reg, _comm, _replies, _drain = fs_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}/filesystem/foo%00bar")
    assert resp.status_code == 400


# ── Auth / multi-user ───────────────────────────────────


async def test_list_filesystem_owner_check_blocks_other_users(
    fs_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify the owner check returns 403 when an authenticated caller
    is not the host owner.

    ``designs/SESSION_WORKSPACE_SELECTION.md`` "Security surface":
    the endpoint exposes the entire host filesystem to the host
    owner, and v1 deliberately does not extend it to non-owner
    users. This test pins that contract: a host owned by alice,
    accessed with bob's identity → 403.
    """
    from omnigent.server.auth import AuthProvider

    _app, _reg, host_store, conv_store = fs_app
    # Re-mount routes with an auth provider that returns the
    # X-Test-User header. Mirrors the pattern in test_hosts_api.py.

    class _Stub(AuthProvider):
        """Stub auth provider that reads X-Test-User from request.

        :returns: User id from the header, or ``None``.
        """

        def get_user_id(self, request: Any) -> str | None:
            """Extract user id from the X-Test-User header.

            :param request: FastAPI Request or WebSocket.
            :returns: Header value or ``None``.
            """
            return request.headers.get("X-Test-User")

    auth = _Stub()
    auth_app = FastAPI()
    registry = HostRegistry()
    auth_app.include_router(
        create_host_tunnel_router(registry, host_store, auth_provider=auth),
        prefix="/v1",
    )
    auth_app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            auth_provider=auth,
        ),
        prefix="/v1",
    )

    # Persist a host owned by alice.
    host_store.upsert_on_connect(
        host_id="f54bb9272002938a3a934bfcb6bb228a",
        name="alice-laptop",
        user_id="alice@example.com",
    )

    async with AsyncClient(
        transport=ASGITransport(app=auth_app), base_url="http://test"
    ) as client:
        resp = await client.get(
            "/v1/hosts/f54bb9272002938a3a934bfcb6bb228a/filesystem",
            headers={"X-Test-User": "bob@example.com"},
        )
    assert resp.status_code == 403


# ── Pagination ──────────────────────────────────────────


async def test_list_filesystem_forwards_pagination_params(
    fs_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Verify the ``limit`` / ``after`` / ``before`` query params are
    forwarded to the host.list_dir frame.

    Capture the frame the route sent and assert its pagination
    fields match the request. Without this forwarding, the Web
    UI's "next page" / "prev page" buttons would silently always
    return the first page.
    """
    from omnigent.host.frames import HostListDirEntry

    app, registry, _comm, replies, _drain = fs_setup

    # Capture the most recent list_dir frame at the registry
    # level so the assertion sees the actual server output.
    captured: dict[str, Any] = {}
    original_send = registry.send_text

    def _capturing_send(conn: Any, text: str) -> None:
        """Wrap send_text to capture list_dir frames.

        :param conn: Host connection.
        :param text: Outbound JSON frame text.
        """
        frame = decode_host_frame(text)
        if isinstance(frame, HostListDirFrame):
            captured["limit"] = frame.limit
            captured["after"] = frame.after
            captured["before"] = frame.before
        original_send(conn, text)

    registry.send_text = _capturing_send  # type: ignore[method-assign]
    replies["/foo"] = {
        "entries": [
            HostListDirEntry(
                name="x",
                path="/foo/x",
                type="file",
                bytes=1,
                modified_at=1,
            ),
        ],
        "has_more": False,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/filesystem/foo",
            params={"limit": 5, "after": "/foo/m"},
        )
    assert resp.status_code == 200
    assert captured == {"limit": 5, "after": "/foo/m", "before": None}


async def test_list_filesystem_limit_above_max_rejected(
    fs_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Verify ``limit`` above the configured max is rejected with 422.

    Without an upper bound, a client could request a page of
    millions of entries — the host would happily attempt the
    listing and the JSON response could exhaust memory.
    """
    app, _reg, _comm, _replies, _drain = fs_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/filesystem",
            params={"limit": 100_000},
        )
    # FastAPI returns 422 for failed Query validation.
    assert resp.status_code == 422
