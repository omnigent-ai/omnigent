"""
Integration tests for ``POST /v1/hosts/{id}/harnesses/{harness}/install``.

Wires up a real host tunnel + REST router pair, drives a fake host that
auto-replies to ``host.install_harness`` frames, and exercises the
endpoint's contract end-to-end. Mirrors ``test_hosts_create_directory.py``
(the create-folder action) — installing a harness shares the same
owner-scoped, host-forwarded design.

These are the executable acceptance criteria for Milestone 1 of the
"Setup From the UI" project: turning the dead-end "binary missing"
warning into a working Install action. The route is gated behind
``OMNIGENT_HARNESS_INSTALL_ENABLED``; the fixture enables it so the
happy-path and validation cases can run, and one test asserts the route
is 404 (invisible) when the flag is off.
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
    HostInstallHarnessFrame,
    HostInstallHarnessResultFrame,
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

_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_HOST_NAME = "install-test-laptop"


@pytest.fixture(autouse=True)
def _enable_install_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the feature flag for every test except the flag-off case.

    The route is invisible (404) unless ``OMNIGENT_HARNESS_INSTALL_ENABLED``
    is truthy; the happy-path and validation tests need it on.
    """
    monkeypatch.setenv("OMNIGENT_HARNESS_INSTALL_ENABLED", "1")


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
def install_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """
    App with host tunnel + REST routes for install-harness tests.

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
async def install_setup(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
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
    Connect a mock host and start an auto-replier for install_harness frames.

    Tests register fake replies in ``replies`` (harness → reply dict)
    before calling the REST endpoint. The auto-replier consumes the
    ``host.install_harness`` frames the route pushes through the
    registry, decodes them, and feeds the configured result back —
    mirroring what ``host_tunnel.py`` does in production. An unregistered
    harness defaults to a successful install that flips the harness to
    ready in the returned readiness map.

    :param install_app: The fixture above.
    :returns: Async iterator yielding the wired-up state.
    """
    app, registry, _hs, _cs = install_app
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
        """Drain outbound WS frames and reply to install_harness frames.

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
            if not isinstance(frame, HostInstallHarnessFrame):
                continue
            reply = replies.get(frame.harness)
            if reply is None:
                # Default: success, harness flips to ready in the
                # recomputed readiness map the host returns.
                reply_frame = HostInstallHarnessResultFrame(
                    request_id=frame.request_id,
                    status="ok",
                    configured_harnesses={frame.harness: True},
                )
            else:
                reply_frame = HostInstallHarnessResultFrame(
                    request_id=frame.request_id,
                    status=reply.get("status", "ok"),
                    configured_harnesses=reply.get("configured_harnesses"),
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


async def test_install_harness_returns_refreshed_readiness(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    A successful install returns the harness flipped to ready.

    The New Chat dialog reads this refreshed readiness to swap the
    Install button back to a ready badge without waiting for a
    reconnect, so the flipped ``configured_harnesses`` entry must
    round-trip through the endpoint.
    """
    app, _reg, _comm, replies, _drain = install_setup
    replies["claude"] = {
        "status": "ok",
        "configured_harnesses": {"claude": True, "claude-native": True},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "harness_install"
    assert body["harness"] == "claude"
    assert body["configured_harnesses"]["claude"] is True


async def test_install_harness_codex_reports_needs_auth_not_ready(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    Installing codex succeeds but readiness stays ``"needs-auth"``.

    codex-native is auth-gated: the binary installs, but readiness only
    flips to ready once a credential is configured (Milestone 2). M1 must
    faithfully surface the intermediate ``"needs-auth"`` state rather than
    pretending the harness is ready.
    """
    app, _reg, _comm, replies, _drain = install_setup
    replies["codex"] = {
        "status": "ok",
        "configured_harnesses": {"codex": "needs-auth", "codex-native": "needs-auth"},
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/install")

    assert resp.status_code == 200
    body = resp.json()
    assert body["configured_harnesses"]["codex"] == "needs-auth"


# ── Feature flag ────────────────────────────────────────


async def test_install_harness_route_hidden_when_flag_off(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With the flag off the route is 404 — the feature is invisible.

    Ships dark by default; only opt-in deployments expose it.
    """
    monkeypatch.setenv("OMNIGENT_HARNESS_INSTALL_ENABLED", "0")
    app, _reg, _hs, _cs = install_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")

    assert resp.status_code == 404


# ── Allowlist enforcement ───────────────────────────────


@pytest.mark.parametrize("harness", ["cursor", "goose", "gemini", "kimi", "hermes"])
async def test_install_harness_rejects_non_allowlisted(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
    harness: str,
) -> None:
    """
    A non-allowlisted harness is rejected with 400 before any frame.

    M1 only supports npm-installable, key/env-auth harnesses; OAuth and
    curl/brew-hint harnesses (notably hermes, whose installer is a
    ``curl | bash``) must be refused server-side so the UI cannot trigger
    an unsupported — or unsafe — install.
    """
    app, _reg, _comm, _replies, _drain = install_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/{harness}/install")

    assert resp.status_code == 400


@pytest.mark.parametrize("harness", ["claude", "codex", "pi", "opencode", "qwen"])
async def test_install_harness_allows_npm_key_auth_harnesses(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
    harness: str,
) -> None:
    """
    Every M1 allowlisted harness is accepted and installs.

    Pins the exact allowlist (claude, codex, pi, opencode, qwen) so a
    future edit that drops one is caught.
    """
    app, _reg, _comm, _replies, _drain = install_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/{harness}/install")

    assert resp.status_code == 200
    assert resp.json()["configured_harnesses"][harness] is True


# ── Failure surfaces ────────────────────────────────────


async def test_install_harness_failed_status_returns_502(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """
    A host-side install failure maps to 502 with the host's message.

    The dialog surfaces this inline so the user sees why the install
    failed rather than a silent no-op.
    """
    app, _reg, _comm, replies, _drain = install_setup
    replies["codex"] = {"status": "failed", "error": "npm registry unreachable"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/install")

    assert resp.status_code == 502
    assert "npm registry unreachable" in resp.json()["detail"]


async def test_install_harness_unknown_host_returns_404(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Installing on an unknown host returns 404 (don't leak existence).
    """
    app, _reg, _hs, _cs = install_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/7139b7e896ef9478abca6480107d1677/harnesses/claude/install"
        )

    assert resp.status_code == 404


async def test_install_harness_offline_host_returns_409(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Installing on a registered-but-offline host returns 409.

    A host row can exist in the store while no live tunnel connection is
    present; the install needs a live connection to forward the frame.
    """
    app, _reg, host_store, _cs = install_app
    # Persist a host row without a live registry connection.
    host_store.upsert_on_connect(
        host_id=_HOST_ID,
        name=_HOST_NAME,
        user_id="local",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")

    assert resp.status_code == 409
