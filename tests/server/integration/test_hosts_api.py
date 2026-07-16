"""Integration tests for the host REST API endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from omnigent.db.utils import now_epoch
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerResultFrame,
    encode_host_frame,
)
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_launch import HostLaunchTarget, resolve_host_launch
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_permission_store.sqlalchemy_store import (
    SqlAlchemyHostPermissionStore,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

pytestmark = pytest.mark.asyncio

_HOST_ID = "33296f9b15e02671c34e013dd711407e"


def _websocket_scope(path: str) -> dict[str, object]:
    """Build an ASGI WebSocket scope.

    :param path: WebSocket path.
    :returns: Minimal ASGI WebSocket scope.
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


def _make_hello(
    name: str = "test-laptop",
    configured_harnesses: dict[str, bool | str] | None = None,
) -> str:
    """Encode a HostHelloFrame for tests.

    :param name: Human-readable host name.
    :param configured_harnesses: Per-harness readiness map to report,
        e.g. ``{"claude-sdk": True}``; ``None`` mimics an older host
        that doesn't report it.
    :returns: JSON-encoded hello frame.
    """
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=name,
            configured_harnesses=configured_harnesses,
        )
    )


@pytest.fixture()
def host_api_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """FastAPI app with host tunnel + REST routes and stores.

    :param db_uri: SQLite URI from the shared fixture.
    :returns: Tuple of (app, registry, host_store, conv_store).
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    host_permission_store = SqlAlchemyHostPermissionStore(db_uri)
    app = FastAPI()
    app.state.host_permission_store = host_permission_store
    app.include_router(
        create_host_tunnel_router(registry, host_store),
        prefix="/v1",
    )
    app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            host_permission_store=host_permission_store,
        ),
        prefix="/v1",
    )
    return app, registry, host_store, conv_store


async def _connect_host(
    app: FastAPI,
    registry: HostRegistry,
    host_id: str = _HOST_ID,
    name: str = "test-laptop",
    configured_harnesses: dict[str, bool | str] | None = None,
) -> ApplicationCommunicator:
    """Connect a mock host via WebSocket tunnel.

    :param app: FastAPI app with host tunnel route.
    :param registry: Host registry to poll for registration.
    :param host_id: Host identifier.
    :param name: Host name for the hello frame.
    :param configured_harnesses: Readiness map for the hello frame,
        e.g. ``{"codex": False}``; ``None`` mimics an older host.
    :returns: Connected ASGI communicator.
    """
    path = f"/v1/hosts/{host_id}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"

    await comm.send_input(
        {"type": "websocket.receive", "text": _make_hello(name, configured_harnesses)},
    )
    while registry.get(host_id) is None:
        await asyncio.sleep(0.01)

    return comm


async def test_list_hosts_empty(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify list_hosts returns empty when no hosts are connected.

    If a non-empty list is returned, the owner filter is broken
    or stale data leaked.
    """
    app, _reg, _hs, _cs = host_api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts")
    assert resp.status_code == 200
    assert resp.json()["hosts"] == []


