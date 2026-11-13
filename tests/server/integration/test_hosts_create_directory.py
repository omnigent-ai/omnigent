"""
Integration tests for ``POST /v1/hosts/{id}/directories`` and
``POST /v1/hosts/{id}/repositories/clone``.

Wires up a real host tunnel + REST router pair, drives a fake host
that auto-replies to ``host.create_dir`` and ``host.clone_repository``
frames, and exercises each endpoint's contract end-to-end. Mirrors the
structure of ``test_hosts_filesystem.py`` (the browse endpoint) — the
create-folder and clone actions share the same owner-scoped,
host-forwarded design.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from omnigent.host.frames import (
    CAP_CLONE_REPOSITORY,
    HostCloneRepositoryFrame,
    HostCloneRepositoryResultFrame,
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostHelloFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import hosts as hosts_routes
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore
from tests.server.helpers import websocket_scope as _websocket_scope

# Same liveness-race flake guard as test_hosts_filesystem.py: the mock
# WS host can be starved + deregistered under parallel CI load. Tests
# are sub-second; rerun rather than fail.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "f28abbfd54a8b22204184b69d9bfd49f"
_HOST_NAME = "mkdir-test-laptop"


def _hello_text(name: str = _HOST_NAME, capabilities: list[str] | None = None) -> str:
    """Encode a hello frame for tests.

    :param name: Host name reported in the hello frame.
    :param capabilities: Capability tokens the host advertises; ``None`` mimics an
        older host that predates them.
    :returns: JSON-encoded hello frame.
    """
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
            capabilities=capabilities or [],
        )
    )


async def _connect_mock_host(
    app: FastAPI,
    registry: HostRegistry,
    capabilities: list[str] | None = None,
) -> ApplicationCommunicator:
    """Connect the mock host tunnel and wait until the registry holds it.

    :param app: App whose tunnel route the mock host dials.
    :param registry: Registry the tunnel registers into.
    :param capabilities: Capabilities the hello frame advertises.
    :returns: The communicator driving the tunnel socket.
    """
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {"type": "websocket.receive", "text": _hello_text(capabilities=capabilities)}
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)
    return comm


@pytest.fixture()
def mkdir_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """
    App with host tunnel + REST routes for create-directory tests.

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
async def mkdir_setup(
    mkdir_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
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
    Connect a mock host and start an auto-replier for create_dir frames.

    Tests register fake replies in ``replies`` (path → reply dict)
    before calling the REST endpoint. The auto-replier consumes the
    ``host.create_dir`` frames the route pushes through the registry,
    decodes them, and feeds the configured result back — mirroring what
    ``host_tunnel.py`` does in production. An unregistered path defaults
    to a successful create echoing the requested path.

    :param mkdir_app: The fixture above.
    :returns: Async iterator yielding the wired-up state.
    """
    app, registry, _hs, _cs = mkdir_app
    comm = await _connect_mock_host(app, registry)
    conn = registry.get(_HOST_ID)
    assert conn is not None
    replies: dict[str, dict[str, Any]] = {}

    async def _drain() -> None:
        """Reply to create-dir frames until fixture teardown cancels the drain."""
        while True:
            output = await comm.receive_output(timeout=None)
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if not isinstance(frame, HostCreateDirFrame):
                continue
            reply = replies.get(frame.path)
            if reply is None:
                # Default: success, echoing the requested path (the host
                # would return the created absolute path).
                reply_frame = HostCreateDirResultFrame(
                    request_id=frame.request_id,
                    status="ok",
                    path=frame.path,
                )
            else:
                reply_frame = HostCreateDirResultFrame(
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
        yield app, registry, comm, replies, drain_task
    finally:
        drain_task.cancel()
        try:
            with suppress(asyncio.CancelledError):
                await drain_task
        finally:
            await comm.send_input({"type": "websocket.disconnect", "code": 1000})
            await comm.wait(timeout=5.0)


# ── Happy path ──────────────────────────────────────────


async def test_create_directory_survives_idle_mock_host(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """The mock host remains connected while no requests are in flight."""
    app, registry, comm, _replies, _drain = mkdir_setup
    await asyncio.sleep(0.75)
    assert not comm.future.done()
    assert registry.get(_HOST_ID) is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories", json={"path": "/projects/new-app"}
        )
    assert response.status_code == 200, response.text
    assert response.json()["path"] == "/projects/new-app"


async def test_create_directory_returns_created_path(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    A valid create request returns the created absolute path.

    This is what the picker navigates into after creating the folder,
    so the path must round-trip through the endpoint intact.
    """
    app, _reg, _comm, replies, _drain = mkdir_setup
    target = "/Users/corey/projects/new-app"
    replies[target] = {"status": "ok", "path": target}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories",
            json={"path": target},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "directory"
    assert body["path"] == target


async def test_create_directory_already_exists_returns_409(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    An "already exists" host result maps to 409 with the message.

    The picker shows this inline so the user knows the name is taken
    rather than seeing a generic failure.
    """
    app, _reg, _comm, replies, _drain = mkdir_setup
    target = "/Users/corey/projects/dup"
    replies[target] = {"status": "ok", "error": "directory already exists"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories",
            json={"path": target},
        )

    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


async def test_create_directory_relative_path_rejected(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    A relative path is rejected with 400 before reaching the host.

    The host needs a path it can resolve on its own; a relative path
    has no stable meaning across host process cwds.
    """
    app, _reg, _comm, _replies, _drain = mkdir_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories",
            json={"path": "relative/dir"},
        )

    assert resp.status_code == 400


async def test_create_directory_unknown_host_returns_404(
    mkdir_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Creating under an unknown host returns 404 (don't leak existence).
    """
    app, _reg, _hs, _cs = mkdir_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/7139b7e896ef9478abca6480107d1677/directories",
            json={"path": "/tmp/x"},
        )

    assert resp.status_code == 404


async def test_create_directory_windows_drive_path_accepted(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """A Windows drive path is absolute and must not 400 as relative."""
    app, _reg, _comm, _replies, _drain = mkdir_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/directories",
            json={"path": r"C:\Users\alice\work\fresh"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == r"C:\Users\alice\work\fresh"


async def _answer_clone_frames(
    comm: ApplicationCommunicator,
    replies: dict[str, dict[str, Any]],
    received: list[HostCloneRepositoryFrame],
) -> None:
    """Reply to clone frames as the host would, recording each request.

    A reply with ``drop`` stays silent (to exercise the timeout); one with
    ``disconnect`` drops the tunnel (to exercise transport loss).

    :param comm: The mock host's tunnel communicator.
    :param replies: Clone result per destination path, e.g. ``{"/x": {"error_code": ...}}``.
    :param received: Every decoded clone request, in arrival order.
    """
    while True:
        output = await comm.receive_output(timeout=None)
        text = output.get("text") if output.get("type") == "websocket.send" else None
        if not isinstance(text, str):
            continue
        frame = decode_host_frame(text)
        if not isinstance(frame, HostCloneRepositoryFrame):
            continue
        received.append(frame)
        reply = replies.get(frame.path, {})
        if reply.get("drop"):
            continue
        if reply.get("disconnect"):
            await comm.send_input({"type": "websocket.disconnect", "code": 1000})
            return
        result = HostCloneRepositoryResultFrame(
            request_id=frame.request_id,
            status=reply.get("status", "ok"),
            path=reply.get("path", frame.path),
            error=reply.get("error"),
            error_code=reply.get("error_code"),
        )
        await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(result)})


@pytest.fixture()
async def clone_setup(
    mkdir_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> AsyncIterator[
    tuple[
        FastAPI,
        HostRegistry,
        dict[str, dict[str, Any]],
        list[HostCloneRepositoryFrame],
    ]
]:
    """Connect a mock host that advertises clone support and answers clone frames.

    :param mkdir_app: The app fixture above.
    :returns: Async iterator yielding ``(app, registry, replies, received)``.
    """
    app, registry, _hs, _cs = mkdir_app
    comm = await _connect_mock_host(app, registry, [CAP_CLONE_REPOSITORY])
    replies: dict[str, dict[str, Any]] = {}
    received: list[HostCloneRepositoryFrame] = []
    drain_task = asyncio.create_task(_answer_clone_frames(comm, replies, received))
    try:
        yield app, registry, replies, received
    finally:
        drain_task.cancel()
        with suppress(asyncio.CancelledError):
            await drain_task
        if not comm.future.done():
            await comm.send_input({"type": "websocket.disconnect", "code": 1000})
            await comm.wait(timeout=5.0)


_CLONE_URL = "https://git.example.com/team/app.git"
_CLONE_PATH = "/Users/alice/code/app"


async def _post_clone(app: FastAPI, body: dict[str, Any]) -> Any:
    """POST a clone request to the test host.

    :param app: The app under test.
    :param body: JSON body for the clone route.
    :returns: The HTTP response.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/v1/hosts/{_HOST_ID}/repositories/clone", json=body)


async def test_clone_repository_returns_checkout_path(
    clone_setup: tuple[
        FastAPI, HostRegistry, dict[str, dict[str, Any]], list[HostCloneRepositoryFrame]
    ],
) -> None:
    """A successful clone returns the checkout path and sends a trimmed request."""
    app, _registry, _replies, received = clone_setup
    response = await _post_clone(
        app,
        {"url": f"  {_CLONE_URL}  ", "branch": " main ", "path": _CLONE_PATH},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"object": "repository", "path": _CLONE_PATH}
    assert [(f.url, f.branch, f.path) for f in received] == [(_CLONE_URL, "main", _CLONE_PATH)]


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        ("invalid_input", 400),
        ("clone_destination_conflict", 409),
        ("clone_timeout", 504),
        ("clone_failed", 502),
    ],
)
async def test_clone_repository_maps_host_error_codes(
    clone_setup: tuple[
        FastAPI, HostRegistry, dict[str, dict[str, Any]], list[HostCloneRepositoryFrame]
    ],
    error_code: str,
    status: int,
) -> None:
    """Each host failure class maps to its HTTP status with the host's message."""
    app, _registry, replies, _received = clone_setup
    replies[_CLONE_PATH] = {"status": "failed", "error": "host said no", "error_code": error_code}
    response = await _post_clone(app, {"url": _CLONE_URL, "path": _CLONE_PATH})
    assert response.status_code == status, response.text
    assert "host said no" in response.json()["detail"]


async def test_clone_repository_on_old_host_is_409_without_a_frame(
    mkdir_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """A host that predates clone support is refused up front and never sent a frame."""
    app, registry, _comm, _replies, _drain = mkdir_setup
    response = await _post_clone(app, {"url": _CLONE_URL, "path": _CLONE_PATH})
    assert response.status_code == 409
    assert "Update this host to clone repositories" in response.json()["detail"]
    conn = registry.get(_HOST_ID)
    assert conn is not None and conn.pending_clones == {}


async def test_clone_repository_timeout_is_504(
    clone_setup: tuple[
        FastAPI, HostRegistry, dict[str, dict[str, Any]], list[HostCloneRepositoryFrame]
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that never answers yields 504 and leaves no pending clone behind."""
    app, registry, replies, _received = clone_setup
    monkeypatch.setattr(hosts_routes, "_CLONE_REPOSITORY_TIMEOUT_S", 0.05)
    replies[_CLONE_PATH] = {"drop": True}
    response = await _post_clone(app, {"url": _CLONE_URL, "path": _CLONE_PATH})
    assert response.status_code == 504
    assert "check the destination" in response.json()["detail"]
    conn = registry.get(_HOST_ID)
    assert conn is not None and conn.pending_clones == {}


async def test_clone_repository_transport_loss_is_502(
    clone_setup: tuple[
        FastAPI, HostRegistry, dict[str, dict[str, Any]], list[HostCloneRepositoryFrame]
    ],
) -> None:
    """A tunnel that drops mid-clone fails the request with 502, not a hang."""
    app, _registry, replies, _received = clone_setup
    replies[_CLONE_PATH] = {"disconnect": True}
    response = await _post_clone(app, {"url": _CLONE_URL, "path": _CLONE_PATH})
    assert response.status_code == 502
    assert "the clone may still finish" in response.json()["detail"]


async def test_clone_repository_rejects_bad_input_before_sending(
    clone_setup: tuple[
        FastAPI, HostRegistry, dict[str, dict[str, Any]], list[HostCloneRepositoryFrame]
    ],
) -> None:
    """Credentials, relative paths and unsafe branches are refused without contacting the host."""
    app, _registry, _replies, received = clone_setup
    secret = "s3cr3t"
    cases = [
        {"url": f"https://alice:{secret}@git.example.com/team/app.git", "path": _CLONE_PATH},
        {"url": _CLONE_URL, "path": "relative/app"},
        {"url": _CLONE_URL, "branch": "-upload-pack=x", "path": _CLONE_PATH},
        {"url": _CLONE_URL, "path": "   "},
    ]
    for body in cases:
        response = await _post_clone(app, body)
        assert response.status_code == 400, body
        assert secret not in response.text
    assert received == []
