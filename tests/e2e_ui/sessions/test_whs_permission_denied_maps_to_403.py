"""E2E regression: a WHS 403 (gRPC ``PERMISSION_DENIED``) reaching the
session-listing funnel must surface as a *handled* 403, not an unhandled 500.

The bug
=======
The workspace hierarchy service, reached through Barnacle's gRPC channel, can
answer a listing request with a raw ``PERMISSION_DENIED`` whose details read
``"Received http2 header with status: 403"``. That ``grpc.RpcError`` is not
caught by any of the server's typed exception handlers
(``omnigent/server/app.py`` registers handlers for ``OmnigentError``,
``StatementError`` and a bare ``Exception`` catch-all only), so it lands in
``_handle_unhandled_exception`` and the user gets

    500 {"error": {"code": "internal_error", "message": "An internal error occurred."}}

with the traceback in the log and nothing naming the resource or a remedy. In
the web SPA the sidebar session list renders "Failed to load: 500 Internal
Server Error". The fix maps this class of gRPC ``PERMISSION_DENIED`` to a
handled 403 (``ErrorCode.FORBIDDEN``) instead.

The reproduction environment
============================
The real trigger lives in Databricks-internal code (``whs_client.list_children``
-> ``ListTreeNodeChildren`` via ``barnacle_grpc_channel``), which is not present
in this repo. This test stands in for it faithfully at the same boundary: a
**real** deny-all gRPC server that ``abort``\\s every call with
``StatusCode.PERMISSION_DENIED`` / ``"Received http2 header with status: 403"``,
wired into the conversation store's listing funnel so a genuine
``grpc._channel._InactiveRpcError`` (byte-identical to the ticket's log) is
raised from inside ``GET /v1/sessions`` -- the request the SPA sidebar issues on
load. The server, SPA and HTTP path are all real; only the workspace-hierarchy
backend is the stand-in.

What this asserts
=================
The tight fix target is the HTTP contract: ``GET /v1/sessions`` must return a
handled ``403 forbidden`` rather than the unhandled ``500 internal_error``. This
test fails today (it observes the 500) and passes once the gRPC
``PERMISSION_DENIED`` is mapped to a handled 403. The browser drive renders the
real user-visible failure (the sidebar's "Failed to load" state) so the journey
can be filmed.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from concurrent import futures
from pathlib import Path

import grpc
import httpx
import pytest
import uvicorn
from playwright.sync_api import Page, expect

from omnigent.runtime import init as init_runtime
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as app_module
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

# The exact gRPC status + details string from the ticket's log.
_WHS_403_DETAILS = "Received http2 header with status: 403"
# gRPC service/method matching the WHS listing RPC named in the ticket.
_WHS_METHOD = "/whs.WorkspaceHierarchyService/ListTreeNodeChildren"


class _DenyAllHandler(grpc.GenericRpcHandler):
    """A gRPC handler that denies every method with ``PERMISSION_DENIED``.

    Stands in for the workspace hierarchy service behind Barnacle answering a
    request with HTTP 403, which the gRPC layer surfaces as
    ``StatusCode.PERMISSION_DENIED`` with the ticket's details string.
    """

    def service(self, handler_call_details: object) -> object:
        def _deny(request: bytes, context: grpc.ServicerContext) -> bytes:
            context.abort(grpc.StatusCode.PERMISSION_DENIED, _WHS_403_DETAILS)
            raise AssertionError("unreachable")  # abort() never returns

        return grpc.unary_unary_rpc_method_handler(_deny)


def _start_deny_all_grpc() -> tuple[grpc.Server, grpc.Channel]:
    """Start the deny-all gRPC server and a channel to it.

    :returns: ``(server, channel)`` -- both owned by the caller, who tears
        them down when done.
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers((_DenyAllHandler(),))
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    return server, channel


