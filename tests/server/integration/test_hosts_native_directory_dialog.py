"""
Integration tests for ``POST /v1/hosts/{id}/native-directory-dialog``.

Wires up a real host tunnel + REST router pair, drives a fake host
that auto-replies to ``host.open_directory_dialog`` frames, and
exercises the endpoint's contract end-to-end. Mirrors the structure of
``test_hosts_create_directory.py`` (the create-folder endpoint) — the
native folder chooser shares the same owner-scoped, host-forwarded
design, differing only in that the host runs an OS dialog instead of a
mkdir.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.host.frames import (
    HostHelloFrame,
    HostOpenDirectoryDialogFrame,
    HostOpenDirectoryDialogResultFrame,
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

# Same liveness-race flake guard as test_hosts_create_directory.py: the
# mock WS host can be starved + deregistered under parallel CI load.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "c1d4e6f90a2b37184d9f0c6b5e8a1d27"
_HOST_NAME = "native-dialog-laptop"


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


def _hello_text(
    name: str = _HOST_NAME,
    capabilities: dict[str, Any] | None = None,
) -> str:
    """Encode a hello frame for tests.

    :param name: Host name reported in the hello frame.
    :param capabilities: Capabilities map to advertise (e.g.
        ``{"native_directory_dialog": True}``); ``None`` mimics an
        older host / unsupported host.
    :returns: JSON-encoded hello frame.
    """
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
            capabilities=capabilities,
        )
    )


@pytest.fixture()
def dialog_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """App with host tunnel + REST routes for native-dialog tests.

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
async def dialog_setup(
    dialog_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> AsyncIterator[
    tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        dict[str, Any],
        asyncio.Task[None],
    ]
]:
    """Connect a mock host and start an auto-replier for dialog frames.

    Tests configure the host's reply in one of two ways:

    - Set ``default_reply`` (the 5th yielded value, a mutable dict) BEFORE
      the call to change the reply used for EVERY request_id. This is the
      simple path for cancelled / unsupported / error statuses, since the
      endpoint mints its own request_id the test cannot predict.
    - Register a per-request_id reply in ``replies`` (the 4th yielded
      value) to override the default for one specific id (rarely needed).

    The auto-replier consumes the ``host.open_directory_dialog`` frames the
    route pushes, decodes them, and feeds the configured result back —
    mirroring what ``host_tunnel.py`` does in production. The default reply
    is a successful pick of ``/tmp/picked``.

    :param dialog_app: The fixture above.
    :returns: Async iterator yielding
        ``(app, registry, comm, replies, default_reply, drain_task)``.
    """
    app, registry, _hs, _cs = dialog_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": _hello_text(capabilities={"native_directory_dialog": True}),
        },
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    conn = registry.get(_HOST_ID)
    assert conn is not None
    # Per-request_id overrides; when none is registered for a frame's id
    # the drain falls back to ``default_reply`` (mutated by a test before
    # the call to drive a cancelled / unsupported / error status).
    replies: dict[str, dict[str, Any]] = {}
    default_reply: dict[str, Any] = {"status": "ok", "path": "/tmp/picked"}
    stop_drain = asyncio.Event()

    async def _drain() -> None:
        """Drain outbound WS frames and reply to dialog frames.

        :returns: None when ``stop_drain`` is set or no events arrive
            within the per-iteration timeout.
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
            if not isinstance(frame, HostOpenDirectoryDialogFrame):
                continue
            reply = replies.get(frame.request_id, default_reply)
            reply_frame = HostOpenDirectoryDialogResultFrame(
                request_id=frame.request_id,
                status=reply.get("status", "ok"),
                path=reply.get("path"),
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
        yield app, registry, comm, replies, default_reply, drain_task
    finally:
        stop_drain.set()
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()


# ── Happy path ──────────────────────────────────────────


_Setup = tuple[
    FastAPI,
    HostRegistry,
    ApplicationCommunicator,
    dict[str, dict[str, Any]],
    dict[str, Any],
    asyncio.Task[None],
]


async def test_native_dialog_returns_picked_path(dialog_setup: _Setup) -> None:
    """A successful host pick returns the chosen absolute path.

    This is the path the Web UI feeds into ``setWorkspace`` before
    session-create validation, so it must round-trip intact.
    """
    app, _reg, _comm, _replies, _default, _drain = dialog_setup
    # The drain's default reply is ok + /tmp/picked.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/native-directory-dialog")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "native_directory_dialog"
    assert body["status"] == "ok"
    assert body["path"] == "/tmp/picked"
    assert body["error"] is None


async def test_native_dialog_cancelled_status_passes_through(dialog_setup: _Setup) -> None:
    """A user-cancelled dialog returns status ``cancelled`` (not an error).

    The Web UI uses this to keep the in-app picker open silently rather
    than surfacing a failure — so the status must survive intact and the
    path must be None. The endpoint mints its own request_id, so the
    fixture's ``default_reply`` (applied to every id) drives the status.
    """
    app, _reg, _comm, _replies, default_reply, _drain = dialog_setup
    default_reply.clear()
    default_reply.update({"status": "cancelled", "path": None, "error": None})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/native-directory-dialog")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["path"] is None
    assert body["error"] is None


async def test_native_dialog_unsupported_status_passes_through(dialog_setup: _Setup) -> None:
    """An ``unsupported`` host result passes through as a non-error status.

    A non-macOS / headless host reports ``unsupported``; the Web UI
    falls back to the in-app picker, so this must NOT be an HTTP error.
    """
    app, _reg, _comm, _replies, default_reply, _drain = dialog_setup
    default_reply.clear()
    default_reply.update({"status": "unsupported", "path": None, "error": "no GUI session"})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/native-directory-dialog")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unsupported"
    assert body["path"] is None
    assert body["error"] == "no GUI session"


async def test_native_dialog_unknown_host_returns_404(
    dialog_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """Requesting a dialog on an unknown host returns 404."""
    app, _reg, _hs, _cs = dialog_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/00000000000000000000000000000000/native-directory-dialog",
        )

    assert resp.status_code == 404


async def test_native_dialog_offline_host_returns_409(
    dialog_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A registered-but-offline host returns 409 (not 404/502).

    The host row exists in the store (so not 404) but no live tunnel
    lands on this replica, so the dialog can't be forwarded → 409
    Conflict, matching the filesystem endpoints' offline posture.
    """
    app, _reg, host_store, _cs = dialog_app
    # Persist the host row without connecting a tunnel (mirrors the
    # filesystem endpoint's offline test): the record exists so the route
    # passes the 404 existence check, but registry.get() is None → 409.
    host_store.upsert_on_connect(
        host_id=_HOST_ID,
        name=_HOST_NAME,
        user_id="local",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/native-directory-dialog")

    assert resp.status_code == 409


# # ── Capability surfacing ────────────────────────────────


async def test_list_hosts_surfaces_advertised_capabilities(
    dialog_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """``GET /v1/hosts`` echoes the live capabilities a host advertised.

    The Web UI gates the "Use system dialog" button on
    ``capabilities.native_directory_dialog``; if the field is dropped or
    read from the DB instead of the live registry, the button would
    never appear (or appear for a host on another replica).
    """
    app, registry, _hs, _cs = dialog_app
    # Connect a host that advertises the native-dialog capability.
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": _hello_text(capabilities={"native_directory_dialog": True}),
        }
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts")

    assert resp.status_code == 200
    hosts = resp.json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["capabilities"] == {"native_directory_dialog": True}

    # Also verify the single-host endpoint surfaces it.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}")
    assert resp.status_code == 200
    assert resp.json()["capabilities"] == {"native_directory_dialog": True}


async def test_list_hosts_capabilities_none_for_older_host(
    dialog_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """An older host (no capabilities) reads as ``None`` in the list.

    Backward compat: the Web UI treats ``null``/absent as "no native
    dialog" and falls back to the in-app picker — never a crash.
    """
    app, registry, _hs, _cs = dialog_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {"type": "websocket.receive", "text": _hello_text()},  # no capabilities
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts")

    assert resp.status_code == 200
    hosts = resp.json()["hosts"]
    assert len(hosts) == 1
    assert hosts[0]["capabilities"] is None


# ── Capability enforcement (BLOCKER 2a) ──────────────────


async def test_native_dialog_rejects_when_capability_absent(
    dialog_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """The route rejects forwarding when the host did NOT advertise support.

    A host connected without ``native_directory_dialog`` (older host,
    non-macOS, headless, or a GUI host a remote user selected) must get a
    422 immediately — the server never forwards a dialog to a host that
    can't show one. The Web UI falls back to the in-app picker on 422.
    """
    app, registry, _hs, _cs = dialog_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    # Connect with NO capabilities (mimics an older / unsupported host).
    await comm.send_input(
        {"type": "websocket.receive", "text": _hello_text()},
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/native-directory-dialog")
    assert resp.status_code == 422
    assert "does not support" in resp.json()["detail"]


# ── Disconnect fails the pending future fast (BLOCKER 1) ──────────


async def test_native_dialog_endpoint_fails_fast_on_host_disconnect(
    dialog_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A host drop mid-dialog fails the endpoint promptly (no 300s hang).

    The drain does NOT reply (the user is still browsing). When the WS
    disconnects, deregister fails the pending future with a ConnectionError
    that the proxy maps to 502 — the endpoint returns within the test's
    short budget instead of waiting the full 300s timeout.
    """
    app, registry, _hs, _cs = dialog_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {
            "type": "websocket.receive",
            "text": _hello_text(capabilities={"native_directory_dialog": True}),
        },
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    # Drain outbound frames WITHOUT replying — the dialog is "open" and
    # the endpoint's future is pending.
    stop_drain = asyncio.Event()

    async def _drain_no_reply() -> None:
        while not stop_drain.is_set():
            try:
                await comm.receive_output(timeout=0.2)
            except asyncio.TimeoutError:
                continue

    drain_task = asyncio.create_task(_drain_no_reply())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            request_task = asyncio.create_task(
                client.post(f"/v1/hosts/{_HOST_ID}/native-directory-dialog"),
            )
            # Let the request reach the pending future.
            await asyncio.sleep(0.1)
            assert registry.get(_HOST_ID) is not None
            # Disconnect the host — deregister fails the pending future.
            await comm.send_input({"type": "websocket.disconnect"})
            resp = await asyncio.wait_for(request_task, timeout=5.0)
        # The endpoint must return promptly (502 connection-lost or 504),
        # NOT hang to the 300s timeout. The disconnect path may surface as
        # 502 (ConnectionError from the failed future) — both are a fast
        # failure, which is the contract under test.
        assert resp.status_code in (502, 504), resp.status_code
    finally:
        stop_drain.set()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(drain_task, timeout=1.0)
