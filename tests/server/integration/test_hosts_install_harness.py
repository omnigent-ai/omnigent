"""
Integration tests for ``POST /v1/hosts/{id}/harnesses/{harness}/install``.

Wires up a real host tunnel + REST router pair, drives a fake host that
auto-replies to ``host.install_harness`` frames, and exercises the
endpoint's contract end-to-end. Mirrors ``test_hosts_create_directory.py``
(the create-folder action) — installing a harness shares the same
owner-scoped, host-forwarded design.

These are the executable acceptance criteria for Milestone 1 of the
"Setup From the UI" project: turning the dead-end "binary missing"
warning into a working Install action. The route is gated by
``harness_install`` in ``OMNIGENT_FEATURES``; the fixture enables it so the
happy-path and validation cases can run, and one test asserts the route is 404
(invisible) when the flag is off.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    CAP_HARNESS_ACCOUNTS,
    CAP_HARNESS_LOGIN,
    CAP_REFRESH_HARNESSES,
    HostHarnessLoginCancelFrame,
    HostHarnessLoginCancelResultFrame,
    HostHarnessLoginCompletedFrame,
    HostHarnessLoginFrame,
    HostHarnessLoginResultFrame,
    HostHarnessLogoutFrame,
    HostHarnessLogoutResultFrame,
    HostHelloFrame,
    HostInstallHarnessFrame,
    HostInstallHarnessResultFrame,
    HostProbeAccountsFrame,
    HostProbeAccountsResultFrame,
    HostRefreshHarnessesFrame,
    HostRefreshHarnessesResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.feature_flags import FeatureFlags
from omnigent.server.host_registry import HarnessLoginState, HostRegistry
from omnigent.server.routes import hosts as hosts_module
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests._helpers.polling import poll_until_async
from tests.server.helpers import websocket_scope as _websocket_scope

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

    The route is invisible (404) unless ``harness_install`` is in the enabled
    feature set; the happy-path and validation tests need it on.
    """
    monkeypatch.setenv("OMNIGENT_FEATURES", "harness_install")


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
            configured_harnesses={"codex": True, "claude": True},
            harness_accounts={"codex": {"kind": "chatgpt"}},
            capabilities=[
                CAP_REFRESH_HARNESSES,
                CAP_HARNESS_ACCOUNTS,
                CAP_HARNESS_LOGIN,
            ],
        )
    )


