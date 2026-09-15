"""
Regression tests for redundant conversation re-reads on the session
snapshot and usage paths.

``GET /v1/sessions/{id}`` authorizes the request and threads its own
conversation read into the snapshot builder — but the snapshot's
subtree-cost recompute calls ``load_session_usage`` WITHOUT the row's
``root_conversation_id``, so the tree root is re-derived by re-reading
the same conversation row the handler already holds. The same
"redundant read before a tree walk" shape recurs in the usage report
(per listed session) and in the turn-completion usage flush
(``external_session_usage`` → own-usage persist, subtree roll-up, and
ancestor publish each re-read the row). The flush keeps ONE extra read
by design: the own-usage persist's monotonic anti-forgery clamp must
baseline against a fresh row, not the request-start authorization row.

These tests pin the number of conversation POINT READS (statements
whose WHERE clause filters on ``conversations.id = ?``) each route
issues, counted at the SQL layer the way the policy-evaluate budget
test does — a store-call oracle cannot see a helper that issues three
statements per call. They FAIL while the redundancy exists and pass
once the already-fetched ``root_conversation_id`` is threaded through
to every tree-scan call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from omnigent.db.utils import _engine_cache
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


@contextmanager
def _capture_sql(db_uri: str) -> Iterator[list[str]]:
    """
    Capture every non-PRAGMA SQL statement executed on the test engine.

    :param db_uri: The test database URI (keys the shared engine cache).
    :returns: Context manager yielding the list statements append to.
    """
    from sqlalchemy import event as sa_event

    engine = _engine_cache[db_uri]
    statements: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        if not statement.lstrip().upper().startswith("PRAGMA"):
            statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        yield statements
    finally:
        sa_event.remove(engine, "before_cursor_execute", _on_exec)


def _conversation_point_reads(statements: list[str]) -> list[str]:
    """
    Filter to point reads of a single conversation row.

    A ``get_conversation`` call issues a row read of the shape
    ``SELECT ... FROM conversations WHERE ... conversations.id = ?``
    (plus separate metadata/labels reads, not matched here). Tree scans
    filter on ``root_conversation_id`` in their WHERE clause and do not
    match. Only the WHERE clause is inspected — the row SELECT's column
    list also names ``root_conversation_id``, which must not exclude it.

    :param statements: Raw captured SQL statements.
    :returns: The matching statements (normalized whitespace).
    """
    matches: list[str] = []
    for raw in statements:
        normalized = " ".join(raw.split())
        if "FROM conversations" not in normalized or " WHERE " not in normalized:
            continue
        where = normalized.split(" WHERE ", 1)[1]
        if "conversations.id = " in where and "root_conversation_id" not in where:
            matches.append(normalized)
    return matches


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> dict[str, Any]:
    """
    Create a session bound to *agent_id* via the public API.

    :param client: Test HTTP client.
    :param agent_id: Agent to bind, e.g. ``"ag_abc123"``.
    :returns: The ``POST /v1/sessions`` response body.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_snapshot_reads_conversation_once(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``GET /v1/sessions/{id}`` must point-read the conversation row exactly
    once (the authorization read the handler threads into the builder).

    The subtree-cost recompute currently calls ``load_session_usage``
    without the authorized row's ``root_conversation_id``, so the root is
    re-derived with a second point read of the same row. This is invisible
    to any behavioural assertion — both variants return the same snapshot —
    so the SQL count is the oracle.

    Uses the web chat's hot path (``include_items=false&include_liveness=false``)
    so the count covers exactly the snapshot build, not the transcript page
    or the liveness bulk lookup.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Warm the agent-cache / spec load so the counted request is steady-state.
    warm = await client.get(f"/v1/sessions/{sid}?include_items=false&include_liveness=false")
    assert warm.status_code == 200, warm.text

    with _capture_sql(db_uri) as statements:
        resp = await client.get(f"/v1/sessions/{sid}?include_items=false&include_liveness=false")

    assert resp.status_code == 200, resp.text
    point_reads = _conversation_point_reads(statements)
    assert len(point_reads) == 1, (
        f"snapshot issued {len(point_reads)} conversation point reads "
        f"(expected 1: the authorization read); the subtree-cost recompute "
        f"is re-deriving the tree root instead of reusing the authorized "
        f"row's root_conversation_id. statements={point_reads}"
    )


async def test_usage_report_does_not_reread_listed_conversations(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``GET /v1/usage`` must not point-read each listed session's row again.

    The report pages sessions with ``list_conversations`` (rows already in
    hand) and then calls ``load_session_usage(conv.id, store)`` per session
    WITHOUT ``conv.root_conversation_id`` — so every listed session costs an
    extra point read of a row the loop is already holding.
    """
    agent = await create_test_agent(client)
    first = await _create_session(client, agent["id"])
    second = await _create_session(client, agent["id"])
    assert first["id"] != second["id"]

    # Warm any caches so the counted request is steady-state.
    warm = await client.get("/v1/usage")
    assert warm.status_code == 200, warm.text

    with _capture_sql(db_uri) as statements:
        resp = await client.get("/v1/usage")

    assert resp.status_code == 200, resp.text
    point_reads = _conversation_point_reads(statements)
    assert len(point_reads) == 0, (
        f"usage report issued {len(point_reads)} conversation point reads "
        f"for rows its own list page already fetched; load_session_usage "
        f"should receive each row's root_conversation_id. "
        f"statements={point_reads}"
    )


async def test_usage_flush_reads_conversation_twice(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A turn-completion usage flush point-reads the conversation exactly twice.

    ``POST /v1/sessions/{id}/events`` with ``external_session_usage`` (the
    native harnesses' turn-completion usage report) previously read the
    same conversation row four times: the route's access check, the
    own-usage persist (``_persist_native_cumulative_usage``), the subtree
    roll-up (``load_session_usage`` without a root), and the ancestor
    publish (``_publish_subtree_cost_to_ancestors`` without the row).

    Two reads are each independently required and stay:

    - the route's access check (authorization);
    - the own-usage persist's read — its baseline for the monotonic
      forged-low-report clamp and the daily-rollup delta MUST be fresh,
      not the request-start row, or a replayed low report racing a real
      one gets a wider window to lower the persisted/enforced cost and
      double-count the daily rollup.

    The read-only tree walks (subtree roll-up, ancestor publish) reuse the
    access-check row's immutable ``root_conversation_id`` (verified against
    the tree it produces), so they cost no extra row read.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    payload = {
        "type": "external_session_usage",
        "data": {"context_tokens": 100, "cumulative_cost_usd": 0.10},
    }
    # Warm caches (e.g. model resolution via the agent cache).
    warm = await client.post(f"/v1/sessions/{sid}/events", json=payload)
    assert warm.status_code == 202, warm.text

    with _capture_sql(db_uri) as statements:
        resp = await client.post(f"/v1/sessions/{sid}/events", json=payload)

    assert resp.status_code == 202, resp.text
    point_reads = _conversation_point_reads(statements)
    assert len(point_reads) == 2, (
        f"usage flush issued {len(point_reads)} conversation point reads "
        f"(expected 2: the access check + the persist's fresh clamp "
        f"baseline); the subtree roll-up and the ancestor publish must "
        f"reuse the access-check row's root instead of re-reading the row. "
        f"statements={point_reads}"
    )
