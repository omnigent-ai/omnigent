"""E2E regression: a cancelled backing gRPC call must not book an unhandled session error.

Journey (Databricks agentbricks embedding): a session's client requests
workspace-tree data from a server route backed by a MAS gRPC call
(``ListTreeNodeChildren`` over a barnacle channel). Two upstream cancellation
shapes reach the route:

* the backing service peer-cancels the in-flight RPC (an upstream
  teardown/restart burst) — ``StatusCode.CANCELLED``;
* a client cancels its own request, the barnacle lease covering the channel is
  released and the endpoint tears down, sending GOAWAY ``Cancelling all calls``
  (``StatusCode.UNAVAILABLE``) to every other call still in flight.

Either way the raw ``grpc._channel._InactiveRpcError`` escapes the route into
the server's generic catch-all, which books it as::

    Unhandled exception: <_InactiveRpcError of RPC that terminated with:
        status = StatusCode.CANCELLED ...

at ERROR level (category UNKNOWN, impact BLOCKING, HTTP 500) — once per
affected request, so a lease release floods the log and the clients.

The reproduction stands in for that deployment: a real in-process gRPC server
plays the backend, and an ``extra_routers`` router (the embedding extension
point agentbricks uses to mount its MAS routers) performs the blocking call
inside a request handler, matching the deployed stack tail (``with_call`` →
``_end_unary_response_blocking`` → ``_InactiveRpcError``). The lease release
is modelled by stopping the backing server without grace while calls are held.

The tests drive the journey over real HTTP against a real uvicorn server and
assert the guarded contract: the client still receives an error response, and
the cancelled RPC — an expected upstream condition — is not logged through
``_handle_unhandled_exception`` as an ERROR-level ``Unhandled exception``.

Run::

    .venv/bin/python -m pytest tests/e2e/test_grpc_peer_cancelled_not_unhandled.py -v
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from concurrent import futures
from contextlib import contextmanager
from dataclasses import dataclass, field
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
_ROUTE = "/v1/mas/workspace-tree/children"
# How many clients hold calls on the leased channel when its lease is released.
_LEASED_CLIENTS = 3
_CLIENT_TIMEOUT_S = 30.0


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


def _serve_grpc(handler) -> tuple[grpc.Server, str]:
    """
    Start a real gRPC server exposing ``ListTreeNodeChildren`` via *handler*.

    :param handler: Unary-unary servicer callable ``(request, context)``.
    :returns: The started server and its ``host:port`` target.
    """
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=_LEASED_CLIENTS + 2))
    server.add_generic_rpc_handlers(
        (
            grpc.method_handlers_generic_handler(
                _GRPC_SERVICE,
                {_GRPC_METHOD: grpc.unary_unary_rpc_method_handler(handler)},
            ),
        )
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, f"127.0.0.1:{port}"


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

    server, target = _serve_grpc(_cancel)
    try:
        yield target
    finally:
        server.stop(grace=None)


@dataclass
class _LeasedBackend:
    """A backing gRPC endpoint that holds every call until its lease is released."""

    server: grpc.Server
    target: str
    arrived: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))

    def wait_for_inflight(self, count: int, timeout: float = 20.0) -> None:
        """
        Block until *count* calls are held in the backend.

        :param count: Number of in-flight calls to wait for.
        :param timeout: Per-call wait budget in seconds.
        """
        for _ in range(count):
            assert self.arrived.acquire(timeout=timeout), "backing call never arrived"

    def release_lease(self) -> None:
        """
        Tear the endpoint down under the held calls, as a released lease does.

        ``stop(grace=None)`` cancels every in-flight call from the server side;
        grpc delivers that to the clients as ``UNAVAILABLE`` / ``Cancelling all
        calls``.
        """
        stopped = self.server.stop(grace=None)
        assert stopped.wait(timeout=20.0), (
            "backing gRPC server did not stop after cancelling its calls"
        )


@pytest.fixture()
def leased_grpc_backend() -> Iterator[_LeasedBackend]:
    """
    Start a real gRPC server that holds ``ListTreeNodeChildren`` calls in flight.

    Stands in for the barnacle-leased MAS endpoint: calls stay open until the
    test releases the lease (tearing the endpoint down) or the call is
    cancelled from under the handler.

    :returns: The backend handle (target, in-flight tracking, lease release).
    """
    backend: _LeasedBackend | None = None

    def _hold(request: bytes, context: grpc.ServicerContext) -> bytes:
        """
        Hold the call until the endpoint teardown cancels it.

        :param request: Raw request payload (unused).
        :param context: Servicer context, polled so a cancelled call returns.
        :returns: An empty listing, discarded because the call was already cancelled.
        """
        del request
        assert backend is not None
        backend.arrived.release()
        # The deadline only guards a hung test; the teardown is what ends the call.
        deadline = time.monotonic() + 60.0
        while context.is_active() and time.monotonic() < deadline:
            time.sleep(0.05)
        if context.is_active():
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "teardown never cancelled the call")
        return b""

    server, target = _serve_grpc(_hold)
    backend = _LeasedBackend(server=server, target=target)
    try:
        yield backend
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


@contextmanager
def _serve_embedding(
    db_uri: str, tmp_path: Path, grpc_target: str
) -> Iterator[tuple[str, list[logging.LogRecord]]]:
    """
    Run a real omnigent server with an embedding router over a gRPC backend.

    Builds the app exactly as a deployment does — real stores, plus an
    ``extra_routers`` entry whose handler calls the backing gRPC service —
    and serves it with uvicorn on a real socket, capturing everything the
    ``omnigent.server.app`` logger emits (the logger the ticket's KPI
    signatures attribute).

    :param db_uri: Per-test database URI from the root conftest.
    :param tmp_path: Pytest temp directory for artifacts and cache.
    :param grpc_target: ``host:port`` of the backing gRPC service.
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
    router, channel = _make_embedding_router(grpc_target)
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


