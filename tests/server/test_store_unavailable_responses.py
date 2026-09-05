"""Tests for the API surface of transient store-availability faults.

When the backing store is momentarily unavailable (rate-limited,
suspended/resuming, SQLite-locked, or out of pooled connections), the
server must answer a retryable ``503 store_unavailable`` — not the
``500 internal_error`` catch-all that signals a genuine defect — and
must log a concise availability WARNING instead of a full internal
stack trace. Genuine defects must keep the 500 + traceback treatment.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from omnigent.runtime.agent_cache import AgentCache
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


def _build_app(db_uri: str, tmp_path: Path) -> tuple[FastAPI, SqlAlchemyConversationStore]:
    """Build a minimal real app whose conversation store the test can break."""
    from omnigent.server.app import create_app

    conversation_store = SqlAlchemyConversationStore(db_uri)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )
    return app, conversation_store


async def _get_sessions(app: FastAPI) -> httpx.Response:
    """Drive ``GET /v1/sessions`` through the app's full middleware stack.

    ``raise_app_exceptions=False`` because Starlette's outermost error
    middleware re-raises after committing the catch-all handler's
    response; the committed response is what a real client sees.
    """
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/v1/sessions")


def _raise_from_store(exc: Exception) -> object:
    """Return a stand-in store method that raises ``exc``."""

    def _raiser(*args: object, **kwargs: object) -> object:
        raise exc

    return _raiser


@pytest.mark.asyncio
async def test_locked_store_returns_retryable_503_not_internal_500(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A busy/locked store answers 503 ``store_unavailable`` with Retry-After.

    "database is locked" is the exact retryable fault a rate-limited or
    write-contended store raises; surfacing it as the generic 500
    ``internal_error`` gives clients no retry signal and makes routine
    availability blips indistinguishable from server bugs.
    """
    app, store = _build_app(db_uri, tmp_path)
    fault = OperationalError(
        "INSERT INTO conversations ...",
        {},
        sqlite3.OperationalError("database is locked"),
    )
    monkeypatch.setattr(store, "list_conversations", _raise_from_store(fault))

    with caplog.at_level("DEBUG", logger="omnigent.server.app"):
        resp = await _get_sessions(app)

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "store_unavailable"
    assert resp.headers.get("retry-after") == "5"
    # The transient fault logs one concise WARNING — no ERROR, no traceback.
    records = [r for r in caplog.records if r.name == "omnigent.server.app"]
    assert records, "expected an availability log line"
    assert all(r.levelname == "WARNING" and r.exc_info is None for r in records), [
        (r.levelname, bool(r.exc_info)) for r in records
    ]


@pytest.mark.asyncio
async def test_defect_shaped_store_error_keeps_500_and_traceback(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuine defect (missing table) still gets 500 + ERROR with traceback.

    The transient classification must stay narrow: an OperationalError
    that signals a schema/logic bug is not retryable, and downgrading its
    logging would hide real defects from operators.
    """
    app, store = _build_app(db_uri, tmp_path)
    fault = OperationalError(
        "SELECT * FROM conversations",
        {},
        sqlite3.OperationalError("no such table: conversations"),
    )
    monkeypatch.setattr(store, "list_conversations", _raise_from_store(fault))

    with caplog.at_level("DEBUG", logger="omnigent.server.app"):
        resp = await _get_sessions(app)

    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "internal_error"
    error_records = [
        r for r in caplog.records if r.name == "omnigent.server.app" and r.levelname == "ERROR"
    ]
    assert error_records and all(r.exc_info for r in error_records)


@pytest.mark.asyncio
async def test_pool_exhaustion_returns_retryable_503(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool-checkout timeout maps to 503 through the catch-all handler.

    ``sqlalchemy.exc.TimeoutError`` is not a ``StatementError``, so it
    reaches the unhandled-exception handler — which must classify it as
    a drained-pool availability event, not an internal error.
    """
    app, store = _build_app(db_uri, tmp_path)
    fault = SqlAlchemyTimeoutError("QueuePool limit of size 5 overflow 10 reached")
    monkeypatch.setattr(store, "list_conversations", _raise_from_store(fault))

    resp = await _get_sessions(app)

    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["code"] == "store_unavailable"