def _wait_until_serving(base_url: str, timeout: float = 30.0) -> None:
    """Block until the app answers, so the SPA has something to load.

    ``/healthz`` (or any always-served route) proves uvicorn is up without
    tripping the faulted listing funnel.

    :param base_url: Server root, e.g. ``http://127.0.0.1:12345``.
    :param timeout: Seconds to wait before giving up.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/", timeout=2.0)
            return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise TimeoutError(f"server at {base_url} did not start within {timeout}s")


@pytest.fixture
def whs_403_server(built_spa: None, tmp_path: Path) -> Iterator[str]:
    """Run the real Omnigent app whose session listing hits a WHS 403.

    A deny-all gRPC backend stands in for the workspace hierarchy service
    behind Barnacle. The conversation store's ``list_conversations`` -- the
    funnel every ``GET /v1/sessions`` query goes through -- calls that backend,
    so a genuine ``grpc._channel._InactiveRpcError`` (``PERMISSION_DENIED`` /
    ``"Received http2 header with status: 403"``) is raised inside the request,
    exactly as ``whs_client.list_children`` -> ``ListTreeNodeChildren`` does in
    production. The SPA is served from the built bundle so the browser can drive
    the real sidebar.

    :param built_spa: Ensures the web SPA bundle is present on disk.
    :param tmp_path: Per-test scratch dir for the sqlite DB / artifacts.
    :yields: The base URL of the running server.
    """
    grpc_server, channel = _start_deny_all_grpc()
    list_children = channel.unary_unary(
        _WHS_METHOD,
        request_serializer=lambda payload: payload,
        response_deserializer=lambda payload: payload,
    )

    class _WhsBackedConversationStore(SqlAlchemyConversationStore):
        """Conversation store whose listing funnel goes through the WHS."""

        def list_conversations(self, *args: object, **kwargs: object) -> object:
            # Raises grpc._channel._InactiveRpcError(PERMISSION_DENIED, ...),
            # the same shape whs_client.list_children raises in production.
            list_children(b"")
            raise AssertionError("unreachable")  # the RPC always raises

    db_uri = f"sqlite:///{tmp_path / 'whs403.db'}"
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    conversation_store = _WhsBackedConversationStore(db_uri)
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
    )
    app = app_module.create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
    )

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30.0
        while not server.started:
            if time.time() > deadline:
                raise TimeoutError("uvicorn did not start")
            time.sleep(0.1)
        _wait_until_serving(base_url)
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        channel.close()
        grpc_server.stop(grace=None)


def test_whs_permission_denied_is_handled_not_500(
    page: Page,
    whs_403_server: str,
) -> None:
    """A WHS gRPC ``PERMISSION_DENIED`` must surface as a handled 403, not a 500.

    Drives the real SPA to render the user-visible failure (the sidebar's
    "Failed to load" state), then pins the fix target on the HTTP contract:
    ``GET /v1/sessions`` must answer ``403 forbidden`` instead of the unhandled
    ``500 internal_error`` the bug produces.

    :param page: Playwright page fixture (fresh context per test; filmed via
        ``--video`` for the reproduction clip).
    :param whs_403_server: Base URL of the app whose session listing hits the
        WHS 403 stand-in.
    """
    # 1. Drive the real user journey: open the app; the sidebar issues its
    #    session-list query on load, which hits the WHS 403.
    page.goto(f"{whs_403_server}/", wait_until="domcontentloaded")

    # The sidebar surfaces a load failure once the query settles. This renders
    # for the reproduction footage regardless of the eventual status code.
    expect(page.get_by_text("Failed to load", exact=False).first).to_be_visible(timeout=30_000)

    # 2. Pin the fix target on the server contract. Today the gRPC
    #    PERMISSION_DENIED escapes to _handle_unhandled_exception and the
    #    endpoint answers 500 internal_error; once mapped to a handled error it
    #    must answer 403 forbidden (naming the resource), never a raw 500.
    resp = httpx.get(f"{whs_403_server}/v1/sessions", params={"limit": 30}, timeout=15.0)
    body = resp.json()
    error_code = body.get("error", {}).get("code")

    assert resp.status_code != 500, (
        "WHS gRPC PERMISSION_DENIED escaped as an unhandled 500 instead of "
        f"a handled 403. Body: {body!r}"
    )
    assert resp.status_code == 403, (
        f"expected a handled 403 for a workspace-hierarchy permission denial, "
        f"got {resp.status_code}. Body: {body!r}"
    )
    assert error_code == "forbidden", (
        f"expected error code 'forbidden', got {error_code!r}. Body: {body!r}"
    )
