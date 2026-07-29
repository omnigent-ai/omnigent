"""Regression tests: the event hot path loads the conversation once.

POST /v1/sessions/{id}/events fires per streamed chunk during active
turns (~98% of server query volume), so redundant conversation/ACL
reloads multiply hard. These pin two things at once, because a
query-count ceiling on its own would stay green if the work it is
counting disappeared entirely:

1. the per-table query counts for an event, and
2. that the event's real effect still happens — the status forward
   reaches the bound runner, the usage recompute publishes the right
   subtree total.

The routing read is deliberately NOT threaded from the handler's row:
runner resolution takes its own fresh single-column binding read so a
concurrent rebind is picked up. The rebind semantics themselves are
pinned in ``tests/runner/test_routing.py``, because the app builds its
own ``RunnerRouter`` over a real tunnel registry with no injection seam —
what IS testable here, and asserted below, is that every event issues its
own binding read rather than reusing a cached resolution.
"""

from __future__ import annotations

import re
from collections import Counter
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from sqlalchemy import event

from omnigent.db.utils import _engine_cache
from omnigent.runtime import session_stream, set_runner_client, set_runner_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# Attribute every statement to its table so the counter cannot miss the
# metadata / labels / UPDATE traffic a conversations-only filter ignored.
_TABLE_RE = re.compile(
    r"\bFROM\s+([a-z_][a-z0-9_]*)|\bINSERT\s+INTO\s+([a-z_][a-z0-9_]*)|\bUPDATE\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)


class _QueryCounts(Counter):
    """Per-table statement counts, retaining the statements themselves."""

    statements: list[str]


@contextmanager
def _count_queries_by_table(engine: Any):
    """Count every non-PRAGMA statement, keyed by the table it touches."""
    counts: _QueryCounts = _QueryCounts()
    counts.statements = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("PRAGMA"):
            return
        counts.statements.append(statement)
        match = _TABLE_RE.search(statement)
        table = next((g for g in match.groups() if g), "other") if match else "other"
        counts[table.lower()] += 1

    event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        yield counts
    finally:
        event.remove(engine, "before_cursor_execute", _on_exec)


@contextmanager
def _record_published(session_id: str):
    """Capture events published to *session_id*'s stream."""
    captured: list[dict[str, Any]] = []
    real_publish = session_stream.publish

    def _spy(conv_id: str, payload: dict[str, Any]) -> None:
        if conv_id == session_id:
            captured.append(payload)
        real_publish(conv_id, payload)

    session_stream.publish = _spy  # type: ignore[assignment]
    try:
        yield captured
    finally:
        session_stream.publish = real_publish  # type: ignore[assignment]


class _RecordingRunnerClient:
    """Minimal runner client that records the events posted to it."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        del timeout
        self.posts.append((url, json))
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))


class _ExplodingRunnerClient:
    """Process-wide fallback client that must never be used.

    Delivery falling back to this client is what let the routed-binding
    assertion pass for four review rounds.
    """

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        raise AssertionError(
            f"event was delivered through the fallback client, not the bound runner: POST {url}"
        )


@pytest.fixture()
def runner_router_reset():
    """Install/remove runner globals around a test."""
    yield
    set_runner_router(None)
    set_runner_client(None)


@pytest.mark.asyncio
async def test_status_event_forwards_and_reads_each_table_once(
    app: Any,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    runner_router_reset: None,
) -> None:
    """
    A status event reaches the runner it is BOUND to, and reads each
    conversation table once — not once per routing lookup on top of the
    handler's own read.

    The event is delivered through a real :class:`RunnerRouter` over a real
    registry, so the binding read under test actually selects the client
    that receives the POST. Earlier versions of this test bound
    ``runner_one`` without registering it: routing reported it offline,
    delivery silently fell back to the process-wide client, and the
    assertion passed while observing nothing about routing. The fallback
    client is installed here as a trap that fails if it is ever used.
    """
    from tests.server.helpers import register_test_runner

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="event-hot-path")
    store.set_runner_id(conv.id, "runner_one")

    # Bind AND register: the app's own router resolves the binding through
    # the read under test and then looks the runner up in the app's
    # registry. Binding without registering is what made routing report the
    # runner offline and delivery fall through to the global client.
    register_test_runner(app, "runner_one")
    runner = _RecordingRunnerClient()
    routed_ids: list[str] = []

    def _client_for_runner(runner_id: str) -> Any:
        routed_ids.append(runner_id)
        return runner

    # Real routing, stubbed transport: everything up to and including the
    # binding lookup is production code; only the tunnel socket is replaced.
    monkeypatch.setattr(app.state.runner_router, "_client_for_runner", _client_for_runner)
    set_runner_client(_ExplodingRunnerClient())  # type: ignore[arg-type]

    payload = {"type": "external_session_status", "data": {"status": "idle"}}
    await client.post(f"/v1/sessions/{conv.id}/events", json=payload)  # warm

    engine = _engine_cache[db_uri]
    baseline = len(runner.posts)
    with _count_queries_by_table(engine) as counts:
        resp = await client.post(f"/v1/sessions/{conv.id}/events", json=payload)

    assert resp.status_code == 202, resp.text
    # Delivered to the BOUND runner, not merely delivered: the routed client
    # is the one that received it, and the global fallback would have raised.
    assert routed_ids and set(routed_ids) == {"runner_one"}, routed_ids
    delivered = runner.posts[baseline:]
    assert [body["type"] for _url, body in delivered] == ["external_session_status"], delivered

    # The handler's single conversation load (conversations + metadata +
    # labels) plus routing's one narrow binding read — and nothing else.
    # Before this change routing re-loaded the whole row, so metadata,
    # conversations and labels were each read twice.
    assert counts["conversation_labels"] == 1, dict(counts)
    full_row_reads = [
        st
        for st in counts.statements
        if st.lstrip().upper().startswith("SELECT")
        and "FROM conversations" in st
        and "conversations.title" in st
    ]
    assert len(full_row_reads) == 1, full_row_reads
    # Routing's read is the NARROW binding lookup: it projects the id and the
    # runner binding only, never the row's other columns.
    binding_reads = [
        st for st in counts.statements if "OUTER JOIN" in st.upper() and "runner_id" in st
    ]
    assert len(binding_reads) == 1, binding_reads
    assert "conversations.title" not in binding_reads[0], binding_reads


@pytest.mark.asyncio
async def test_usage_event_publishes_subtree_total_with_exact_reads(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    The usage event recomputes the subtree total from the handler's row
    (no root re-resolution) and publishes the correct value — a count
    ceiling alone would pass if the recompute were deleted.
    """
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="usage-hot-path")
    child = store.create_conversation(
        kind="sub_agent", parent_conversation_id=conv.id, title="usage:child"
    )
    store.set_session_usage(child.id, {"total_cost_usd": 0.25})

    payload = {"type": "external_session_usage", "data": {"cumulative_cost_usd": 0.5}}
    await client.post(f"/v1/sessions/{conv.id}/events", json=payload)  # warm

    engine = _engine_cache[db_uri]
    with _record_published(conv.id) as published:
        with _count_queries_by_table(engine) as counts:
            resp = await client.post(f"/v1/sessions/{conv.id}/events", json=payload)

    assert resp.status_code == 202, resp.text
    usage_events = [e for e in published if e.get("type") == "session.usage"]
    assert usage_events, f"no session.usage published: {published}"
    # Parent's own 0.5 plus the child's 0.25 — proves the subtree recompute
    # ran and used the whole tree, not just this row.
    assert usage_events[-1]["total_cost_usd"] == pytest.approx(0.75)

    # Per-table totals, not a ceiling on one table: a ceiling hid extra
    # metadata and label statements entirely, and "<= 3" also passed with the
    # anti-replay read removed. conversations = handler row, the anti-replay
    # merge read (which must stay fresh) and the tree page scan; no root
    # re-resolution in any of them.
    assert dict(counts) == {
        "conversations": 4,
        "omnigent_conversation_metadata": 5,
        "conversation_labels": 4,
    }, dict(counts)
    # The fourth conversations read is the ancestor fan-out loading its own
    # tree at publication time. Reusing the tree summed above saved it and was
    # wrong: two siblings then published from their own request-start
    # snapshots, delivering a newer total followed by an older one.