async def _connect_mock_host(app: FastAPI, registry: HostRegistry) -> ApplicationCommunicator:
    """Open a tunnel, complete the hello handshake, and wait for registration.

    :param app: The wired FastAPI app (tunnel + REST routers).
    :param registry: The registry the tunnel registers the connection into.
    :returns: The connected ``ApplicationCommunicator`` (caller drains it).
    """
    comm = ApplicationCommunicator(app, _websocket_scope(f"/v1/hosts/{_HOST_ID}/tunnel"))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input({"type": "websocket.receive", "text": _hello_text()})
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)
    return comm


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

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        """Convert application errors to structured JSON responses."""
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
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
    comm = await _connect_mock_host(app, registry)

    conn = registry.get(_HOST_ID)
    assert conn is not None
    replies: dict[str, dict[str, Any]] = {}

    async def _drain() -> None:
        """Reply to install frames until fixture teardown cancels the drain."""
        while True:
            output = await comm.receive_output(timeout=None)
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if isinstance(frame, HostProbeAccountsFrame):
                probe_reply = replies.get("__accounts__", {})
                probe_reply["count"] = int(probe_reply.get("count", 0)) + 1
                replies["__accounts__"] = probe_reply
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostProbeAccountsResultFrame(
                                request_id=frame.request_id,
                                status=probe_reply.get("status", "ok"),
                                harness_accounts=probe_reply.get(
                                    "harness_accounts",
                                    {"codex": {"kind": "chatgpt"}},
                                ),
                                error=probe_reply.get("error"),
                            )
                        ),
                    }
                )
                continue
            if isinstance(frame, HostHarnessLoginFrame):
                reply = replies.get("__login__", {})
                replies.setdefault("__started__", {})[frame.login_id] = True
                replies.setdefault("__login_harnesses__", {})[frame.harness] = True
                if reply.get("drop"):
                    continue
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostHarnessLoginResultFrame(
                                request_id=frame.request_id,
                                status=reply.get("status", "ok"),
                                login_id=reply.get("login_id", frame.login_id),
                                verification_url=reply.get(
                                    "verification_url", "https://example.test/device"
                                ),
                                user_code=reply.get("user_code", "ABCD-EFGH"),
                                expires_at=reply.get("expires_at", 2_000_000_000),
                                error=reply.get("error"),
                            )
                        ),
                    }
                )
                continue
            if isinstance(frame, HostHarnessLoginCancelFrame):
                replies.setdefault("__cancelled__", {})[frame.login_id] = True
                cancel_reply = replies.get("__cancel__", {})
                completion = cancel_reply.get("completion")
                if isinstance(completion, dict):
                    await comm.send_input(
                        {
                            "type": "websocket.receive",
                            "text": encode_host_frame(
                                HostHarnessLoginCompletedFrame(
                                    login_id=frame.login_id,
                                    success=completion.get("success", True),
                                    expired=completion.get("expired", False),
                                    error=completion.get("error"),
                                )
                            ),
                        }
                    )
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostHarnessLoginCancelResultFrame(
                                request_id=frame.request_id,
                                status=cancel_reply.get("status", "ok"),
                                error=cancel_reply.get("error"),
                            )
                        ),
                    }
                )
                continue
            if isinstance(frame, HostHarnessLogoutFrame):
                reply = replies.get("__logout__")
                replies.setdefault("__logout_harnesses__", {})[frame.harness] = True
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostHarnessLogoutResultFrame(
                                request_id=frame.request_id,
                                status="ok",
                                configured_harnesses=(
                                    reply.get("configured_harnesses")
                                    if reply is not None
                                    else {frame.harness: True}
                                ),
                                harness_accounts=(
                                    reply.get("harness_accounts")
                                    if reply is not None
                                    else {frame.harness: {"kind": "unknown", "expired": True}}
                                ),
                            )
                        ),
                    }
                )
                continue
            if isinstance(frame, HostRefreshHarnessesFrame):
                reply = replies.get("__refresh__", {})
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostRefreshHarnessesResultFrame(
                                request_id=frame.request_id,
                                status=reply.get("status", "ok"),
                                configured_harnesses=reply.get(
                                    "configured_harnesses", {"codex-native": True}
                                ),
                                harness_accounts=reply.get("harness_accounts"),
                                gateway_inference=reply.get(
                                    "gateway_inference", {"codex-native": False}
                                ),
                                error=reply.get("error"),
                            )
                        ),
                    }
                )
                continue
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
                    harness_accounts=reply.get("harness_accounts"),
                    version=reply.get("version"),
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


