"""Regression: a WHS RESOURCE_EXHAUSTED on a conversation fetch must not
escape as an unhandled 500.

Every conversation fetch runs its own WHS ``GetTreeNode`` gRPC
call (the deliberate ACL + existence gate). Under a burst that gate can
saturate the workspace-wide 60-concurrent-request budget, and WHS answers
with ``StatusCode.RESOURCE_EXHAUSTED``
("REQUEST_LIMIT_EXCEEDED: Workspace ... exceeded the concurrent limit of 60
requests."). That transient error is neither retried nor mapped to a
retry-able status: it propagates out of ``get_conversation`` unhandled,
reaches ``server/app.py`` ``_handle_unhandled_exception``, and the user's
session load / turn fails with a bare 500 ``internal_error``.

Per the ticket's own correction, the fix is NOT to cache/batch the WHS call
(it is a permission gate and must run per read). The defect is the
*handling*: a transient ``RESOURCE_EXHAUSTED`` must be retried with backoff
and then surfaced as a retry-able **503** with a retry hint, never as an
unhandled 500.

This test drives the real ``GET /v1/sessions/{id}`` route -> the snapshot
builder -> ``ConversationStore.get_conversation`` chain and the real server
exception handlers, and asserts the post-fix contract (RED until the
handling lands, GREEN after).

Environment note / stand-in: the reported failing store is the
Databricks-internal ``databricks_mysql_conversation_store`` calling WHS over
Barnacle gRPC, which is not present in this OSS repo. Here the OSS
``SqlAlchemyConversationStore`` stands in for that store, and an injected
``grpc.RpcError`` carrying ``StatusCode.RESOURCE_EXHAUSTED`` (shaped like the
``_InactiveRpcError`` in the ticket) stands in for the real WHS
budget-exhaustion. The observable this guards -- a transient store/RPC error
mapped to a retry-able 503 instead of escaping as an unhandled 500 -- is
server-layer and reproduces faithfully on the stand-in.
"""

from __future__ import annotations

import asyncio
import threading

import grpc
import httpx
import pytest
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# Number of concurrent conversation fetches in the burst. Each is one
# ``GET /v1/sessions/{id}`` -> one ``get_conversation`` -> (in production)
# one WHS ``GetTreeNode``, which is what saturates the workspace budget.
_BURST = 6

# The verbatim WHS message from the bug report.
_WHS_LIMIT_MESSAGE = (
    "REQUEST_LIMIT_EXCEEDED: Workspace 1965859176160743 exceeded the "
    "concurrent limit of 60 requests."
)


class _ResourceExhaustedRpcError(grpc.RpcError):
    """Stand-in for the ``_InactiveRpcError`` the WHS gate raises when the
    workspace 60-concurrent-request budget is exhausted."""

    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.RESOURCE_EXHAUSTED

    def details(self) -> str:
        return _WHS_LIMIT_MESSAGE

    def __str__(self) -> str:
        return (
            "<_InactiveRpcError of RPC that terminated with:\n"
            "\tstatus = StatusCode.RESOURCE_EXHAUSTED\n"
            f'\tdetails = "{_WHS_LIMIT_MESSAGE}"\n>'
        )


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
        name=f"whs-repro-agent-{agent_id}",
        bundle_location="test:///bundle",
    )
    return conv_store.create_conversation(agent_id=agent_id).id


async def test_resource_exhausted_fetch_is_retryable_not_unhandled_500(
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst of conversation fetches that exhaust the workspace budget must
    surface as retry-able 503s, never as unhandled 500 ``internal_error``.

    Pre-fix: ``RESOURCE_EXHAUSTED`` escapes ``get_conversation`` unhandled,
    reaches ``_handle_unhandled_exception``, and every fetch returns a bare
    500 ``internal_error`` -- the user's session load / turn fails.

    Post-fix: the transient exhaustion is retried and then mapped to a
    retry-able 503, so it is handled (an ``OmnigentError``-shaped response),
    not an unhandled 500.

    :param app: The in-process FastAPI app (real stores, real routes).
    :param db_uri: SQLite database URI shared with the app.
    :param monkeypatch: pytest attribute patcher (auto-reverted per test).
    :returns: None.
    """
    session_id = _seed_session(db_uri)

    # Only after seeding (which never calls get_conversation): make the WHS
    # ACL gate report the workspace budget exhausted for every conversation
    # fetch. get_conversation runs under asyncio.to_thread, so count under a
    # lock to prove the burst actually reached the gate on every fetch.
    lock = threading.Lock()
    fetch_calls = {"n": 0}

    def _raise_exhausted(self: SqlAlchemyConversationStore, conversation_id: str):
        with lock:
            fetch_calls["n"] += 1
        raise _ResourceExhaustedRpcError()

    monkeypatch.setattr(SqlAlchemyConversationStore, "get_conversation", _raise_exhausted)

    # raise_app_exceptions=False so we observe the HTTP response the user's
    # client actually receives (an unhandled exception is otherwise re-raised
    # by Starlette's ServerErrorMiddleware into the in-process test).
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            *[client.get(f"/v1/sessions/{session_id}") for _ in range(_BURST)]
        )

    # The burst actually reached the exhausted gate on every fetch (guards
    # against the test passing because the route short-circuited earlier).
    assert fetch_calls["n"] == _BURST, (
        f"expected {_BURST} conversation fetches to hit the WHS gate, got {fetch_calls['n']}"
    )

    for resp in responses:
        # Hard invariant: the transient exhaustion must never escape as an
        # unhandled 500 internal_error (the pre-fix, ticket-reported failure).
        is_unhandled_500 = (
            resp.status_code == 500
            and resp.json().get("error", {}).get("code") == "internal_error"
        )
        assert not is_unhandled_500, (
            "WHS RESOURCE_EXHAUSTED escaped as an unhandled 500 "
            "(_handle_unhandled_exception) instead of being retried / mapped "
            f"to a retry-able 503. Response body: {resp.json()}"
        )
        # Fix target: a transient upstream-budget exhaustion is a retry-able
        # condition, so it surfaces as 503 (with a retry hint), not 5xx-internal.
        assert resp.status_code == 503, (
            "expected a retry-able 503 for a transient WHS RESOURCE_EXHAUSTED, "
            f"got HTTP {resp.status_code}: {resp.json()}"
        )