@pytest.mark.asyncio
async def test_ancestor_publish_reads_the_tree_after_a_sibling_lands(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A parent's badge must not go stale because it was computed from a tree
    read at the START of the request that happened to publish it.

    Each usage event re-publishes every ancestor's subtree total. Taking that
    total from the tree loaded at the start of the current request made the
    figure a fact about request-arrival time, so a sibling's spend landing
    mid-request was published as if it had not happened — two siblings
    reporting around each other delivered a newer total and then an older one,
    and the parent was left showing the smaller number.

    This pins that specific regression, not a universal "never moves
    backwards" guarantee: a sibling's spend landing AFTER the ancestor
    fan-out's own tree read but before publish can still be missed, exactly
    as on unmodified `main` — that window is pre-existing and unrelated to
    the request-start snapshot this PR removed.

    The interleave is constructed rather than hoped for: a sibling's spend is
    committed immediately after this request's first tree load, which is
    exactly the window. The fan-out reads its own tree afterwards, so the
    published total includes it.
    """
    from omnigent.server.routes._sessions import orchestration as orch

    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(title="fanout-parent")
    reporter = store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title="usage:reporter"
    )
    sibling = store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, title="usage:sibling"
    )

    payload = {"type": "external_session_usage", "data": {"cumulative_cost_usd": 1.0}}
    await client.post(f"/v1/sessions/{reporter.id}/events", json=payload)  # warm

    real_load = orch.load_session_tree
    landed = {"done": False}

    def _load_then_land_sibling(*args: Any, **kwargs: Any):
        tree = real_load(*args, **kwargs)
        if not landed["done"]:
            landed["done"] = True
            # The sibling's spend commits here: after this request read a tree,
            # before anything is published from it.
            store.set_session_usage(sibling.id, {"total_cost_usd": 2.0})
        return tree

    monkeypatch.setattr(orch, "load_session_tree", _load_then_land_sibling)

    with _record_published(parent.id) as parent_events:
        resp = await client.post(f"/v1/sessions/{reporter.id}/events", json=payload)
    assert resp.status_code == 202, resp.text
    assert landed["done"], "the interleave never happened; the test proves nothing"

    totals = [e["total_cost_usd"] for e in parent_events if e.get("type") == "session.usage"]
    assert totals, f"the parent received no usage event: {parent_events}"
    # reporter 1.0 + sibling 2.0, read after the sibling landed.
    assert totals[-1] == pytest.approx(3.0), (
        f"the parent's published total {totals} was computed from a tree read "
        f"before the sibling's spend landed"
    )