async def test_install_harness_survives_idle_mock_host(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """An idle mock host stays connected until fixture teardown."""
    app, registry, comm, _replies, _drain = install_setup
    await asyncio.sleep(0.75)
    assert not comm.future.done()
    assert registry.get(_HOST_ID) is not None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")
    assert response.status_code == 200, response.text
    assert response.json()["configured_harnesses"]["claude"] is True


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
        "version": "2.1.295",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "harness_install"
    assert body["harness"] == "claude"
    assert body["configured_harnesses"]["claude"] is True
    assert body["version"] == "2.1.295"


async def test_install_harness_tolerates_a_garbled_gateway_inference(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A host that answers with a non-mapping ``gateway_inference`` must not 500.

    The reply is host-supplied, so it goes through the same tolerant decode the
    tunnel path uses: anything that isn't a string→bool object reads as
    "unknown". Recording it straight would have blown up in ``dict(...)``.

    The garbled value is injected at the proxy's return, not through the mock
    host: the frame decoder normalises it on the way in, so a fixture reply
    could never reach the route with it still garbled.
    """
    app, registry, _comm, _replies, _drain = install_setup

    async def _garbled_reply(**_kwargs: Any) -> dict[str, Any]:
        """A host reply whose gateway_inference is a list, not a map."""
        return {
            "status": "ok",
            "configured_harnesses": {"claude": True},
            "gateway_inference": ["claude-native"],
        }

    monkeypatch.setattr(
        "omnigent.server.routes.hosts._proxy_install_harness",
        _garbled_reply,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")

    assert resp.status_code == 200, resp.text
    assert resp.json()["gateway_inference"] is None
    assert registry.gateway_inference(_HOST_ID) is None


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


# ── Coalescing concurrent installs ──────────────────────


async def test_install_coalesces_concurrent_same_family(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two overlapping installs of one family reach the host as a single frame.

    ``codex`` and ``codex-native`` both resolve to the ``openai`` install key,
    so a user who fires both (a double-click, or two spellings) must not drive
    two concurrent global ``npm install -g`` runs — npm's global writes aren't
    race-safe. The route coalesces them onto one in-flight task keyed on the
    resolved family, so exactly one ``host.install_harness`` frame is sent and
    both HTTP callers get the same result.
    """
    app, registry, _hs, _cs = install_app
    comm = await _connect_mock_host(app, registry)
    conn = registry.get(_HOST_ID)
    assert conn is not None

    install_frames: list[str] = []
    first_install_arrived = asyncio.Event()
    second_install_arrived = asyncio.Event()
    release = asyncio.Event()
    original_install_key = hosts_module.ui_install_key

    def observe_install_key(harness: str) -> str | None:
        install_key = original_install_key(harness)
        if harness == "codex-native":
            second_install_arrived.set()
        return install_key

    monkeypatch.setattr(hosts_module, "ui_install_key", observe_install_key)

    async def _drain_holding_reply() -> None:
        """Record each install frame, then reply once ``release`` is set.

        Holding the reply keeps the shared task in flight so a second
        request lands while the first is still pending — exactly the
        window coalescing must cover.
        """
        while True:
            output = await comm.receive_output(timeout=None)
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if not isinstance(frame, HostInstallHarnessFrame):
                continue
            install_frames.append(frame.harness)
            first_install_arrived.set()
            await release.wait()
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostInstallHarnessResultFrame(
                            request_id=frame.request_id,
                            status="ok",
                            configured_harnesses={frame.harness: "needs-auth"},
                        )
                    ),
                }
            )

    try:
        async with (
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
            asyncio.TaskGroup() as tasks,
        ):
            drain_task = tasks.create_task(_drain_holding_reply())
            first = tasks.create_task(client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/install"))
            await asyncio.wait_for(first_install_arrived.wait(), timeout=5.0)
            assert "openai" in conn.inflight_installs
            second = tasks.create_task(
                client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex-native/install")
            )
            await asyncio.wait_for(second_install_arrived.wait(), timeout=5.0)
            assert not first.done()
            assert not second.done()
            release.set()
            resp_first, resp_second = await asyncio.wait_for(
                asyncio.gather(first, second), timeout=5.0
            )
            drain_task.cancel()
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait(timeout=5.0)

    # Exactly one frame reached the host despite two concurrent requests.
    assert install_frames == ["codex"]
    assert resp_first.status_code == 200
    assert resp_second.status_code == 200
    # Both callers echo their own requested harness but share the one coalesced
    # readiness map (keyed on the harness that actually reached the host).
    assert resp_first.json()["harness"] == "codex"
    assert resp_second.json()["harness"] == "codex-native"
    assert resp_first.json()["configured_harnesses"]["codex"] == "needs-auth"
    assert resp_second.json()["configured_harnesses"]["codex"] == "needs-auth"


# ── Feature flag ────────────────────────────────────────


async def test_refresh_harnesses_returns_fresh_host_maps(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, _registry, _comm, replies, _drain = install_setup
    replies["__refresh__"] = {
        "configured_harnesses": {
            "claude-native": "needs-auth",
            "codex-native": True,
        },
        "harness_accounts": {},
        "gateway_inference": {
            "claude-native": True,
            "codex-native": False,
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/refresh")

    assert resp.status_code == 200
    assert resp.json() == {
        "configured_harnesses": {
            "claude-native": "needs-auth",
            "codex-native": True,
        },
        "harness_accounts": {},
        "gateway_inference": {
            "claude-native": True,
            "codex-native": False,
        },
    }


async def test_refresh_harness_accounts_updates_hosts_and_throttles(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """A successful account probe updates list/detail and is cached for 60 seconds."""
    app, registry, _comm, replies, _drain = install_setup
    accounts = {"codex": {"kind": "chatgpt", "email": "person@example.test"}}
    replies["__accounts__"] = {"harness_accounts": accounts}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/accounts/refresh")
        second = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/accounts/refresh")
        conn = registry.get(_HOST_ID)
        assert conn is not None and conn.harness_accounts_probed_at is not None
        conn.harness_accounts_probed_at -= 61
        third = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/accounts/refresh")
        listing = await client.get("/v1/hosts")
        detail = await client.get(f"/v1/hosts/{_HOST_ID}")

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json() == second.json() == third.json() == {"harness_accounts": accounts}
    assert replies["__accounts__"]["count"] == 2
    assert listing.json()["hosts"][0]["harness_accounts"] == accounts
    assert detail.json()["harness_accounts"] == accounts


async def test_refresh_harness_accounts_rejects_unsupported_host(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, registry, _comm, _replies, _drain = install_setup
    conn = registry.get(_HOST_ID)
    assert conn is not None
    conn.hello.capabilities = [
        capability for capability in conn.hello.capabilities if capability != CAP_HARNESS_ACCOUNTS
    ]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/accounts/refresh")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "login_unsupported"


async def test_refresh_harness_accounts_rejects_offline_host(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, registry, _comm, _replies, _drain = install_setup
    registry.deregister(_HOST_ID)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/accounts/refresh")

    assert response.status_code == 409


async def test_refresh_harnesses_failed_host_result_returns_502(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, _registry, _comm, replies, _drain = install_setup
    replies["__refresh__"] = {
        "status": "failed",
        "error": "readiness refresh failed: probe exited",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/refresh")

    assert resp.status_code == 502
    assert (
        resp.json()["detail"]
        == "host readiness refresh failed: readiness refresh failed: probe exited"
    )


async def test_refresh_harnesses_route_hidden_when_flag_off(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    _app, registry, host_store, conv_store = install_app
    off_app = FastAPI()
    off_app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            feature_flags=FeatureFlags(),
        ),
        prefix="/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=off_app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/refresh")
        accounts_resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/accounts/refresh")

    assert resp.status_code == 404
    assert accounts_resp.status_code == 404


async def test_harness_install_callable_flips_route_without_restart(
    db_uri: str,
) -> None:
    """A deployment gate can reveal every setup route between requests."""
    enabled = False
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(
            registry,
            host_store,
            harness_install=lambda: enabled,
        ),
        prefix="/v1",
    )
    app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            harness_install=lambda: enabled,
        ),
        prefix="/v1",
    )
    comm = await _connect_mock_host(app, registry)
    conn = registry.get(_HOST_ID)
    assert conn is not None
    conn.harness_logins["login-1"] = HarnessLoginState(
        harness="codex",
        status="pending",
        expires_at=2**62,
    )

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            hidden = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/login-1")
            enabled = True
            visible = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/login-1")
            refresh_task = asyncio.create_task(
                client.post(f"/v1/hosts/{_HOST_ID}/harnesses/accounts/refresh")
            )
            output = await comm.receive_output(timeout=1)
            frame = decode_host_frame(output["text"])
            assert isinstance(frame, HostProbeAccountsFrame)
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostProbeAccountsResultFrame(
                            request_id=frame.request_id,
                            status="ok",
                            harness_accounts={"codex": {"kind": "chatgpt"}},
                        )
                    ),
                }
            )
            refreshed = await refresh_task
        assert hidden.status_code == 404
        assert visible.status_code == 200
        assert visible.json() == {"status": "pending", "error": None, "expired": False}
        assert refreshed.status_code == 200
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})