async def test_list_hosts_returns_connected_host(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify a connected host appears in the list with status 'online'.

    If status is 'offline' or the host is missing, the DB upsert
    or registry enrichment is broken.
    """
    app, registry, _hs, _cs = host_api_app
    _comm = await _connect_host(app, registry)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts")

    assert resp.status_code == 200
    hosts = resp.json()["hosts"]
    # Exactly one host should be listed.
    assert len(hosts) == 1, (
        f"Expected 1 host, got {len(hosts)}. "
        "Either the upsert didn't write or the owner filter excluded it."
    )
    assert hosts[0]["host_id"] == _HOST_ID
    assert hosts[0]["name"] == "test-laptop"
    assert hosts[0]["status"] == "online"
    # A tunnel-connected (user-owned) host is not sandbox-backed; the
    # field must be present and None so clients can tell it apart from
    # server-managed hosts without a schema sniff.
    assert hosts[0]["sandbox_provider"] is None


async def test_list_hosts_reports_sandbox_provider_for_managed_host(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify a server-managed sandbox host carries its provider in the list.

    Clients (the web UI host pickers) hide sandbox-backed hosts based
    on this field. If it comes back None or missing for a managed
    host, sandbox hosts reappear in the pickers as if they were
    user-connectable machines.
    """
    app, _reg, host_store, _cs = host_api_app
    host_store.register_managed_host(
        host_id="b8a8862c405a01143b4373e2b155b02a",
        name="sandbox-host",
        # Auth is disabled in this fixture, so list_hosts resolves the
        # caller to the reserved "local" owner — the managed host must
        # belong to it to be visible.
        owner="local",
        token="launch-token-secret",
        provider="modal",
        sandbox_id="sb-12345",
        token_expires_at=int(time.time()) + 3600,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts")

    assert resp.status_code == 200
    hosts = resp.json()["hosts"]
    assert len(hosts) == 1, (
        f"Expected exactly the managed host, got {len(hosts)} hosts. "
        "Either register_managed_host didn't persist or the owner "
        "filter excluded it."
    )
    assert hosts[0]["host_id"] == "b8a8862c405a01143b4373e2b155b02a"
    # Pre-registered managed hosts start offline until the in-sandbox
    # host process dials the tunnel.
    assert hosts[0]["status"] == "offline"
    assert hosts[0]["sandbox_provider"] == "modal"


async def test_get_host_returns_details(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify get_host returns the correct details for a connected host.
    """
    app, registry, _hs, _cs = host_api_app
    _comm = await _connect_host(app, registry)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/v1/hosts/{_HOST_ID}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["host_id"] == _HOST_ID
    assert data["name"] == "test-laptop"
    assert data["status"] == "online"
    # Detail endpoint mirrors list_hosts: a tunnel-connected host is
    # not sandbox-backed, so the field is present and None.
    assert data["sandbox_provider"] is None


async def test_hosts_api_surfaces_configured_harnesses(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify the readiness map a host reports in its hello is persisted
    and surfaced by both GET /v1/hosts and GET /v1/hosts/{id}.

    This is the data path the web agent picker's "not configured"
    warning reads. If the map is dropped anywhere along
    hello → upsert_on_connect → hosts route, the picker silently
    stops warning (None reads as unknown).
    """
    app, registry, _hs, _cs = host_api_app
    _comm = await _connect_host(
        app,
        registry,
        configured_harnesses={"claude-sdk": True, "codex": "needs-auth"},
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listing = await client.get("/v1/hosts")
        single = await client.get(f"/v1/hosts/{_HOST_ID}")

    assert listing.status_code == 200
    # Exact map equality end-to-end: the False bit is what drives the
    # picker warning; a lossy encode/persist would drop it.
    assert listing.json()["hosts"][0]["configured_harnesses"] == {
        "claude-sdk": True,
        "codex": "needs-auth",
    }
    assert single.status_code == 200
    assert single.json()["configured_harnesses"] == {"claude-sdk": True, "codex": "needs-auth"}


async def test_hosts_api_configured_harnesses_null_for_older_host(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify a host that doesn't report readiness (older build) lists
    with configured_harnesses null — unknown, not {}.

    An empty dict would read as "explicitly reported, nothing
    configured" only if a key were looked up — but null is the
    contract the web helper keys on to suppress warnings entirely.
    """
    app, registry, _hs, _cs = host_api_app
    _comm = await _connect_host(app, registry)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts")

    assert resp.status_code == 200
    assert resp.json()["hosts"][0]["configured_harnesses"] is None


async def test_get_host_404(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify get_host returns 404 for an unknown host_id.
    """
    app, _reg, _hs, _cs = host_api_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts/aababcc3941edb738172734a9ab7bb8c")
    assert resp.status_code == 404


async def test_list_and_get_host_report_online_from_other_replica(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    db_uri: str,
) -> None:
    """
    Verify a host connected to replica B is reported as ``online``
    when ``GET /v1/hosts`` and ``GET /v1/hosts/{host_id}`` are
    served by replica A.

    The hosts table is the cross-replica source of truth for
    ``status``; if either endpoint instead reads from its own
    in-memory :class:`HostRegistry`, the host will incorrectly
    appear as ``offline`` on every replica except the one that
    owns the WebSocket. This test simulates that topology with
    two routers backed by the same DB but distinct registries.
    """
    app, registry_b, _hs, _cs = host_api_app
    # Replica B owns the WebSocket connection.
    _comm = await _connect_host(app, registry_b)

    # Replica A: fresh app, fresh registry, same DB. It never sees
    # the host's WebSocket — only the persisted row.
    registry_a = HostRegistry()
    host_store_a = HostStore(db_uri)
    conv_store_a = SqlAlchemyConversationStore(db_uri)
    host_permission_store_a = SqlAlchemyHostPermissionStore(db_uri)
    app_a = FastAPI()
    app_a.include_router(
        create_hosts_router(
            registry_a,
            host_store_a,
            conv_store_a,
            host_permission_store=host_permission_store_a,
        ),
        prefix="/v1",
    )
    assert registry_a.get(_HOST_ID) is None, (
        "Test setup is broken: replica A's registry should be empty."
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_a),
        base_url="http://test",
    ) as client:
        list_resp = await client.get("/v1/hosts")
        get_resp = await client.get(f"/v1/hosts/{_HOST_ID}")

    assert list_resp.status_code == 200
    hosts = list_resp.json()["hosts"]
    assert len(hosts) == 1, f"Replica A should see 1 host via the shared DB, got {len(hosts)}."
    assert hosts[0]["status"] == "online", (
        "Replica A reported offline for a host connected to replica B — "
        "the read path is consulting the local registry instead of the DB."
    )

    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "online", (
        "GET /v1/hosts/{id} reported offline for a host connected to "
        "replica B — same bug as list_hosts."
    )


async def test_list_hosts_reports_offline_after_disconnect(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify a host that has disconnected is reported as ``offline``.

    After the tunnel close callback runs ``set_offline``, the DB
    row's ``status`` flips to ``"offline"`` and the API must
    reflect that.
    """
    app, registry, host_store, _cs = host_api_app
    comm = await _connect_host(app, registry)

    # Close the tunnel; the disconnect callback writes
    # status="offline" to the DB.
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
    for _ in range(200):
        row = host_store.get_host(_HOST_ID)
        if row is not None and row.status == "offline":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("host did not transition to offline within 2s")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        list_resp = await client.get("/v1/hosts")
        get_resp = await client.get(f"/v1/hosts/{_HOST_ID}")

    assert list_resp.status_code == 200
    assert list_resp.json()["hosts"][0]["status"] == "offline"
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "offline"


async def test_launch_runner_happy_path(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify the full launch flow: host receives launch frame, responds
    with 'launched', endpoint returns runner_id.

    If the host never receives the frame, the send_text path is broken.
    If the future doesn't resolve, the receive loop routing is broken.
    """
    app, registry, _hs, conv_store = host_api_app
    comm = await _connect_host(app, registry)

    conv = conv_store.create_conversation(agent_id=None)

    async def _respond_to_launch() -> None:
        """Read the launch frame from the host's outbound queue and respond."""
        conn = registry.get(_HOST_ID)
        assert conn is not None
        # Drain until we find the launch frame (skip pings).

        from omnigent.host.frames import HostLaunchRunnerFrame, decode_host_frame

        for _ in range(20):
            output = await comm.receive_output(timeout=2.0)
            if output["type"] != "websocket.send":
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostLaunchRunnerFrame):
                result = encode_host_frame(
                    HostLaunchRunnerResultFrame(
                        request_id=frame.request_id,
                        status="launched",
                        runner_id="runner_token_abc",
                    )
                )
                await comm.send_input(
                    {"type": "websocket.receive", "text": result},
                )
                return
        raise AssertionError("Host never received launch frame")

    responder = asyncio.create_task(_respond_to_launch())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={
                "session_id": conv.id,
                "workspace": "/tmp/test-workspace",
            },
        )

    await responder

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["status"] == "launching"
    # runner_id should be a deterministic hash of the binding token.
    assert data["runner_id"].startswith("runner_token_")

    # Session row should have runner_id and host_id set.
    updated_conv = conv_store.get_conversation(conv.id)
    assert updated_conv is not None
    assert updated_conv.runner_id == data["runner_id"], (
        "runner_id should be written to the session row before sending the launch frame"
    )
    assert updated_conv.host_id == _HOST_ID, "host_id should be written to the session row"


async def test_launch_runner_harness_not_configured_returns_412(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify the dedicated launch endpoint maps a host refusal carrying
    error_code='harness_not_configured' to a 412 with the specific
    error code (parity with POST /v1/sessions), after rolling back
    the runner bind.

    If this degrades to the generic 502, the client loses the
    machine-readable code (and the `omnigent setup` hint) on the
    fork-resume relaunch path.
    """
    from omnigent.errors import OmnigentError

    app, registry, _hs, conv_store = host_api_app

    # The bare test app has no exception handlers; register the same
    # OmnigentError → JSON handler create_app installs (app.py), so the
    # route's raise surfaces exactly as it would in production wiring.
    @app.exception_handler(OmnigentError)
    async def _handle(request: object, exc: OmnigentError) -> JSONResponse:
        """Convert OmnigentError to the production JSON error shape.

        :param request: The incoming request (unused).
        :param exc: The application error raised by the route.
        :returns: The same JSON body create_app's handler produces.
        """
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    comm = await _connect_host(app, registry)
    conv = conv_store.create_conversation(agent_id=None)

    async def _refuse_launch() -> None:
        """Reply 'failed' with the structured harness error code."""
        from omnigent.host.frames import HostLaunchRunnerFrame, decode_host_frame

        for _ in range(20):
            output = await comm.receive_output(timeout=2.0)
            if output["type"] != "websocket.send":
                continue
            frame = decode_host_frame(output["text"])
            if isinstance(frame, HostLaunchRunnerFrame):
                await comm.send_input(
                    {
                        "type": "websocket.receive",
                        "text": encode_host_frame(
                            HostLaunchRunnerResultFrame(
                                request_id=frame.request_id,
                                status="failed",
                                error=("harness 'codex' is not configured — run `omnigent setup`"),
                                error_code="harness_not_configured",
                            )
                        ),
                    },
                )
                return
        raise AssertionError("Host never received launch frame")

    responder = asyncio.create_task(_refuse_launch())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": "/tmp/test-workspace"},
        )
    await responder

    # 412 with the machine-readable code — not the generic 502.
    assert resp.status_code == 412, f"Expected 412, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["error"]["code"] == "harness_not_configured"
    assert "omnigent setup" in body["error"]["message"]

    # _rollback_failed_launch ran: the session is fully unbound so a
    # retry after `omnigent setup` starts clean.
    updated = conv_store.get_conversation(conv.id)
    assert updated is not None
    assert updated.runner_id is None, "failed launch must unbind runner_id"
    assert updated.host_id is None, "failed launch must unbind host_id"


async def test_launch_runner_409_host_offline(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify launch returns 409 when the host is in the DB but not
    connected.

    If it returns 200, the offline check is missing.
    """
    app, _reg, host_store, conv_store = host_api_app
    host_store.upsert_on_connect(_HOST_ID, "laptop", "local")
    host_store.set_offline(_HOST_ID)

    conv = conv_store.create_conversation(agent_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": "/tmp"},
        )
    assert resp.status_code == 409


async def test_launch_runner_400_already_bound(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify launch returns 400 when the session already has a runner.

    If it returns 200, a second runner would be spawned for the
    same session, causing routing confusion.
    """
    app, registry, _hs, conv_store = host_api_app
    _comm = await _connect_host(app, registry)

    conv = conv_store.create_conversation(
        agent_id=None,
        runner_id="runner_existing",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": "/tmp"},
        )
    assert resp.status_code == 400


async def test_launch_runner_404_unknown_host(
    host_api_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify launch returns 404 when the host doesn't exist.
    """
    app, _reg, _hs, conv_store = host_api_app
    conv = conv_store.create_conversation(agent_id=None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/aababcc3941edb738172734a9ab7bb8c/runners",
            json={"session_id": conv.id, "workspace": "/tmp"},
        )
    assert resp.status_code == 404


# ── Multi-user ownership tests ────────────────────────


class _StubAuthProvider:
    """Auth provider that returns a user ID from a request header.

    Lets tests simulate multiple users by setting ``X-Test-User``.

    :param header: Header name to read.
    """

    def __init__(self, header: str = "x-test-user") -> None:
        """Initialize with a header name.

        :param header: HTTP header carrying the user identity.
        """
        self._header = header

    def get_user_id(self, request: object) -> str | None:
        """Extract user ID from the request header.

        :param request: FastAPI Request or WebSocket.
        :returns: User ID string, or ``None`` if absent.
        """
        headers = getattr(request, "headers", {})
        return headers.get(self._header)


@pytest.fixture()
def multi_user_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """App with auth provider for multi-user ownership tests.

    :param db_uri: SQLite URI.
    :returns: Tuple of (app, registry, host_store, conv_store).
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    permission_store = SqlAlchemyPermissionStore(db_uri)
    host_permission_store = SqlAlchemyHostPermissionStore(db_uri)
    auth = _StubAuthProvider()
    app = FastAPI()
    # Stash the stores so tests can set up session and host grants.
    app.state.permission_store = permission_store
    app.state.host_permission_store = host_permission_store

    # Same OmnigentError → JSON handler create_app installs, so auth
    # failures (require_user raises OmnigentError, not HTTPException)
    # surface as 401 responses instead of raising through the client.
    from omnigent.errors import OmnigentError

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: object, exc: OmnigentError) -> JSONResponse:
        """Convert OmnigentError to the production JSON error shape.

        :param request: The incoming request (unused).
        :param exc: The application error raised by the route.
        :returns: The same JSON body create_app's handler produces.
        """
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        # local_single_user=False: this fixture models a deployed
        # multi-user server, so host_id re-own must be refused (the
        # behavior under test). Override the suite-wide single-user
        # default from tests/conftest.py (OMNIGENT_LOCAL_SINGLE_USER=1),
        # which create_host_tunnel_router would otherwise read from env.
        create_host_tunnel_router(
            registry, host_store, auth_provider=auth, local_single_user=False
        ),
        prefix="/v1",
    )
    app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            auth_provider=auth,
            permission_store=permission_store,
            host_permission_store=host_permission_store,
        ),
        prefix="/v1",
    )
    return app, registry, host_store, conv_store


async def test_list_hosts_filters_by_owner(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify that GET /v1/hosts only returns hosts owned by the
    requesting user.

    If alice sees bob's host, the owner filter is broken and
    host enumeration is possible across users.
    """
    _app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect(
        "f54bb9272002938a3a934bfcb6bb228a", "alice-laptop", "alice@test.com"
    )
    host_store.upsert_on_connect("774d8c150c0060ddb61a91b23b64a0d0", "bob-laptop", "bob@test.com")

    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
        # Alice sees only her host.
        resp = await client.get(
            "/v1/hosts",
            headers={"x-test-user": "alice@test.com"},
        )
        assert resp.status_code == 200
        host_ids = {h["host_id"] for h in resp.json()["hosts"]}
        assert host_ids == {"f54bb9272002938a3a934bfcb6bb228a"}, (
            f"Alice should only see f54bb9272002938a3a934bfcb6bb228a, got {host_ids}. "
            "Owner filtering on GET /v1/hosts is broken."
        )

        # Bob sees only his host.
        resp = await client.get(
            "/v1/hosts",
            headers={"x-test-user": "bob@test.com"},
        )
        host_ids = {h["host_id"] for h in resp.json()["hosts"]}
        assert host_ids == {"774d8c150c0060ddb61a91b23b64a0d0"}, (
            f"Bob should only see 774d8c150c0060ddb61a91b23b64a0d0, got {host_ids}."
        )


async def test_get_host_403_wrong_owner(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify that GET /v1/hosts/{id} returns 403 when the requesting
    user doesn't own the host.

    If it returns 200, a user can read another user's host details,
    which is an information leak.
    """
    _app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect(
        "294391bc835cde1130ef2a02dcd2b7b3", "alice-laptop", "alice@test.com"
    )

    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
        resp = await client.get(
            "/v1/hosts/294391bc835cde1130ef2a02dcd2b7b3",
            headers={"x-test-user": "bob@test.com"},
        )
    assert resp.status_code == 403, (
        f"Expected 403 for wrong owner, got {resp.status_code}. "
        "Owner check on GET /v1/hosts/{{id}} is missing."
    )


async def test_launch_runner_403_wrong_owner(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify that POST /v1/hosts/{id}/runners returns 403 when the
    requesting user doesn't own the host.

    If it returns 200, a user can launch runners on another user's
    machine — a critical security violation.
    """
    _app, registry, host_store, conv_store = multi_user_app
    host_store.upsert_on_connect(
        "a20f57f124c33161e2e17a8998af5b1f", "alice-laptop", "alice@test.com"
    )
    from omnigent.host.frames import HostHelloFrame

    registry.register(
        "a20f57f124c33161e2e17a8998af5b1f",
        type(
            "FakeWS",
            (),
            {
                "send_text": lambda self, d: None,
                "receive_text": lambda self: "",
            },
        )(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="alice-laptop"),
        owner="alice@test.com",
    )
    conv = conv_store.create_conversation(agent_id=None)

    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/a20f57f124c33161e2e17a8998af5b1f/runners",
            json={"session_id": conv.id, "workspace": "/tmp"},
            headers={"x-test-user": "bob@test.com"},
        )
    assert resp.status_code == 403, (
        f"Expected 403 for wrong owner on launch, got {resp.status_code}. "
        "Owner check on POST /v1/hosts/{{id}}/runners is missing."
    )


async def test_launch_runner_validates_workspace_boundary(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    POST /v1/hosts/{id}/runners validates the requested workspace against
    the agent's ``os_env.cwd`` sandbox boundary (W6) and rejects an
    out-of-boundary path with 400, leaving the session unbound.

    Without this, an owner could bind an arbitrary workspace via this
    bind-existing-session-to-host shortcut and escape the agent's declared
    sandbox — the boundary check ``POST /v1/sessions`` enforces would be
    skipped. ``validate_workspace`` itself (with its host.stat round-trip)
    is covered by the session-create + e2e suites; here we assert the
    endpoint wires it in and maps failures to 400 before binding.
    """
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.routes import _workspace_validation
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore

    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    host_permission_store = SqlAlchemyHostPermissionStore(db_uri)
    agent_store = SqlAlchemyAgentStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")

    # Single-user wiring (no auth_provider): resolve_host_launch authorizes
    # against the local owner so we reach the workspace validation directly.
    app = FastAPI()
    app.include_router(
        create_hosts_router(
            registry,
            host_store,
            conv_store,
            host_permission_store=host_permission_store,
            agent_store=agent_store,
            agent_cache=agent_cache,
        ),
        prefix="/v1",
    )

    host_store.upsert_on_connect(_HOST_ID, "laptop", "local")
    registry.register(
        _HOST_ID,
        type(
            "FakeWS",
            (),
            {"send_text": lambda self, d: None, "receive_text": lambda self: ""},
        )(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name="laptop"),
        owner="local",
    )
    conv = conv_store.create_conversation(agent_id=None)

    seen: dict[str, object] = {}

    async def _fake_validate_workspace(
        *,
        host_registry: object,
        host_id: str,
        workspace: str,
        spec_cwd: str | None,
        host_name_for_errors: str | None = None,
    ) -> str:
        """Stand in for validate_workspace; record input and reject."""
        seen["workspace"] = workspace
        raise _workspace_validation.WorkspaceValidationError(
            f"workspace '{workspace}' is outside the agent's required path"
        )

    monkeypatch.setattr(_workspace_validation, "validate_workspace", _fake_validate_workspace)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": "/etc"},
        )

    assert resp.status_code == 400, (
        f"out-of-boundary workspace must be rejected with 400, got {resp.status_code}: {resp.text}"
    )
    assert "outside the agent's required path" in resp.json()["detail"]
    # The endpoint validated exactly the caller-supplied workspace.
    assert seen.get("workspace") == "/etc"
    # Rejected BEFORE binding: a failed validation must not leave a runner.
    refetched = conv_store.get_conversation(conv.id)
    assert refetched is not None
    assert refetched.runner_id is None, "a rejected launch must not bind a runner"


async def test_tunnel_rejects_unauthenticated_when_auth_enabled(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    With an auth provider configured, a tunnel connection carrying no
    identity is closed (4004), not silently granted RESERVED_USER_LOCAL.

    Regression for the fail-closed fix: if the tunnel fell back to
    ``local`` on missing identity, an unauthenticated peer would be
    treated as the admin-equivalent local user and could
    hijack or enumerate other users' hosts.
    """
    app, registry, host_store, _cs = multi_user_app
    host_id = "3eeb18892635b9ed8ba2d060c8f58dd4"
    path = f"/v1/hosts/{host_id}/tunnel"
    # No x-test-user header -> stub auth returns None -> unauthenticated.
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})

    # Unauthenticated peers are refused BEFORE the WS upgrade, so the
    # first (and only) output is the close — no accept, no frame exchange.
    closed = await comm.receive_output(timeout=1.0)
    assert closed["type"] == "websocket.close", (
        f"expected the tunnel to close on missing identity before accept, got {closed!r}"
    )
    # 4004 = our "unauthenticated" close code.
    assert closed.get("code") == 4004, f"expected close code 4004, got {closed.get('code')!r}"
    # Fail-closed: nothing registered in-memory and nothing persisted, so
    # RESERVED_USER_LOCAL was never written as the host owner.
    assert registry.get(host_id) is None
    assert host_store.get_host(host_id) is None


async def test_tunnel_accepts_authenticated_owner(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    With auth configured, a tunnel carrying a valid identity registers
    the host owned by that user — the fail-closed check must not break
    the authenticated happy path.
    """
    app, registry, host_store, _cs = multi_user_app
    host_id = "d66146fe4cc6b0dc1d342f6a89475046"
    path = f"/v1/hosts/{host_id}/tunnel"
    scope = _websocket_scope(path)
    # Authenticated as alice via the stub's x-test-user header.
    scope["headers"] = [(b"x-test-user", b"alice@test.com")]
    comm = ApplicationCommunicator(app, scope)
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {"type": "websocket.receive", "text": _make_hello()},
    )
    while registry.get(host_id) is None:
        await asyncio.sleep(0.01)

    conn = registry.get(host_id)
    assert conn is not None
    # Owner comes from the authenticated identity, not the local fallback.
    assert conn.owner == "alice@test.com"
    stored = host_store.get_host(host_id)
    assert stored is not None
    assert stored.owner == "alice@test.com"


def _register_fake_host(registry: HostRegistry, host_id: str, owner: str) -> None:
    """Register an online host with a no-op WebSocket for ownership tests."""
    registry.register(
        host_id,
        type(
            "FakeWS",
            (),
            {
                "send_text": lambda self, d: None,
                "receive_text": lambda self: "",
            },
        )(),
        HostHelloFrame(version="0.1.0", frame_protocol_version=1, name=host_id),
        owner=owner,
    )


async def test_resolve_host_launch_enforces_host_and_session_ownership(
    db_uri: str,
) -> None:
    """
    The shared launch-authorization helper rejects every cross-user
    path and only succeeds when the caller owns BOTH the host and the
    session. This is the single chokepoint both launch routes use, so
    each branch here maps to a real exploit it blocks.
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm = SqlAlchemyPermissionStore(db_uri)
    host_perm = SqlAlchemyHostPermissionStore(db_uri)

    # Alice owns an online host and a session.
    host_store.upsert_on_connect(
        "f54bb9272002938a3a934bfcb6bb228a", "alice-laptop", "alice@test.com"
    )
    _register_fake_host(registry, "f54bb9272002938a3a934bfcb6bb228a", "alice@test.com")
    conv = conv_store.create_conversation(agent_id=None)
    perm.ensure_user("alice@test.com")
    perm.grant("alice@test.com", conv.id, LEVEL_OWNER)

    stores = {
        "host_store": host_store,
        "host_registry": registry,
        "conversation_store": conv_store,
        "permission_store": perm,
        "host_permission_store": host_perm,
    }

    # Happy path: Alice owns both → returns the resolved target.
    target = resolve_host_launch(
        user_id="alice@test.com",
        host_id="f54bb9272002938a3a934bfcb6bb228a",
        session_id=conv.id,
        **stores,
    )
    assert isinstance(target, HostLaunchTarget)
    assert target.host.owner == "alice@test.com"
    assert target.conv.id == conv.id

    # Bob targets Alice's HOST → 403. Blocks the inline-launch RCE
    # (running a runner on someone else's machine).
    with pytest.raises(HTTPException) as exc:
        resolve_host_launch(
            user_id="bob@test.com",
            host_id="f54bb9272002938a3a934bfcb6bb228a",
            session_id=conv.id,
            **stores,
        )
    assert exc.value.status_code == 403

    # Bob owns his own host but targets Alice's SESSION → 404. Blocks
    # the launch_runner session-hijack (binding her session to his runner).
    host_store.upsert_on_connect("774d8c150c0060ddb61a91b23b64a0d0", "bob-laptop", "bob@test.com")
    _register_fake_host(registry, "774d8c150c0060ddb61a91b23b64a0d0", "bob@test.com")
    with pytest.raises(HTTPException) as exc:
        resolve_host_launch(
            user_id="bob@test.com",
            host_id="774d8c150c0060ddb61a91b23b64a0d0",
            session_id=conv.id,
            **stores,
        )
    # 404 (not 403) so other users' sessions aren't enumerable.
    assert exc.value.status_code == 404

    # Unknown host → 404.
    with pytest.raises(HTTPException) as exc:
        resolve_host_launch(
            user_id="alice@test.com",
            host_id="b76b800b737d646b3ae8e06071d622c3",
            session_id=conv.id,
            **stores,
        )
    assert exc.value.status_code == 404

    # Host known but offline (in the store, no live connection) → 409.
    host_store.upsert_on_connect("3d9665477127e41f42de3f4109418173", "alice-old", "alice@test.com")
    with pytest.raises(HTTPException) as exc:
        resolve_host_launch(
            user_id="alice@test.com",
            host_id="3d9665477127e41f42de3f4109418173",
            session_id=conv.id,
            **stores,
        )
    assert exc.value.status_code == 409


async def test_launch_runner_rejects_other_users_session(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Bob owns the host (host-owner check passes) but targets Alice's
    session → 404. Without the session-ownership check Bob could bind
    Alice's session to his runner and read her prompts / forge replies.
    """
    app, registry, host_store, conv_store = multi_user_app
    perm = app.state.permission_store

    # Bob's own, online host.
    host_store.upsert_on_connect("774d8c150c0060ddb61a91b23b64a0d0", "bob-laptop", "bob@test.com")
    _register_fake_host(registry, "774d8c150c0060ddb61a91b23b64a0d0", "bob@test.com")

    # Alice's session (owned by Alice).
    conv = conv_store.create_conversation(agent_id=None)
    perm.ensure_user("alice@test.com")
    perm.grant("alice@test.com", conv.id, LEVEL_OWNER)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/774d8c150c0060ddb61a91b23b64a0d0/runners",
            json={"session_id": conv.id, "workspace": "/tmp"},
            headers={"x-test-user": "bob@test.com"},
        )
    assert resp.status_code == 404, (
        f"Expected 404 binding another user's session, got {resp.status_code}. "
        "Session-ownership check on POST /v1/hosts/{id}/runners is missing."
    )
    # And the session was not bound to Bob's runner.
    still = conv_store.get_conversation(conv.id)
    assert still is not None
    assert still.runner_id is None


async def test_failed_connect_does_not_offline_another_users_host(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    A peer connecting to another owner's host_id is refused, and that
    refusal must NOT flip the existing owner's host offline.

    The cross-owner conflict is now caught before accept() and answered
    with a 409 (close fallback when the denial-response extension is
    absent, as in this raw scope). The DoS guarantee is unchanged and
    arguably stronger: the peer's connection is never accepted, so the
    broad except that once called set_offline(host_id) on a never-
    registered connection cannot run.
    """
    app, _registry, host_store, _cs = multi_user_app

    # Alice's host is registered and online.
    host_store.upsert_on_connect(
        "be2a05c6f9530d33276f7c4b34bc39ad", "alice-laptop", "alice@test.com"
    )
    before = host_store.get_host("be2a05c6f9530d33276f7c4b34bc39ad")
    assert before is not None
    assert before.status == "online"

    # Bob (a different authenticated user) connects to Alice's host_id.
    scope = _websocket_scope("/v1/hosts/be2a05c6f9530d33276f7c4b34bc39ad/tunnel")
    scope["headers"] = [(b"x-test-user", b"bob@test.com")]
    comm = ApplicationCommunicator(app, scope)
    await comm.send_input({"type": "websocket.connect"})
    # Refused before accept(); no denial extension in this scope, so the
    # server falls back to a pre-accept close (code 4009).
    closed = await comm.receive_output(timeout=1.0)
    assert closed["type"] == "websocket.close"
    assert closed["code"] == 4009
    with contextlib.suppress(Exception):
        await comm.wait(timeout=2.0)

    after = host_store.get_host("be2a05c6f9530d33276f7c4b34bc39ad")
    assert after is not None
    # Bob never claimed the host_id...
    assert after.owner == "alice@test.com"
    # ...and crucially, Alice's host is still online: the pre-accept
    # refusal never runs set_offline on Bob's never-registered connection.
    assert after.status == "online", (
        "Bob's failed connect to Alice's host_id flipped her host offline "
        "(set_offline ran on a connection that never registered) - DoS."
    )


async def test_runner_exited_report_surfaces_in_runner_status(
    db_uri: str,
) -> None:
    """
    A ``host.runner_exited`` frame from the daemon reaches the runner
    status endpoint as an ``error`` field.

    This is the end-to-end wire path of the fail-fast diagnostic: the
    daemon watches a spawned runner, reports its death over the host
    tunnel, and the client polling ``GET /v1/runners/{id}/status``
    must see the cause instead of plain ``online: false``. A failure
    here means crashed runners regress to the blind 60s timeout with
    "check the logs directory".
    """
    from omnigent.host.frames import HostRunnerExitedFrame
    from omnigent.server.host_registry import RunnerExitReports
    from omnigent.server.routes.runner_tunnel import create_runner_tunnel_router

    registry = HostRegistry()
    host_store = HostStore(db_uri)
    reports = RunnerExitReports()
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(registry, host_store, runner_exit_reports=reports),
        prefix="/v1",
    )
    from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry

    app.include_router(
        create_runner_tunnel_router(TunnelRegistry(), runner_exit_reports=reports),
        prefix="/v1",
    )

    daemon_error = (
        "runner process exited with code 1 (log on host: ~/x.log)\n"
        "--- runner log tail ---\nModuleNotFoundError: No module named 'claude_agent_sdk'"
    )
    _comm = await _connect_host(app, registry)
    await _comm.send_input(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(
                HostRunnerExitedFrame(runner_id="runner_dead", error=daemon_error)
            ),
        }
    )
    # The receive loop processes the frame asynchronously — wait until
    # the report lands (bounded; a hang here means the frame was
    # dropped by the tunnel's receive loop).
    async with asyncio.timeout(2.0):
        while reports.get_visible("runner_dead", None) is None:
            await asyncio.sleep(0.01)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/runners/runner_dead/status")

    assert resp.status_code == 200
    body = resp.json()
    # The runner never connected a tunnel, so it reads offline...
    assert body["online"] is False
    # ...and the daemon's full cause — including the log tail naming
    # the actual failure — is surfaced verbatim for the waiting client.
    assert body["error"] == daemon_error


async def test_runner_exited_invokes_callback_with_runner_and_error(
    db_uri: str,
) -> None:
    """
    A ``host.runner_exited`` frame fires the ``on_runner_exited``
    callback with ``(runner_id, error)``.

    This callback is how the server marks the crashed runner's
    session(s) failed and pushes the cause to the open view (the
    runner never connected a tunnel, so the runner-tunnel disconnect
    path never fires). If the wiring breaks, a crashed runner's
    sessions stay stuck "starting" with no error — the exact desktop
    bug this fixes.
    """
    from omnigent.host.frames import HostRunnerExitedFrame

    registry = HostRegistry()
    host_store = HostStore(db_uri)
    received: list[tuple[str, str]] = []

    async def _record(runner_id: str, error: str) -> None:
        received.append((runner_id, error))

    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(registry, host_store, on_runner_exited=_record),
        prefix="/v1",
    )

    _comm = await _connect_host(app, registry)
    await _comm.send_input(
        {
            "type": "websocket.receive",
            "text": encode_host_frame(
                HostRunnerExitedFrame(runner_id="runner_x", error="exited with code 1")
            ),
        }
    )
    async with asyncio.timeout(2.0):
        while not received:
            await asyncio.sleep(0.01)

    # The callback got the exact runner id and error string off the frame.
    assert received == [("runner_x", "exited with code 1")]


# ── Admin fleet view (?all=true) ────────────────────────


async def test_list_all_hosts_admin_sees_every_owner(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify GET /v1/hosts?all=true returns every owner's hosts to an
    admin, with the fleet-only fields (created_at, last_seen,
    session_count) present.
    """
    app, _reg, host_store, conv_store = multi_user_app
    perm_store: SqlAlchemyPermissionStore = app.state.permission_store
    perm_store.ensure_user("admin@test.com", is_admin=True)
    host_store.upsert_on_connect("host_fleet_a", "alice-laptop", "alice@test.com")
    host_store.upsert_on_connect("host_fleet_b", "bob-laptop", "bob@test.com")
    # Two sessions bound to alice's host so session_count has signal.
    for _ in range(2):
        conv = conv_store.create_conversation(agent_id=None)
        conv_store.set_host_id(conv.id, "host_fleet_a", "/tmp/ws")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/v1/hosts",
            params={"all": "true"},
            headers={"x-test-user": "admin@test.com"},
        )
    assert resp.status_code == 200
    hosts = {h["host_id"]: h for h in resp.json()["hosts"]}
    assert {"host_fleet_a", "host_fleet_b"} <= set(hosts), (
        f"Admin should see every owner's hosts, got {set(hosts)}."
    )
    fleet_a = hosts["host_fleet_a"]
    assert fleet_a["owner"] == "alice@test.com"
    assert fleet_a["session_count"] == 2
    assert isinstance(fleet_a["created_at"], int)
    assert isinstance(fleet_a["last_seen"], int)


async def test_list_all_hosts_403_non_admin(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify GET /v1/hosts?all=true fails closed (403) for a non-admin —
    NOT a silently owner-filtered 200 they could mistake for the fleet.
    """
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_fleet_c", "alice-laptop", "alice@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/v1/hosts",
            params={"all": "true"},
            headers={"x-test-user": "bob@test.com"},
        )
    assert resp.status_code == 403, (
        f"Expected 403 for non-admin ?all=true, got {resp.status_code}. "
        "The admin fleet view must fail closed."
    )


async def test_list_all_hosts_plain_list_unchanged_for_admin(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify the default (no ?all) listing stays owner-scoped even for
    an admin, and carries none of the fleet-only fields — the picker
    payload is unchanged by the admin feature (additive/opt-in).
    """
    app, _reg, host_store, _cs = multi_user_app
    perm_store: SqlAlchemyPermissionStore = app.state.permission_store
    perm_store.ensure_user("admin@test.com", is_admin=True)
    host_store.upsert_on_connect("host_fleet_d", "alice-laptop", "alice@test.com")
    host_store.upsert_on_connect("host_fleet_e", "admin-laptop", "admin@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            "/v1/hosts",
            headers={"x-test-user": "admin@test.com"},
        )
    assert resp.status_code == 200
    hosts = resp.json()["hosts"]
    assert {h["host_id"] for h in hosts} == {"host_fleet_e"}
    assert "session_count" not in hosts[0]
    assert "last_seen" not in hosts[0]


# ── Host shutdown (POST /v1/hosts/{id}/shutdown) ────────────────────────


def _register_live_conn(registry: HostRegistry, host_id: str, owner: str) -> object:
    """Register a fake live connection so send_text enqueues frames.

    :param registry: The registry under test.
    :param host_id: Host identifier to register.
    :param owner: Owner identity for the connection.
    :returns: The registered HostConnection (queue readable by tests).
    """
    hello = HostHelloFrame(version="0", frame_protocol_version=1, name=host_id)
    return registry.register(host_id, ws=object(), hello=hello, owner=owner)


async def test_shutdown_host_owner_sends_frame(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify the owner can shut their host down: 200, and a
    host.shutdown frame is enqueued on the host's tunnel.
    """
    import json as _json

    app, registry, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_shut_a", "alice-laptop", "alice@test.com")
    conn = _register_live_conn(registry, "host_shut_a", "alice@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_shut_a/shutdown",
            headers={"x-test-user": "alice@test.com"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"status": "shutting_down"}
    frame = _json.loads(conn.outbound_queue.get_nowait())
    assert frame["kind"] == "host.shutdown"
    assert "alice@test.com" in frame["reason"]


async def test_shutdown_host_admin_non_owner_allowed(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify an admin who does NOT own the host can still shut it down
    (the operator reclaim path).
    """
    app, registry, host_store, _cs = multi_user_app
    perm_store: SqlAlchemyPermissionStore = app.state.permission_store
    perm_store.ensure_user("admin@test.com", is_admin=True)
    host_store.upsert_on_connect("host_shut_b", "alice-laptop", "alice@test.com")
    _register_live_conn(registry, "host_shut_b", "alice@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_shut_b/shutdown",
            headers={"x-test-user": "admin@test.com"},
        )
    assert resp.status_code == 200


async def test_shutdown_host_403_non_owner_non_admin(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify shutdown fails closed for a caller who is neither the owner
    nor an admin — and no frame reaches the host.
    """
    app, registry, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_shut_c", "alice-laptop", "alice@test.com")
    conn = _register_live_conn(registry, "host_shut_c", "alice@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_shut_c/shutdown",
            headers={"x-test-user": "bob@test.com"},
        )
    assert resp.status_code == 403
    assert conn.outbound_queue.empty(), "No shutdown frame may be sent on a refused request"


async def test_shutdown_host_409_offline(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify shutdown returns 409 when the host has no live tunnel —
    there is nothing to signal.
    """
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_shut_d", "alice-laptop", "alice@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_shut_d/shutdown",
            headers={"x-test-user": "alice@test.com"},
        )
    assert resp.status_code == 409


async def test_shutdown_host_404_unknown(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """Verify shutdown returns 404 for an unknown host id."""
    app, _reg, _hs, _cs = multi_user_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_nonexistent/shutdown",
            headers={"x-test-user": "alice@test.com"},
        )
    assert resp.status_code == 404


async def test_shutdown_host_400_managed_sandbox(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Verify shutdown refuses managed sandbox hosts (400) — they are
    torn down via the provider API, not a daemon-exit frame.
    """
    app, registry, host_store, _cs = multi_user_app
    host_store.register_managed_host(
        host_id="host_shut_e",
        name="sandbox-e",
        owner="alice@test.com",
        token="tok-shut-e",
        provider="modal",
        sandbox_id="sb-1",
        token_expires_at=now_epoch() + 3600,
    )
    _register_live_conn(registry, "host_shut_e", "alice@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/hosts/host_shut_e/shutdown",
            headers={"x-test-user": "alice@test.com"},
        )
    assert resp.status_code == 400


# ── Host sharing (shared visibility for SP-owned hosts) ───────────
# Covers the feature spec acceptance criteria AC-002..AC-007: shared
# hosts appear in /v1/hosts, a `view` grantee can read but not browse
# or launch, a `use` grantee can, non-granted users are blocked, and
# only owner/admin/manage can mutate grants.


def _grant_host(app: FastAPI, user_id: str, host_id: str, level: int) -> None:
    """Seed a host grant directly via the app's host permission store.

    :param app: The multi_user_app FastAPI instance.
    :param user_id: Grantee user id.
    :param host_id: Host to share.
    :param level: Host level (1=view, 2=use, 3=manage).
    """
    # The grantee must exist as a users row for the FK to hold.
    app.state.permission_store.ensure_user(user_id)
    app.state.host_permission_store.grant(user_id, host_id, level)


async def test_shared_host_appears_in_list_for_grantee(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """AC-002: /v1/hosts returns owned + shared, excludes unshared third-party.

    Alice owns one host and is granted `use` on the SP-owned host; she
    sees both. Bob's unrelated host stays hidden from her.
    """
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_alice", "alice-laptop", "alice@test.com")
    host_store.upsert_on_connect("host_sp", "coda-app", "app-sp@example")
    host_store.upsert_on_connect("host_bob", "bob-laptop", "bob@test.com")
    _grant_host(app, "alice@test.com", "host_sp", 2)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts", headers={"x-test-user": "alice@test.com"})
    assert resp.status_code == 200
    hosts = {h["host_id"]: h for h in resp.json()["hosts"]}
    assert set(hosts) == {"host_alice", "host_sp"}, (
        f"Alice should see her host plus the shared SP host, got {set(hosts)}."
    )
    # The owned host is flagged owned; the shared host is flagged shared
    # with a `use` level so the UI can label and gate it.
    assert hosts["host_alice"]["owned_by_current_user"] is True
    assert hosts["host_alice"]["permission_level"] == "owner"
    assert hosts["host_sp"]["owned_by_current_user"] is False
    assert hosts["host_sp"]["permission_level"] == "use"


async def test_view_grantee_can_read_host(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """AC-003: GET /v1/hosts/{id} allows a `view` grantee."""
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_sp", "coda-app", "app-sp@example")
    _grant_host(app, "alice@test.com", "host_sp", 1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts/host_sp", headers={"x-test-user": "alice@test.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["host_id"] == "host_sp"
    assert body["owned_by_current_user"] is False
    assert body["permission_level"] == "view"


async def test_filesystem_rejects_view_allows_use(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """AC-004: filesystem browsing rejects `view`, allows `use`.

    A `view` grantee is 403 on the filesystem endpoint. A `use`
    grantee gets past the access check (the host is offline here, so
    the call then 409s — proving auth passed, not the browse itself).
    """
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_sp", "coda-app", "app-sp@example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # view grantee → 403
        _grant_host(app, "viewer@test.com", "host_sp", 1)
        resp = await client.get(
            "/v1/hosts/host_sp/filesystem", headers={"x-test-user": "viewer@test.com"}
        )
        assert resp.status_code == 403, (
            "A view-only grantee must not browse the host filesystem (SR-002)."
        )

        # use grantee → passes the access check; host offline → 409.
        _grant_host(app, "user@test.com", "host_sp", 2)
        resp = await client.get(
            "/v1/hosts/host_sp/filesystem", headers={"x-test-user": "user@test.com"}
        )
        assert resp.status_code == 409, (
            f"A use grantee should pass auth and hit 'host offline' (409); got {resp.status_code}."
        )


async def test_nongranted_user_blocked_everywhere(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """AC-006: a non-granted user cannot list, read, or browse another's host."""
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_sp", "coda-app", "app-sp@example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Not in the list.
        resp = await client.get("/v1/hosts", headers={"x-test-user": "stranger@test.com"})
        assert resp.json()["hosts"] == []
        # Read → 403.
        resp = await client.get("/v1/hosts/host_sp", headers={"x-test-user": "stranger@test.com"})
        assert resp.status_code == 403
        # Browse → 403.
        resp = await client.get(
            "/v1/hosts/host_sp/filesystem", headers={"x-test-user": "stranger@test.com"}
        )
        assert resp.status_code == 403


async def test_only_owner_or_manage_can_grant(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """AC-007: only owner/admin/manage may grant or revoke host access."""
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_sp", "coda-app", "app-sp@example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # A `use` grantee cannot grant to others.
        _grant_host(app, "user@test.com", "host_sp", 2)
        resp = await client.put(
            "/v1/hosts/host_sp/permissions/victim@test.com",
            json={"level": "view"},
            headers={"x-test-user": "user@test.com"},
        )
        assert resp.status_code == 403, "A `use` grantee must not be able to grant."

        # The owner can grant.
        resp = await client.put(
            "/v1/hosts/host_sp/permissions/alice@test.com",
            json={"level": "manage"},
            headers={"x-test-user": "app-sp@example"},
        )
        assert resp.status_code == 200
        assert resp.json()["level"] == "manage"

        # The `manage` grantee can now grant further (FR-016a chain).
        resp = await client.put(
            "/v1/hosts/host_sp/permissions/bob@test.com",
            json={"level": "use"},
            headers={"x-test-user": "alice@test.com"},
        )
        assert resp.status_code == 200

        # The grant is visible in the permissions listing.
        resp = await client.get(
            "/v1/hosts/host_sp/permissions", headers={"x-test-user": "app-sp@example"}
        )
        assert resp.status_code == 200
        levels = {p["user_id"]: p["level"] for p in resp.json()["permissions"]}
        # user@test.com was seeded `use` at the top of the test; alice and
        # bob were granted above. All three legitimate grants are listed.
        assert levels == {
            "user@test.com": "use",
            "alice@test.com": "manage",
            "bob@test.com": "use",
        }

        # Revoke works and is reflected.
        resp = await client.delete(
            "/v1/hosts/host_sp/permissions/bob@test.com",
            headers={"x-test-user": "app-sp@example"},
        )
        assert resp.status_code == 200 and resp.json()["revoked"] is True


async def test_cannot_grant_to_owner_or_unknown_level(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """Granting to the owner, or an invalid level, is a 400."""
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_sp", "coda-app", "app-sp@example")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.put(
            "/v1/hosts/host_sp/permissions/app-sp@example",
            json={"level": "view"},
            headers={"x-test-user": "app-sp@example"},
        )
        assert resp.status_code == 400, "Granting to the host owner is meaningless."

        resp = await client.put(
            "/v1/hosts/host_sp/permissions/alice@test.com",
            json={"level": "owner"},
            headers={"x-test-user": "app-sp@example"},
        )
        assert resp.status_code == 400, "`owner` is not a grantable level."


async def test_private_hosts_stay_private_with_no_grants(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """AC-001 semantics: with no grants, each user sees only their own host."""
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_alice", "alice-laptop", "alice@test.com")
    host_store.upsert_on_connect("host_bob", "bob-laptop", "bob@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/hosts", headers={"x-test-user": "alice@test.com"})
        assert {h["host_id"] for h in resp.json()["hosts"]} == {"host_alice"}


# ── M6 hardening: unauthenticated fail-closed + audit trail ─────────


async def test_new_endpoints_401_unauthenticated(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    Every new endpoint fails closed with 401 when the caller presents
    no identity at all (auth provider configured, header absent) — an
    anonymous caller must never fall through to the single-user path.
    """
    app, registry, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_hard_a", "alice-laptop", "alice@test.com")
    _register_live_conn(registry, "host_hard_a", "alice@test.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, url, kwargs in [
            ("GET", "/v1/hosts?all=true", {}),
            ("POST", "/v1/hosts/host_hard_a/shutdown", {}),
            ("GET", "/v1/hosts/host_hard_a/permissions", {}),
            ("PUT", "/v1/hosts/host_hard_a/permissions/bob@test.com", {"json": {"level": "use"}}),
            ("DELETE", "/v1/hosts/host_hard_a/permissions/bob@test.com", {}),
        ]:
            resp = await client.request(method, url, **kwargs)
            assert resp.status_code == 401, (
                f"{method} {url} must 401 for an unauthenticated caller, got {resp.status_code}"
            )


async def test_stranger_cannot_read_grant_list(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """
    GET /v1/hosts/{id}/permissions fails closed (403) for a caller with
    no manage-level access — the grant list names principals and must
    not be enumerable by a mere `use` grantee or a stranger.
    """
    app, _reg, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_hard_b", "alice-laptop", "alice@test.com")
    _grant_host(app, "user@test.com", "host_hard_b", 2)  # use, not manage

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for caller in ("stranger@test.com", "user@test.com"):
            resp = await client.get(
                "/v1/hosts/host_hard_b/permissions",
                headers={"x-test-user": caller},
            )
            assert resp.status_code == 403, f"{caller} must not read the grant list"


async def test_audit_trail_for_shutdown_grant_revoke(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Shutdown, grant, and revoke each emit an omnigent.audit entry
    carrying actor + target (C-N3), so an operator can reconstruct who
    did what from the server log alone.
    """
    import json as _json

    app, registry, host_store, _cs = multi_user_app
    host_store.upsert_on_connect("host_hard_c", "alice-laptop", "alice@test.com")
    _register_live_conn(registry, "host_hard_c", "alice@test.com")

    with caplog.at_level("INFO", logger="omnigent.audit"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.put(
                "/v1/hosts/host_hard_c/permissions/bob@test.com",
                json={"level": "use"},
                headers={"x-test-user": "alice@test.com"},
            )
            await client.delete(
                "/v1/hosts/host_hard_c/permissions/bob@test.com",
                headers={"x-test-user": "alice@test.com"},
            )
            await client.post(
                "/v1/hosts/host_hard_c/shutdown",
                headers={"x-test-user": "alice@test.com"},
            )

    entries = [
        _json.loads(r.message.removeprefix("audit: "))
        for r in caplog.records
        if r.name == "omnigent.audit"
    ]
    by_action = {e["action"]: e for e in entries}
    assert set(by_action) == {
        "host.permission.grant",
        "host.permission.revoke",
        "host.shutdown",
    }
    for entry in entries:
        assert entry["actor"] == "alice@test.com"
        assert entry["target"] == "host_hard_c"
    assert by_action["host.permission.grant"]["principal"] == "bob@test.com"
    assert by_action["host.permission.grant"]["level"] == "use"
    assert by_action["host.permission.revoke"]["principal"] == "bob@test.com"


async def test_audit_trail_for_fleet_view(
    multi_user_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    The admin fleet enumeration (?all=true) is a security-relevant read
    across every owner, so it emits an omnigent.audit entry with the
    actor and a host count — an owner-scoped listing does NOT.
    """
    import json as _json

    app, _reg, host_store, _cs = multi_user_app
    perm_store: SqlAlchemyPermissionStore = app.state.permission_store
    perm_store.ensure_user("admin@test.com", is_admin=True)
    host_store.upsert_on_connect("host_fleet_audit_a", "alice-laptop", "alice@test.com")
    host_store.upsert_on_connect("host_fleet_audit_b", "bob-laptop", "bob@test.com")

    with caplog.at_level("INFO", logger="omnigent.audit"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Owner-scoped read: no audit.
            await client.get("/v1/hosts", headers={"x-test-user": "alice@test.com"})
            # Fleet read: audited.
            await client.get(
                "/v1/hosts",
                params={"all": "true"},
                headers={"x-test-user": "admin@test.com"},
            )

    entries = [
        _json.loads(r.message.removeprefix("audit: "))
        for r in caplog.records
        if r.name == "omnigent.audit"
    ]
    fleet_entries = [e for e in entries if e["action"] == "host.fleet.list"]
    assert len(fleet_entries) == 1, (
        f"expected exactly one fleet-list audit entry, got {[e['action'] for e in entries]}"
    )
    assert fleet_entries[0]["actor"] == "admin@test.com"
    assert fleet_entries[0]["host_count"] >= 2