@pytest.fixture()
def embedded_server(
    db_uri: str,
    tmp_path: Path,
    cancelling_grpc_target: str,
) -> Iterator[tuple[str, list[logging.LogRecord]]]:
    """
    Real omnigent server whose embedding route is backed by the peer-cancelling service.

    :param db_uri: Per-test database URI from the root conftest.
    :param tmp_path: Pytest temp directory for artifacts and cache.
    :param cancelling_grpc_target: Target of the peer-cancelling gRPC server.
    :returns: ``(base_url, records)`` — the server URL and captured records.
    """
    with _serve_embedding(db_uri, tmp_path, cancelling_grpc_target) as served:
        yield served


@pytest.fixture()
def leased_embedded_server(
    db_uri: str,
    tmp_path: Path,
    leased_grpc_backend: _LeasedBackend,
) -> Iterator[tuple[str, list[logging.LogRecord]]]:
    """
    Real omnigent server whose embedding route is backed by the leased endpoint.

    :param db_uri: Per-test database URI from the root conftest.
    :param tmp_path: Pytest temp directory for artifacts and cache.
    :param leased_grpc_backend: The holding backend standing in for the leased channel.
    :returns: ``(base_url, records)`` — the server URL and captured records.
    """
    with _serve_embedding(db_uri, tmp_path, leased_grpc_backend.target) as served:
        yield served


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


def _unhandled_records(
    records: list[logging.LogRecord], status_name: str | None = None
) -> list[logging.LogRecord]:
    """
    ERROR-level catch-all bookings of a failed RPC.

    :param records: Records captured from the ``omnigent.server.app`` logger.
    :param status_name: gRPC status name quoted in the booked exception, or
        ``None`` to match a booking of any status.
    :returns: The matching ``Unhandled exception`` records.
    """
    return [
        record
        for record in records
        if record.levelno >= logging.ERROR
        and record.funcName == "_handle_unhandled_exception"
        and record.getMessage().startswith("Unhandled exception:")
        and f"StatusCode.{status_name or ''}" in record.getMessage()
    ]


def _upstream_cancelled_records(
    records: list[logging.LogRecord], status_name: str
) -> list[logging.LogRecord]:
    """
    WARNING-level upstream-cancellation bookings of an RPC that ended with *status_name*.

    :param records: Records captured from the ``omnigent.server.app`` logger.
    :param status_name: gRPC status name quoted in the booked exception.
    :returns: The matching ``Upstream call cancelled by its peer`` records.
    """
    return [
        record
        for record in records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("Upstream call cancelled by its peer:")
        and f"StatusCode.{status_name}" in record.getMessage()
    ]


def _booked_category(record: logging.LogRecord) -> str:
    """
    :param record: The captured record.
    :returns: The ``error_category`` attribute the booking carries, or ``?``.
    """
    attributes = getattr(record, "attributes", None) or {}
    return attributes.get("error_category", "?")


