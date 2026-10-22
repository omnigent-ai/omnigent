"""E2E regression: a peer-cancelled gRPC call must not book an unhandled session error.

Journey (Databricks agentbricks embedding): a session's client requests
workspace-tree data from a server route backed by a MAS gRPC call
(``ListTreeNodeChildren`` over a barnacle channel). The backing service
cancels the in-flight RPC (an upstream teardown/restart burst) and the raw
``grpc._channel._InactiveRpcError`` with ``StatusCode.CANCELLED`` escapes the
route into the server's generic catch-all, which books it as::

    Unhandled exception: <_InactiveRpcError of RPC that terminated with:
        status = StatusCode.CANCELLED ...

at ERROR level (category UNKNOWN, impact BLOCKING, HTTP 500).

The reproduction stands in for that deployment: a real in-process gRPC server
peer-cancels every call, and an ``extra_routers`` router (the embedding
extension point agentbricks uses to mount its MAS routers) performs the
blocking call inside a request handler, matching the deployed stack tail
(``with_call`` → ``_end_unary_response_blocking`` → ``_InactiveRpcError``).

The test drives the journey over real HTTP against a real uvicorn server and
asserts the guarded contract: the client still receives an error response, and
the cancelled RPC — an expected upstream condition — is not logged through
``_handle_unhandled_exception`` as an ERROR-level ``Unhandled exception``.

Run::

    .venv/bin/python -m pytest tests/e2e/test_grpc_peer_cancelled_not_unhandled.py -v
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Iterator
from concurrent import futures
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import APIRouter

# The reported failure is a grpcio client error escaping a route; without
# grpcio there is nothing to reproduce.
grpc = pytest.importorskip("grpc", reason="grpcio is required to raise the peer-cancelled RPC")

from omnigent.runtime import init as init_runtime  # noqa: E402
from omnigent.runtime.agent_cache import AgentCache  # noqa: E402
from omnigent.server.app import create_app  # noqa: E402
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore  # noqa: E402
from omnigent.stores.artifact_store.local import LocalArtifactStore  # noqa: E402
from omnigent.stores.conversation_store.sqlalchemy_store import (  # noqa: E402
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore  # noqa: E402
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S  # noqa: E402

# The gRPC method the ticket's stack tail names (MAS tree listing via barnacle).
_GRPC_SERVICE = "mas.TreeService"
_GRPC_METHOD = "ListTreeNodeChildren"


class _RecordingHandler(logging.Handler):
    """Capture log records emitted by the server's app logger."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        """
        :param records: Shared list the handler appends every record to.
        """
        super().__init__()
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        """
        Append the record for later assertions.

        :param record: The emitted log record.
        """
        self._records.append(record)


@pytest.fixture()
def cancelling_grpc_target() -> Iterator[str]:
    """
    Start a real gRPC server whose peer terminates every RPC with CANCELLED.

    Stands in for the MAS backend cancelling in-flight ``ListTreeNodeChildren``
    calls during an upstream teardown: the client observes a genuine
    ``_InactiveRpcError`` with ``StatusCode.CANCELLED``, the exception the
    ticket's log signature quotes.

    :returns: The ``host:port`` target of the cancelling gRPC server.
    """

    def _cancel(request: bytes, context: grpc.ServicerContext) -> bytes:
        """
        Terminate the RPC with ``StatusCode.CANCELLED`` and no details.

        :param request: Raw request payload (unused).
        :param context: Servicer context used to cancel the call.
        :returns: Never returns normally; ``abort`` raises.
        """
        del request
        context.abort(grpc.StatusCode.CANCELLED, "")
        return b""  # pragma: no cover - unreachable, abort() raises

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                _GRPC_SERVICE,
                {_GRPC_METHOD: grpc.unary_unary_rpc_method_handler(_cancel)},
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.stop(grace=None)


def _make_embedding_router(target: str) -> tuple[APIRouter, grpc.Channel]:
    """
    Build a deployment-style router backed by a blocking gRPC listing call.

    Mirrors how the agentbricks embedding mounts MAS routes through
    ``create_app(extra_routers=...)``: the handler performs a synchronous
    ``with_call`` (the exact frame in the ticket's stack tail) and lets any
    ``RpcError`` escape into the server's exception handling, as the deployed
    router does.

    :param target: ``host:port`` of the backing gRPC service.
    :returns: The router and the channel (for teardown).
    """
    channel = grpc.insecure_channel(target)
    list_children = channel.unary_unary(
        f"/{_GRPC_SERVICE}/{_GRPC_METHOD}",
        request_serializer=lambda payload: payload,
        response_deserializer=lambda payload: payload,
    )
    router = APIRouter()

    @router.get("/workspace-tree/children")
    def tree_children() -> dict[str, list[str]]:
        """
        List workspace-tree children via the backing gRPC service.

        :returns: The (empty) children listing when the backend answers.
        """
        response, _call = list_children.with_call(b"")
        del response
        return {"children": []}

    return router, channel


@pytest.fixture()
def embedded_server(
    db_uri: str,
    tmp_path: Path,
    cancelling_grpc_target: str,
) -> Iterator[tuple[str, list[logging.LogRecord]]]:
    """
    Run a real omnigent server with an embedding router over the gRPC backend.

    Builds the app exactly as a deployment does — real stores, plus an
    ``extra_routers`` entry whose handler calls the cancelling gRPC service —
    and serves it with uvicorn on a real socket, capturing everything the
    ``omnigent.server.app`` logger emits (the logger the ticket's KPI
    signatures attribute).

    :param db_uri: Per-test database URI from the root conftest.
    :param tmp_path: Pytest temp directory for artifacts and cache.
    :param cancelling_grpc_target: Target of the peer-cancelling gRPC server.
    :returns: ``(base_url, records)`` — the server URL and captured records.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_cache = AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache")
    init_runtime(
        conversation_store=conversation_store,
        agent_store=agent_store,
        agent_cache=agent_cache,
        file_store=file_store,
        artifact_store=artifact_store,
    )
    router, channel = _make_embedding_router(cancelling_grpc_target)
    app = create_app(
        agent_store=agent_store,
        file_store=file_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=agent_cache,
        extra_routers=[(router, "/v1/mas", ["mas"])],
    )

    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    app_logger = logging.getLogger("omnigent.server.app")
    app_logger.addHandler(handler)

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    _wait_until_serving(base_url)
    try:
        yield base_url, records
    finally:
        app_logger.removeHandler(handler)
        server.should_exit = True
        thread.join(timeout=15)
        channel.close()


def _wait_until_serving(base_url: str) -> None:
    """
    Block until the server answers HTTP (any status) or the boot budget lapses.

    :param base_url: Server base URL to probe.
    """
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=2.0)
        except httpx.HTTPError:
            time.sleep(POLL_INTERVAL_S)
            continue
        return
    raise RuntimeError(f"Server did not come up within {HEALTH_TIMEOUT_S}s")


