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

Two shapes a point-read count alone cannot see, covered below:

* A read issued by a **collaborator** the snapshot calls rather than by
  the snapshot itself. The runner router point-reads the conversation
  when the caller doesn't hand over the row it already holds; a test
  app with no router wired never reaches that read, so the count stays
  at 1 whether or not the row is passed. Asserted on the argument
  instead of the SQL.
* A duplicated **tree scan**. The usage report reads no extra rows per
  listed session, yet loaded each session's tree twice — once for the
  sums and once for the harness roll-up. Tree scans filter on
  ``root_conversation_id``, so they need their own counter.
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


def _conversation_tree_scans(statements: list[str]) -> list[str]:
    """
    Filter to tree scans — reads of every row under one tree root.

    Complement of :func:`_conversation_point_reads`: a tree page load
    filters on ``conversations.root_conversation_id = ?``. Counting these
    separately catches a caller that loads the same tree twice, which costs
    no extra point read and is therefore invisible to that counter.

    :param statements: Raw captured SQL statements.
    :returns: The matching statements (normalized whitespace).
    """
    matches: list[str] = []
    for raw in statements:
        normalized = " ".join(raw.split())
        if "FROM conversations" not in normalized or " WHERE " not in normalized:
            continue
        where = normalized.split(" WHERE ", 1)[1]
        if "root_conversation_id = " in where:
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


async def test_create_reuses_inserted_conversation(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A create without initial items must not point-read its new row."""
    agent = await create_test_agent(client)

    with _capture_sql(db_uri) as statements:
        resp = await client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "labels": {"source": "create"}},
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["labels"]["source"] == "create"
    point_reads = _conversation_point_reads(statements)
    assert point_reads == [], (
        "create point-read the row it had just inserted instead of reusing the "
        f"authoritative create result: statements={point_reads}"
    )


async def test_create_rereads_after_initial_items(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Initial-item persistence keeps one refresh for the current snapshot."""
    agent = await create_test_agent(client)
    payload = {
        "agent_id": agent["id"],
        "initial_items": [
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            }
        ],
    }

    with _capture_sql(db_uri) as statements:
        resp = await client.post("/v1/sessions", json=payload)

    assert resp.status_code == 201, resp.text
    point_reads = _conversation_point_reads(statements)
    assert len(point_reads) == 2, (
        "create with initial items should read once while appending and refresh "
        "exactly once after persistence; "
        f"statements={point_reads}"
    )


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


async def test_snapshot_hands_its_row_to_the_runner_router(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The snapshot must pass its authorized row to the runner router.

    ``client_for_session_resources`` point-reads the conversation when the
    caller doesn't supply one — a second read of the row the snapshot is
    already holding, and on a split-DB deployment one round trip per
    backend. The point-read counter above cannot see it: the router is a
    collaborator, and a test app with none wired never reaches its read.
    So assert on what the snapshot hands over.

    Every session open, every reconnect and every ``PATCH`` response builds
    a snapshot, so this is the most frequently paid duplicate of the set.
    """
    from omnigent import runtime as omnigent_runtime
    from omnigent.errors import ErrorCode, OmnigentError

    received: list[object] = []

    class _RecordingRouter:
        """Router stand-in recording the ``conversation`` it was handed."""

        def client_for_session_resources(
            self,
            conversation_id: str,
            *,
            conversation: object | None = None,
        ) -> object:
            """Record the row, then decline like an unbound session does."""
            del conversation_id
            received.append(conversation)
            # The snapshot treats "no runner bound" as a normal outcome, so
            # declining here exercises the same path a real unbound session
            # takes without needing a live runner.
            raise OmnigentError("no runner bound", code=ErrorCode.CONFLICT)

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # The snapshot resolves the router through ``omnigent.runtime`` at call
    # time, so patch it there rather than on the importing module.
    monkeypatch.setattr(omnigent_runtime, "get_runner_router", lambda: _RecordingRouter())
    resp = await client.get(f"/v1/sessions/{sid}?include_items=false&include_liveness=false")

    assert resp.status_code == 200, resp.text
    assert len(received) == 1, f"expected one router lookup, got {len(received)}"
    handed = received[0]
    assert handed is not None, (
        "the snapshot called the runner router without its conversation, so the "
        "router re-reads the row the snapshot already holds; pass conversation=conv"
    )
    assert getattr(handed, "id", None) == sid


async def test_usage_report_loads_each_session_tree_once(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``GET /v1/usage`` must load each listed session's tree once, not twice.

    With the usage page enabled the report needs both the subtree sums and
    the tree's rows (to roll up which harnesses ran in it). Asking for the
    sums and then re-loading the tree paged every listed session's tree
    twice — no extra point read, so the counter above stays satisfied while
    the scan count doubles with the page size.

    The page-details branch is gated on a release feature resolved when the
    router is built, so this mounts its own usage router with the feature on
    rather than trying to flip it after the shared app exists.
    """
    from fastapi import FastAPI

    from omnigent.server.feature_flags import Feature, FeatureFlags
    from omnigent.server.routes.usage import create_usage_router
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    agent = await create_test_agent(client)
    sessions = [await _create_session(client, agent["id"]) for _ in range(3)]
    assert len({s["id"] for s in sessions}) == 3

    detailed = FastAPI()
    detailed.include_router(
        create_usage_router(
            SqlAlchemyConversationStore(db_uri),
            feature_flags=FeatureFlags(frozenset({Feature.USAGE_PAGE})),
        ),
        prefix="/v1",
    )
    transport = httpx.ASGITransport(app=detailed)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as detailed_client:
        # Warm any caches so the counted request is steady-state.
        warm = await detailed_client.get("/v1/usage")
        assert warm.status_code == 200, warm.text

        with _capture_sql(db_uri) as statements:
            resp = await detailed_client.get("/v1/usage")

    assert resp.status_code == 200, resp.text
    # Count against what the report listed, not what this test created: the
    # report is user-scoped and the workspace may hold other rows.
    listed = len(resp.json()["sessions"])
    assert listed >= len(sessions)
    scans = _conversation_tree_scans(statements)
    assert len(scans) == listed, (
        f"usage report issued {len(scans)} tree scans for {listed} listed sessions "
        f"(expected one each); the harness roll-up should reuse the tree the sums "
        f"were computed from. statements={scans}"
    )
