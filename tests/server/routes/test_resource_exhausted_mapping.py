"""Targeted tests for the transient-exhaustion -> 503 error mapping.

The server's catch-all exception handler classifies a gRPC-shaped
``RESOURCE_EXHAUSTED`` escaping a store read (e.g. a per-read ACL gate's
quota under a burst) as a retry-able condition: HTTP 503 with a
``Retry-After`` hint and the ``resource_exhausted`` error code, instead of
an unhandled 500 ``internal_error``. These tests pin the response contract
and the classifier's shape-matching, including that other gRPC statuses do
NOT get the retry-able mapping.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.server.app import _is_resource_exhausted_rpc_error
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


class _GrpcShapedError(Exception):
    """Minimal gRPC-shaped error: a callable ``code()`` returning a status."""

    def __init__(self, status_name: str) -> None:
        super().__init__(f"rpc terminated with {status_name}")
        self._status_name = status_name

    def code(self) -> SimpleNamespace:
        return SimpleNamespace(name=self._status_name)


def _seed_session(db_uri: str) -> str:
    """Seed one real conversation bound to a real agent.

    :param db_uri: SQLite database URI shared with the test app.
    :returns: The seeded session/conversation id.
    """
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(
        agent_id,
        name=f"exhaustion-mapping-agent-{agent_id}",
        bundle_location="test:///bundle",
    )
    return conv_store.create_conversation(agent_id=agent_id).id


async def _fetch_with_store_error(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> httpx.Response:
    """GET a seeded session whose store fetch raises ``error``.

    :param app: The in-process FastAPI app (real stores, real routes).
    :param db_uri: SQLite database URI shared with the app.
    :param monkeypatch: pytest attribute patcher (auto-reverted per test).
    :param error: The exception the conversation fetch raises.
    :returns: The HTTP response the user's client receives.
    """
    session_id = _seed_session(db_uri)

    def _raise(self: SqlAlchemyConversationStore, conversation_id: str):
        raise error

    monkeypatch.setattr(SqlAlchemyConversationStore, "get_conversation", _raise)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/v1/sessions/{session_id}")


async def test_resource_exhausted_maps_to_503_with_retry_hint(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry-able mapping carries the full contract: 503 status, the
    ``resource_exhausted`` code, a human-readable message, and a
    ``Retry-After`` hint so clients know to back off and retry."""
    resp = await _fetch_with_store_error(
        app, db_uri, monkeypatch, _GrpcShapedError("RESOURCE_EXHAUSTED")
    )

    assert resp.status_code == 503, f"got HTTP {resp.status_code}: {resp.json()}"
    error = resp.json()["error"]
    assert error["code"] == "resource_exhausted"
    assert error["message"]
    assert resp.headers.get("Retry-After", "").isdigit(), (
        f"expected a numeric Retry-After hint, headers: {dict(resp.headers)}"
    )


async def test_other_grpc_statuses_stay_unhandled_500(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-exhaustion gRPC failure (e.g. UNAVAILABLE) is not retry-able
    by this contract and must keep surfacing as an internal 500, so the
    mapping never masks real faults as transient."""
    resp = await _fetch_with_store_error(app, db_uri, monkeypatch, _GrpcShapedError("UNAVAILABLE"))

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"


def test_classifier_matches_only_resource_exhausted_shape() -> None:
    """The duck-typed classifier accepts the gRPC RESOURCE_EXHAUSTED shape
    and rejects everything else, including hostile ``code`` attributes."""
    assert _is_resource_exhausted_rpc_error(_GrpcShapedError("RESOURCE_EXHAUSTED"))

    assert not _is_resource_exhausted_rpc_error(_GrpcShapedError("UNAVAILABLE"))
    assert not _is_resource_exhausted_rpc_error(Exception("plain"))

    non_callable_code = Exception("http-ish")
    non_callable_code.code = 8  # type: ignore[attr-defined]
    assert not _is_resource_exhausted_rpc_error(non_callable_code)

    class _RaisingCode(Exception):
        def code(self) -> SimpleNamespace:
            raise RuntimeError("status unavailable")

    assert not _is_resource_exhausted_rpc_error(_RaisingCode())