async def test_create_app_harness_install_callable_fails_closed_then_flips(
    db_uri: str,
    tmp_path: Path,
    runtime_init: None,
) -> None:
    """The production app shares one resilient gate across info and setup routes."""
    gate_state = "raise"

    def harness_install_gate() -> bool:
        if gate_state == "raise":
            raise RuntimeError("deployment flag unavailable")
        return gate_state == "on"

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    host_store = HostStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        host_store=host_store,
        harness_install=harness_install_gate,
    )
    registry = app.state.host_registry
    comm = await _connect_mock_host(app, registry)

    async def reply_to_setup_requests() -> None:
        while True:
            output = await comm.receive_output(timeout=None)
            if output.get("type") != "websocket.send" or not isinstance(output.get("text"), str):
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostInstallHarnessFrame):
                result = HostInstallHarnessResultFrame(
                    request_id=frame.request_id,
                    status="ok",
                    configured_harnesses={frame.harness: True},
                )
            elif isinstance(frame, HostRefreshHarnessesFrame):
                result = HostRefreshHarnessesResultFrame(
                    request_id=frame.request_id,
                    status="ok",
                    configured_harnesses={"claude": True},
                )
            else:
                continue
            await comm.send_input({"type": "websocket.receive", "text": encode_host_frame(result)})

    drain_task = asyncio.create_task(reply_to_setup_requests())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            closed_info = await client.get("/v1/info")
            closed_install = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")
            closed_refresh = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/refresh")

            gate_state = "on"
            open_info = await client.get("/v1/info")
            open_install = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")
            open_refresh = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/refresh")

        assert closed_info.status_code == 200
        assert closed_info.json()["features"]["harness_install"] is False
        assert closed_info.json()["harness_install_enabled"] is False
        assert closed_install.status_code == closed_refresh.status_code == 404
        assert open_info.status_code == 200
        assert open_info.json()["features"]["harness_install"] is True
        assert open_info.json()["harness_install_enabled"] is True
        assert open_install.status_code == open_refresh.status_code == 200
    finally:
        drain_task.cancel()
        with suppress(asyncio.CancelledError):
            await drain_task
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait(timeout=5.0)


