"""Mid-flight request cancellation must not book as an unhandled 500.

**Scenario:** a request handler is cancelled mid-flight (the shape a client
disconnect / request teardown produces downstream) and lets a *bare*
``CancelledError`` escape before any response is sent. Starlette's
``BaseHTTPMiddleware`` runs the downstream app inside a child coroutine whose
``except Exception`` guard does not catch ``CancelledError`` (a
``BaseException``), so the child exits having sent nothing and captured no
exception; ``call_next`` then hits ``anyio.EndOfStream`` and raises
``RuntimeError("No response returned.")`` into the server's request-metrics
middleware::

    File ".../starlette/middleware/base.py", in call_next
        message = await recv_stream.receive()
    anyio.EndOfStream
    ...
    File ".../starlette/middleware/base.py", in call_next
        raise RuntimeError("No response returned.")

Unhandled, that surfaced through the catch-all exception handler as
``Unhandled exception: No response returned.`` — an UNKNOWN/BLOCKING 500 booked
against the server for what is a benign client-gone teardown.

**Expected behavior (asserted here):** the ``_record_server_metrics``
middleware absorbs that specific ``RuntimeError`` as a client-disconnect
outcome — a 499 ("client closed request") response with no unhandled-exception
error log — while a *genuine* handler exception still books as an unhandled
500 ``internal_error``.

The fault routes are mounted on the **real** ``create_app`` ASGI stack, so the
tests drive the genuine ``_record_server_metrics`` middleware and the genuine
catch-all ``Exception`` handler.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import pytest
from fastapi import APIRouter, Request

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as app_module
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

pytestmark = pytest.mark.asyncio

_CANCELLED_PATH = "/v1/__test_cancelled_midflight"
_BOOM_PATH = "/v1/__test_genuine_error"


def _build_fault_router() -> APIRouter:
    """Routes that model downstream faults inside the real ASGI stack.

    ``_CANCELLED_PATH`` is cancelled mid-request and lets a bare
    ``CancelledError`` escape before any response is produced — the
    downstream cancellation a client disconnect / request teardown injects
    into an in-flight handler. FastAPI never gets to serialize a response,
    so the inner ASGI app under ``_record_server_metrics`` completes with
    nothing sent and no ``Exception`` (``CancelledError`` is a
    ``BaseException``).

    ``_BOOM_PATH`` raises a genuine ``RuntimeError`` — the unhandled-server-
    error shape that must keep booking as a 500.
    """
    router = APIRouter()

    @router.get(_CANCELLED_PATH)
    async def _cancelled_midflight(request: Request) -> dict[str, str]:
        # An inner task the handler awaits is cancelled out from under it
        # (as the request's task scope is torn down on disconnect). Awaiting
        # the cancelled task re-raises a bare CancelledError into the handler.
        inner = asyncio.create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)  # let the task start
        inner.cancel()
        await inner  # bare CancelledError propagates out of the endpoint
        return {"unreachable": "true"}  # pragma: no cover

    @router.get(_BOOM_PATH)
    async def _genuine_error(request: Request) -> dict[str, str]:
        raise RuntimeError("boom")

    return router


def _make_app(db_uri: str, tmp_path: Path):
    """Build the real ``create_app`` ASGI stack plus the fault routes."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return app_module.create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        extra_routers=[(_build_fault_router(), "", ["fault-injection"])],
    )


def _unhandled_exception_records(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """Error records the catch-all ``Exception`` handler logged."""
    return [
        rec
        for rec in caplog.records
        if rec.name == "omnigent.server.app"
        and rec.funcName == "_handle_unhandled_exception"
        and rec.getMessage().startswith("Unhandled exception:")
    ]


async def test_midflight_cancellation_books_as_client_disconnect(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cancelled in-flight handler yields a benign 499, not an unhandled 500.

    The ``_record_server_metrics`` middleware must absorb the
    ``RuntimeError("No response returned.")`` Starlette raises for a
    cancelled downstream app instead of letting the catch-all handler log it
    as ``Unhandled exception: No response returned.`` and answer 500.
    """
    app = _make_app(db_uri, tmp_path)

    # raise_app_exceptions=False mirrors uvicorn: ServerErrorMiddleware sends
    # the response it built, then re-raises for the server's own logging; the
    # transport suppresses that re-raise the same way so we observe the
    # response the wire client would see.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with caplog.at_level(logging.INFO, logger="omnigent.server.app"):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(_CANCELLED_PATH)

    # Booked as "client closed request", not as an internal server error.
    assert resp.status_code == 499, (
        f"expected a benign 499 for the mid-flight cancellation, got "
        f"{resp.status_code}: {resp.text!r}"
    )
    assert resp.content == b""
    assert resp.headers.get("X-Request-Id"), "the 499 must keep its request id"

    # The catch-all handler must not book this as an unhandled UNKNOWN error.
    unhandled = _unhandled_exception_records(caplog)
    assert not unhandled, (
        "mid-flight cancellation must not reach _handle_unhandled_exception; "
        f"logged: {[r.getMessage() for r in unhandled]!r}"
    )

    # The teardown stays observable as a benign informational record.
    assert any(
        rec.name == "omnigent.server.app"
        and rec.levelno == logging.INFO
        and "Request cancelled before a response was sent" in rec.getMessage()
        for rec in caplog.records
    ), f"captured: {[(r.levelname, r.getMessage()) for r in caplog.records]!r}"


async def test_genuine_handler_error_still_books_as_unhandled_500(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real handler exception keeps the unhandled-500 booking.

    Guards the middleware's narrow catch: only the cancellation-shaped
    ``RuntimeError("No response returned.")`` is absorbed; any other error
    still flows to the catch-all handler and answers 500 ``internal_error``.
    """
    app = _make_app(db_uri, tmp_path)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="omnigent.server.app"):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(_BOOM_PATH)

    assert resp.status_code == 500, resp.text
    assert resp.json().get("error", {}).get("code") == "internal_error"

    matching = _unhandled_exception_records(caplog)
    assert matching, "a genuine error must still reach the catch-all handler"
    exc = matching[0].exc_info[1] if matching[0].exc_info else None
    assert isinstance(exc, RuntimeError) and str(exc) == "boom", exc