def _describe_booking(record: logging.LogRecord) -> str:
    """
    One-line summary of a booked exception record for assertion messages.

    :param record: The captured record.
    :returns: Level, category and the first message line.
    """
    first_line = record.getMessage().splitlines()[0]
    return f"{record.levelname} category={_booked_category(record)}: {first_line}"


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

    response = httpx.get(f"{base_url}{_ROUTE}", timeout=_CLIENT_TIMEOUT_S)

    # The journey still ends in an observable error: the backing call failed,
    # so the route must not fabricate a success (and must answer at all).
    assert response.status_code >= 400

    unhandled = _unhandled_records(records, "CANCELLED")
    assert not unhandled, (
        "peer-cancelled gRPC call was booked as an unhandled session error: "
        + unhandled[0].getMessage().splitlines()[0]
    )


@dataclass
class _ClientOutcome:
    """What one client received after the lease release."""

    name: str
    status_code: int | None = None
    body: str = ""
    error: str = ""

    def describe(self) -> str:
        """
        :returns: One-line summary for assertion messages.
        """
        if self.error:
            return f"{self.name}: transport error {self.error}"
        return f"{self.name}: HTTP {self.status_code} {self.body}"

    def error_code(self) -> str | None:
        """
        :returns: The ``error.code`` of a JSON error body, or ``None`` without one.
        """
        try:
            return json.loads(self.body)["error"]["code"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None


def _drive_lease_release(base_url: str, backend: _LeasedBackend) -> list[_ClientOutcome]:
    """
    Several clients hold calls on the leased channel when its lease is released.

    Each client sends the listing request; once every backing call is held,
    the lease release tears the endpoint down under all of them.

    :param base_url: Server base URL.
    :param backend: The leased backend standing in for the barnacle channel.
    :returns: What each client received.
    """
    outcomes = [_ClientOutcome(f"client-{index}") for index in range(_LEASED_CLIENTS)]

    def _request(outcome: _ClientOutcome) -> None:
        try:
            response = httpx.get(f"{base_url}{_ROUTE}", timeout=_CLIENT_TIMEOUT_S)
        except httpx.HTTPError as exc:
            outcome.error = repr(exc)
            return
        outcome.status_code = response.status_code
        outcome.body = response.text

    threads = [
        threading.Thread(target=_request, args=(outcome,), daemon=True) for outcome in outcomes
    ]
    for thread in threads:
        thread.start()
    try:
        backend.wait_for_inflight(_LEASED_CLIENTS)
    finally:
        # Tear the endpoint down even if a call never arrived, so every request
        # ends and the client threads can be joined.
        backend.release_lease()
        for thread in threads:
            thread.join(timeout=_CLIENT_TIMEOUT_S + 5)
    assert not any(thread.is_alive() for thread in threads), "a client never got a response"
    return outcomes


def test_lease_release_answers_499_and_books_no_unhandled_errors(
    leased_embedded_server: tuple[str, list[logging.LogRecord]],
    leased_grpc_backend: _LeasedBackend,
) -> None:
    """
    Endpoint teardown answers connected callers with the typed 499 and books no unhandled error.

    :param leased_embedded_server: Base URL of the running server plus the
        records captured from the ``omnigent.server.app`` logger.
    :param leased_grpc_backend: The leased backend whose teardown is driven.
    """
    base_url, records = leased_embedded_server

    outcomes = _drive_lease_release(base_url, leased_grpc_backend)

    # Responses: every client gets the coded, retryable 499.
    received = [(outcome.status_code, outcome.error_code()) for outcome in outcomes]
    assert received == [(499, "upstream_cancelled")] * len(outcomes), (
        "lease release did not answer the clients with upstream_cancelled:\n"
        + "\n".join(outcome.describe() for outcome in outcomes)
    )

    # Bookings: nothing reaches the catch-all as an unhandled error, whatever
    # status the teardown surfaced under.
    unhandled = _unhandled_records(records)
    assert not unhandled, (
        f"lease release booked {len(unhandled)} unhandled session error(s) "
        f"for {_LEASED_CLIENTS} in-flight calls:\n"
        + "\n".join(_describe_booking(record) for record in unhandled)
    )

    # Exactly one WARNING upstream-cancellation booking per cancelled call.
    cancelled = _upstream_cancelled_records(records, "UNAVAILABLE")
    assert len(cancelled) == len(outcomes), (
        "expected one WARNING upstream-cancellation booking per cancelled call, "
        f"got {len(cancelled)}:\n"
        + "\n".join(
            _describe_booking(record) for record in records if record.levelno >= logging.WARNING
        )
    )
    assert {_booked_category(record) for record in cancelled} == {"upstream"}, [
        _describe_booking(record) for record in cancelled
    ]