def test_peer_cancelled_rpc_is_not_booked_as_unhandled_session_error(
    embedded_server: tuple[str, list[logging.LogRecord]],
) -> None:
    """
    A CANCELLED backing RPC must not surface as an unhandled session error.

    Drives the reconstructed journey: request the gRPC-backed listing route
    while the peer cancels the in-flight call. The request must still end in
    an error response the client can render, but the cancellation — an
    expected upstream condition — must not be booked through
    ``_handle_unhandled_exception`` as an ERROR-level ``Unhandled exception``.

    :param embedded_server: Base URL of the running server plus the records
        captured from the ``omnigent.server.app`` logger.
    """
    base_url, records = embedded_server

    response = httpx.get(f"{base_url}/v1/mas/workspace-tree/children", timeout=30.0)

    # The journey still ends in an observable error: the backing call failed,
    # so the route must not fabricate a success (and must answer at all).
    assert response.status_code >= 400

    unhandled = [
        record
        for record in records
        if record.levelno >= logging.ERROR
        and record.funcName == "_handle_unhandled_exception"
        and record.getMessage().startswith("Unhandled exception:")
        and "StatusCode.CANCELLED" in record.getMessage()
    ]
    assert not unhandled, (
        "peer-cancelled gRPC call was booked as an unhandled session error: "
        + unhandled[0].getMessage().splitlines()[0]
    )