async def test_refresh_harnesses_old_host_returns_unsupported_without_waiting(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, registry, _comm, _replies, _drain = install_setup
    conn = registry.get(_HOST_ID)
    assert conn is not None
    conn.hello.capabilities = []

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/refresh")

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "refresh_unsupported"


async def test_refresh_harnesses_offline_host_returns_409(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    app, _registry, host_store, _conv_store = install_app
    host_store.upsert_on_connect(host_id=_HOST_ID, name=_HOST_NAME, user_id="local")
    host_store.set_offline(_HOST_ID)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/refresh")

    assert resp.status_code == 409


async def test_refresh_harnesses_non_owner_returns_403(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    from omnigent.server.auth import AuthProvider

    _app, _registry, host_store, conv_store = install_app

    class _Stub(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return request.headers.get("X-Test-User")

    auth = _Stub()
    auth_app = FastAPI()
    registry = HostRegistry()
    auth_app.include_router(
        create_host_tunnel_router(registry, host_store, auth_provider=auth), prefix="/v1"
    )
    auth_app.include_router(
        create_hosts_router(registry, host_store, conv_store, auth_provider=auth), prefix="/v1"
    )
    host_store.upsert_on_connect(host_id=_HOST_ID, name=_HOST_NAME, user_id="alice@example.com")

    async with AsyncClient(
        transport=ASGITransport(app=auth_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/refresh",
            headers={"X-Test-User": "bob@example.com"},
        )

    assert resp.status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("POST", "login", id="start"),
        pytest.param("GET", "login/login-1", id="status"),
        pytest.param("DELETE", "login/login-1", id="cancel"),
        pytest.param("POST", "logout", id="logout"),
    ],
)
async def test_harness_account_routes_non_owner_return_403(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    method: str,
    path: str,
) -> None:
    from omnigent.server.auth import AuthProvider

    _app, _registry, host_store, conv_store = install_app

    class _Stub(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return request.headers.get("X-Test-User")

    auth = _Stub()
    auth_app = FastAPI()
    registry = HostRegistry()
    auth_app.include_router(
        create_host_tunnel_router(registry, host_store, auth_provider=auth), prefix="/v1"
    )
    auth_app.include_router(
        create_hosts_router(registry, host_store, conv_store, auth_provider=auth), prefix="/v1"
    )
    host_store.upsert_on_connect(
        host_id=_HOST_ID,
        name=_HOST_NAME,
        user_id="alice@example.com",
    )

    async with AsyncClient(
        transport=ASGITransport(app=auth_app), base_url="http://test"
    ) as client:
        response = await client.request(
            method,
            f"/v1/hosts/{_HOST_ID}/harnesses/codex/{path}",
            headers={"X-Test-User": "bob@example.com"},
        )

    assert response.status_code == 403


async def test_codex_login_start_status_completion_and_logout(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """Device details are returned once; polling exposes only status and accounts."""
    app, registry, comm, _replies, _drain = install_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
        assert started.status_code == 200, started.text
        login_id = started.json()["login_id"]
        assert started.json() == {
            "login_id": login_id,
            "verification_url": "https://example.test/device",
            "user_code": "ABCD-EFGH",
            "expires_at": 2_000_000_000,
        }

        pending = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/{login_id}")
        assert pending.json() == {"status": "pending", "error": None, "expired": False}

        await comm.send_input(
            {
                "type": "websocket.receive",
                "text": encode_host_frame(
                    HostHarnessLoginCompletedFrame(
                        login_id=login_id,
                        success=True,
                        configured_harnesses={"codex": True},
                        harness_accounts={
                            "codex": {
                                "kind": "chatgpt",
                                "email": "person@example.test",
                                "plan": "pro",
                                "expired": False,
                            }
                        },
                    )
                ),
            }
        )
        conn = registry.get(_HOST_ID)
        assert conn is not None
        await poll_until_async(
            lambda: conn.harness_logins[login_id].status != "pending",
            timeout_s=2,
            interval_s=0.01,
        )
        completed = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/{login_id}")
        assert completed.json() == {
            "status": "succeeded",
            "error": None,
            "expired": False,
            "configured_harnesses": {"codex": True},
            "harness_accounts": {
                "codex": {
                    "kind": "chatgpt",
                    "email": "person@example.test",
                    "plan": "pro",
                    "expired": False,
                }
            },
        }

        logged_out = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/logout")
        assert logged_out.json() == {
            "configured_harnesses": {"codex": True},
            "harness_accounts": {"codex": {"kind": "unknown", "expired": True}},
        }


async def test_codex_login_busy_and_cancel(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, _registry, _comm, _replies, _drain = install_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
        assert first.status_code == 200
        login_id = first.json()["login_id"]
        busy = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
        assert busy.status_code == 409
        assert busy.json() == {"code": "login_busy", "login_id": login_id}
        cancelled = await client.delete(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/{login_id}")
        assert cancelled.json() == {
            "status": "cancelled",
            "error": None,
            "expired": False,
        }


async def test_codex_login_cancel_warning_releases_busy_state(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """Confirmed local teardown is terminal even when vendor cancellation warns."""
    app, _registry, _comm, replies, _drain = install_setup
    replies["__cancel__"] = {
        "status": "ok",
        "error": "could not cancel login: vendor cancel failed",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
        login_id = first.json()["login_id"]
        cancelled = await client.delete(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/{login_id}")
        restarted = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")

    assert cancelled.json() == {
        "status": "cancelled",
        "error": "could not cancel login: vendor cancel failed",
        "expired": False,
    }
    assert restarted.status_code == 200
    assert restarted.json()["login_id"] != login_id


async def test_host_reported_busy_login_can_be_polled_and_cancelled(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """A host-owned busy id is adopted into server lifecycle state."""
    app, _registry, _comm, replies, _drain = install_setup
    replies["__login__"] = {"status": "busy", "login_id": "host-active-login"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        busy = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
        status = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/host-active-login")
        cancelled = await client.delete(
            f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/host-active-login"
        )

    assert busy.status_code == 409
    assert busy.json() == {"code": "login_busy", "login_id": "host-active-login"}
    assert status.json() == {"status": "pending", "error": None, "expired": False}
    assert cancelled.json() == {"status": "cancelled", "error": None, "expired": False}
    assert replies["__cancelled__"] == {"host-active-login": True}


async def test_login_completion_wins_cancel_not_found_race(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    """A completion received during cancel remains the authoritative terminal state."""
    app, _registry, _comm, replies, _drain = install_setup
    replies["__cancel__"] = {
        "status": "failed",
        "error": "login not found",
        "completion": {"success": True},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
        login_id = started.json()["login_id"]
        cancelled = await client.delete(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/{login_id}")

    assert cancelled.status_code == 200
    assert cancelled.json() == {"status": "succeeded", "error": None, "expired": False}


async def test_codex_native_login_uses_canonical_host_family(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, _registry, _comm, replies, _drain = install_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex-native/login")
        assert started.status_code == 200
        login_id = started.json()["login_id"]
        pending = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex-native/login/{login_id}")
        cancelled = await client.delete(
            f"/v1/hosts/{_HOST_ID}/harnesses/codex-native/login/{login_id}"
        )

    assert replies["__login_harnesses__"] == {"codex": True}
    assert pending.json() == {"status": "pending", "error": None, "expired": False}
    assert cancelled.json() == {
        "status": "cancelled",
        "error": None,
        "expired": False,
    }


async def test_codex_native_logout_uses_canonical_host_family(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, _registry, _comm, replies, _drain = install_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex-native/logout")

    assert response.status_code == 200
    assert replies["__logout_harnesses__"] == {"codex": True}


async def test_login_start_rejects_non_https_verification_url(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, registry, _comm, replies, _drain = install_setup
    replies["__login__"] = {"verification_url": "javascript:alert(1)"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")

    assert response.status_code == 502
    assert response.json()["detail"] == "host returned an invalid login URL"
    conn = registry.get(_HOST_ID)
    assert conn is not None
    assert conn.active_harness_logins == {}
    assert conn.harness_logins == {}


async def test_codex_login_start_timeout_sends_cancel_and_clears_busy(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, registry, _comm, replies, _drain = install_setup
    replies["__login__"] = {"drop": True}
    monkeypatch.setattr(hosts_module, "_HARNESS_LOGIN_START_TIMEOUT_S", 0.01)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")

    assert response.status_code == 504
    started_ids = set(replies["__started__"])
    assert started_ids
    assert set(replies["__cancelled__"]) == started_ids
    conn = registry.get(_HOST_ID)
    assert conn is not None
    assert conn.active_harness_logins == {}


async def test_logout_probe_failure_preserves_connected_host_snapshot(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, registry, _comm, replies, _drain = install_setup
    replies["__logout__"] = {
        "configured_harnesses": None,
        "harness_accounts": None,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/logout")

    assert response.status_code == 200
    assert response.json() == {
        "configured_harnesses": {"codex": True, "claude": True},
        "harness_accounts": {"codex": {"kind": "chatgpt"}},
    }
    conn = registry.get(_HOST_ID)
    assert conn is not None
    assert conn.hello.configured_harnesses == {"codex": True, "claude": True}
    assert conn.hello.harness_accounts == {"codex": {"kind": "chatgpt"}}


async def test_codex_login_completion_reports_expired_status(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, registry, comm, _replies, _drain = install_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
        assert started.status_code == 200
        login_id = started.json()["login_id"]
        await comm.send_input(
            {
                "type": "websocket.receive",
                "text": encode_host_frame(
                    HostHarnessLoginCompletedFrame(
                        login_id=login_id,
                        success=False,
                        expired=True,
                        error="login expired",
                    )
                ),
            }
        )
        conn = registry.get(_HOST_ID)
        assert conn is not None
        await poll_until_async(
            lambda: conn.harness_logins[login_id].status != "pending",
            timeout_s=2,
            interval_s=0.01,
        )

        status = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/{login_id}")

    assert status.json() == {
        "status": "expired",
        "error": "login expired",
        "expired": True,
    }


async def test_server_expiry_cancels_host_login(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, registry, _comm, replies, _drain = install_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
        login_id = started.json()["login_id"]
        conn = registry.get(_HOST_ID)
        assert conn is not None
        conn.harness_logins[login_id].expires_at = 0
        status = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/{login_id}")

    assert status.json()["status"] == "expired"
    assert replies["__cancelled__"] == {login_id: True}


async def test_terminal_login_is_pruned_after_grace_period(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, registry, _comm, _replies, _drain = install_setup
    conn = registry.get(_HOST_ID)
    assert conn is not None
    conn.harness_logins["old-login"] = HarnessLoginState(
        harness="codex",
        status="succeeded",
        expires_at=0,
        terminal_at=100,
    )
    monkeypatch.setattr(hosts_module, "now_epoch", lambda: 401)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login/old-login")

    assert response.status_code == 404
    assert "old-login" not in conn.harness_logins


async def test_codex_login_old_host_returns_login_unsupported(
    install_setup: tuple[
        FastAPI,
        HostRegistry,
        ApplicationCommunicator,
        dict[str, dict[str, Any]],
        asyncio.Task[None],
    ],
) -> None:
    app, registry, _comm, _replies, _drain = install_setup
    conn = registry.get(_HOST_ID)
    assert conn is not None
    conn.hello.capabilities = [CAP_REFRESH_HARNESSES]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "login_unsupported"


async def test_codex_login_route_hidden_when_flag_off(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    app, registry, host_store, conv_store = install_app
    comm = await _connect_mock_host(app, registry)
    off_app = FastAPI()
    off_app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            feature_flags=FeatureFlags(),
        ),
        prefix="/v1",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=off_app), base_url="http://test"
        ) as client:
            response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
            listed = (await client.get("/v1/hosts")).json()["hosts"][0]
            detail = (await client.get(f"/v1/hosts/{_HOST_ID}")).json()
        assert response.status_code == 404
        for host_payload in (listed, detail):
            assert "harness_accounts" not in host_payload
            assert "harness_accounts_supported" not in host_payload
            assert "harness_login_supported" not in host_payload
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})


async def test_codex_login_offline_host_returns_409(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    app, _registry, host_store, _conv_store = install_app
    host_store.upsert_on_connect(host_id=_HOST_ID, name=_HOST_NAME, user_id="local")
    host_store.set_offline(_HOST_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/codex/login")
    assert response.status_code == 409


async def test_install_harness_route_hidden_when_flag_off(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    With the flag off the route is 404 — the feature is invisible.

    Ships dark by default; only opt-in deployments expose it.
    """
    _app, registry, host_store, conv_store = install_app
    off_app = FastAPI()
    off_app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            feature_flags=FeatureFlags(),
        ),
        prefix="/v1",
    )
    async with AsyncClient(transport=ASGITransport(app=off_app), base_url="http://test") as client:
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
    Installing on an offline host returns 409.

    A host row can exist in the store while offline (no live tunnel connection
    present); the install needs a live connection to forward the frame.
    """
    app, _reg, host_store, _cs = install_app
    # Persist a host row as offline without a live registry connection.
    host_store.upsert_on_connect(
        host_id=_HOST_ID,
        name=_HOST_NAME,
        user_id="local",
    )
    host_store.set_offline(_HOST_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/v1/hosts/{_HOST_ID}/harnesses/claude/install")

    assert resp.status_code == 409


async def test_install_harness_non_owner_returns_403(
    install_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """A host owned by another user returns 403 — not installable by non-owners.

    Exercises the ownership branch with a real authenticated ``user_id`` (the
    default fixtures run unauthenticated, so ``user_id`` is ``None`` and the
    comparison is skipped): a host owned by alice, hit with bob's identity, must
    403. Guards the ``host.user_id`` owner check against a field rename.
    """
    from omnigent.server.auth import AuthProvider

    _app, _reg, host_store, conv_store = install_app

    class _Stub(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return request.headers.get("X-Test-User")

    auth = _Stub()
    auth_app = FastAPI()
    registry = HostRegistry()
    auth_app.include_router(
        create_host_tunnel_router(registry, host_store, auth_provider=auth), prefix="/v1"
    )
    auth_app.include_router(
        create_hosts_router(registry, host_store, conv_store, auth_provider=auth), prefix="/v1"
    )
    host_store.upsert_on_connect(host_id=_HOST_ID, name=_HOST_NAME, user_id="alice@example.com")

    async with AsyncClient(
        transport=ASGITransport(app=auth_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/harnesses/claude/install",
            headers={"X-Test-User": "bob@example.com"},
        )

    assert resp.status_code == 403
