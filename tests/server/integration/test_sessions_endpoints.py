"""Integration tests for /v1/sessions endpoints.

Exercises every sessions-API surface added in the migration:
``GET /v1/sessions``, ``PATCH /v1/sessions/{id}``,
``GET /v1/sessions/{id}/items``, title/labels on create, and
function_call_output routing.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests hit the real route → store
pipeline without subprocesses.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from omnigent.llms.context_window import ModelPricing
from omnigent.runtime.tool_output import MAX_TOOL_OUTPUT_BYTES
from omnigent.spec.types import SkillSpec
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore
from omnigent.tools.builtins.load_skill import format_skill_meta_text
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    initial_message: str | None = None,
    title: str | None = None,
    labels: dict[str, str] | None = None,
    terminal_launch_args: list[str] | None = None,
) -> dict[str, Any]:
    """
    Create a session and return the response JSON.

    :param client: The test HTTP client.
    :param agent_id: Agent to bind, e.g. ``"ag_abc123"``.
    :param initial_message: When set, seed the session with a
        user message.
    :param title: Optional session title.
    :param labels: Optional initial labels.
    :param terminal_launch_args: Optional native-terminal
        pass-through CLI args, e.g.
        ``["--dangerously-skip-permissions"]``.
    :returns: The ``POST /v1/sessions`` response body.
    """
    payload: dict[str, Any] = {"agent_id": agent_id}
    if initial_message is not None:
        payload["initial_items"] = [
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": initial_message}],
                },
            },
        ]
    if title is not None:
        payload["title"] = title
    if labels is not None:
        payload["labels"] = labels
    if terminal_launch_args is not None:
        payload["terminal_launch_args"] = terminal_launch_args
    resp = await client.post("/v1/sessions", json=payload)
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    return resp.json()


async def _wait_for_idle(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """
    Poll ``GET /v1/sessions/{id}`` until the session reaches
    ``idle`` or ``failed``.

    :param client: The test HTTP client.
    :param session_id: Session to poll.
    :param timeout_s: Maximum seconds to wait.
    :returns: The terminal session snapshot.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    snap: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/v1/sessions/{session_id}")
        snap = resp.json()
        if snap["status"] in ("idle", "failed"):
            return snap
        await asyncio.sleep(0.1)
    raise AssertionError(
        f"session {session_id} did not reach idle/failed within {timeout_s}s; "
        f"snapshot={json.dumps(snap, indent=2)}"
    )


# ── POST /v1/sessions with title and labels ─────────────


async def test_create_session_with_title_and_labels(
    client: httpx.AsyncClient,
) -> None:
    """Title and labels flow through to the created session snapshot."""
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        title="my test session",
        labels={"env": "test", "priority": "high"},
    )
    assert session["title"] == "my test session"
    assert session["labels"]["env"] == "test"
    assert session["labels"]["priority"] == "high"


async def test_create_session_without_title_returns_none(
    client: httpx.AsyncClient,
) -> None:
    """Omitting title returns null in the snapshot."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    assert session["title"] is None


# ── GET /v1/sessions (list) ──────────────────────────────


async def test_list_sessions_returns_only_sessions(
    client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/sessions`` returns sessions (conversations with
    agent_id), not legacy conversations created via /v1/responses.
    """
    agent = await create_test_agent(client)
    s1 = await _create_session(client, agent["id"], title="session-1")
    s2 = await _create_session(client, agent["id"], title="session-2")

    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    data = resp.json()["data"]
    session_ids = {s["id"] for s in data}
    assert s1["id"] in session_ids
    assert s2["id"] in session_ids
    for s in data:
        assert s["agent_id"] is not None


async def test_list_sessions_filters_by_agent_id(
    client: httpx.AsyncClient,
) -> None:
    """``agent_id`` query param scopes to sessions bound to that agent.

    The filter uses the ``conversations.agent_id`` column directly
    (the tasks table has been removed). Sessions carry their agent
    binding in the conversation row, so no seeding is required.
    """
    a1 = await create_test_agent(client, name="agent-a")
    a2 = await create_test_agent(client, name="agent-b")
    s1 = await _create_session(client, a1["id"], title="a1-session")
    await _create_session(client, a2["id"], title="a2-session")

    resp = await client.get("/v1/sessions", params={"agent_id": a1["id"]})
    assert resp.status_code == 200
    data = resp.json()["data"]
    ids = {s["id"] for s in data}
    assert s1["id"] in ids
    assert all(s["agent_id"] == a1["id"] for s in data)


async def test_list_sessions_pagination(
    client: httpx.AsyncClient,
) -> None:
    """Cursor pagination works with limit and after."""
    agent = await create_test_agent(client)
    sessions = []
    for i in range(3):
        s = await _create_session(client, agent["id"], title=f"s-{i}")
        sessions.append(s)

    resp = await client.get("/v1/sessions", params={"limit": 1, "order": "asc"})
    assert resp.status_code == 200
    page1 = resp.json()
    assert len(page1["data"]) == 1
    assert page1["has_more"] is True

    resp = await client.get(
        "/v1/sessions",
        params={"limit": 1, "order": "asc", "after": page1["data"][0]["id"]},
    )
    page2 = resp.json()
    assert len(page2["data"]) == 1
    assert page2["data"][0]["id"] != page1["data"][0]["id"]


async def test_list_sessions_kind_filter(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``kind`` scopes the list: ``default`` (the default) hides
    sub-agent children, ``sub_agent`` returns only them, and ``any``
    returns both. ``any`` powers the new-session agent picker's
    discovery of agents that are only bound to sub-agent sessions.
    """
    agent = await create_test_agent(client, name="kind-filter-agent")
    parent = await _create_session(client, agent["id"], title="kind-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="coder:kind-child",
        parent_conversation_id=parent["id"],
        agent_id=agent["id"],
    )

    # Omitting ``kind`` keeps the pre-param behavior: the sidebar's
    # view lists only top-level sessions. The child appearing here
    # would mean the default regressed to unfiltered.
    resp = await client.get("/v1/sessions", params={"agent_id": agent["id"]})
    assert resp.status_code == 200
    default_ids = {s["id"] for s in resp.json()["data"]}
    assert parent["id"] in default_ids
    assert child.id not in default_ids

    # kind=any returns the union, and the child row carries its agent
    # binding — the field the picker harvests for discovery.
    resp = await client.get("/v1/sessions", params={"agent_id": agent["id"], "kind": "any"})
    assert resp.status_code == 200
    any_rows = {s["id"]: s for s in resp.json()["data"]}
    assert parent["id"] in any_rows
    assert child.id in any_rows
    assert any_rows[child.id]["agent_id"] == agent["id"]

    # kind=sub_agent returns only the child for this agent.
    resp = await client.get("/v1/sessions", params={"agent_id": agent["id"], "kind": "sub_agent"})
    assert resp.status_code == 200
    sub_ids = {s["id"] for s in resp.json()["data"]}
    assert sub_ids == {child.id}

    # Values outside default|sub_agent|any are rejected by the Query
    # pattern, not silently passed through to the store.
    resp = await client.get("/v1/sessions", params={"kind": "bogus"})
    assert resp.status_code == 422


async def test_list_sessions_includes_title_and_status(
    client: httpx.AsyncClient,
) -> None:
    """Each list item has title, status, labels, and timestamps."""
    agent = await create_test_agent(client)
    await _create_session(client, agent["id"], title="titled-session")

    resp = await client.get("/v1/sessions")
    data = resp.json()["data"]
    assert len(data) >= 1
    item = data[0]
    assert "title" in item
    assert "status" in item
    assert "created_at" in item
    assert "updated_at" in item
    assert item["status"] in ("idle", "running", "failed")


async def test_list_sessions_includes_workspace_and_host_id(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``GET /v1/sessions`` surfaces each session's ``workspace`` and
    ``host_id``.

    The Web UI's new-session dialog reads ``workspace`` to warn when a
    new session would share a working directory with an existing one.
    These fields are populated from the conversation row; we bind a
    host + workspace directly via the store (the create path requires a
    live host tunnel to validate the workspace).

    Covers both shapes:
    - A bound session surfaces ``workspace`` + ``host_id``. A regression
      that dropped ``workspace=conv.workspace`` from the list builder
      would fail the value assertion below.
    - An unbound session omits both keys (the route serializes with
      ``exclude_none``), which is the shape the UI reads as "not bound to
      a directory" (``Conversation.workspace`` optional).

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI, shared with the app's
        store so a write here is visible to the route.
    """
    agent = await create_test_agent(client)
    bound = await _create_session(client, agent["id"], title="bound-session")
    # Left unbound (no host/workspace) to pin the omitted-keys case.
    unbound = await _create_session(client, agent["id"], title="unbound-session")

    # Register the host first — conversations.host_id is an FK to the
    # hosts table, so binding a non-existent host_id would fail the FK.
    host = HostStore(db_uri).upsert_on_connect("host_test", "test-laptop", "owner@example.com")
    # set_host_id writes host_id + workspace together (the
    # ck_conversations_workspace_required_for_host constraint forbids a
    # host_id with a NULL workspace).
    SqlAlchemyConversationStore(db_uri).set_host_id(
        bound["id"], host_id=host.host_id, workspace="/Users/me/repo"
    )

    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    items_by_id = {s["id"]: s for s in resp.json()["data"]}

    assert bound["id"] in items_by_id, (
        f"bound session {bound['id']} missing from list; got {list(items_by_id)}"
    )
    bound_item = items_by_id[bound["id"]]
    # Both must round-trip the stored row; workspace is the field the
    # conflict hint depends on, host_id scopes the comparison per host.
    assert bound_item["workspace"] == "/Users/me/repo"
    assert bound_item["host_id"] == host.host_id

    assert unbound["id"] in items_by_id, (
        f"unbound session {unbound['id']} missing from list; got {list(items_by_id)}"
    )
    unbound_item = items_by_id[unbound["id"]]
    # exclude_none drops unset fields entirely — not present, not null.
    assert "workspace" not in unbound_item
    assert "host_id" not in unbound_item


@pytest.mark.parametrize(
    "cached_status,expected_list_status",
    [
        ("running", "running"),
        # NO_DBOS path: parent turn parked on async-work drain.
        # Sidebar treats this as "something is happening" — wire
        # type doesn't carry "waiting" so it normalizes to "running".
        ("waiting", "running"),
        ("failed", "failed"),
        ("idle", "idle"),
    ],
)
async def test_list_sessions_reflects_relay_status_cache(
    client: httpx.AsyncClient,
    cached_status: str,
    expected_list_status: str,
) -> None:
    """The list endpoint reads ``_session_status_cache`` so the sidebar
    spinner reflects the runner-relayed live status, not the (mostly
    empty) ``tasks`` table that the NO_DBOS path no longer writes to.
    """
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    sessions_module._session_status_cache[session["id"]] = cached_status
    try:
        resp = await client.get("/v1/sessions")
        items = resp.json()["data"]
        item = next(s for s in items if s["id"] == session["id"])
        assert item["status"] == expected_list_status
    finally:
        sessions_module._session_status_cache.pop(session["id"], None)


async def test_list_sessions_rolls_up_busy_child_status(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``GET /v1/sessions`` reports a parent row as running while any
    direct sub-agent child is running.

    The sidebar gets non-active row status from the list endpoint and
    ``WS /v1/sessions/updates``, not from ``child_sessions``. Seeding a
    real child conversation plus the relay-fed status cache proves the
    parent roll-up happens in the shared list-item builder and that an
    unrelated sibling parent stays idle.
    """
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    parent = await _create_session(client, agent["id"], title="parent")
    other = await _create_session(client, agent["id"], title="other-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="coder:auth",
        parent_conversation_id=parent["id"],
        agent_id=agent["id"],
    )

    sessions_module._session_status_cache.pop(parent["id"], None)
    sessions_module._session_status_cache.pop(other["id"], None)
    sessions_module._session_status_cache[child.id] = "waiting"
    try:
        resp = await client.get("/v1/sessions")
        assert resp.status_code == 200
        items_by_id = {item["id"]: item for item in resp.json()["data"]}

        assert items_by_id[parent["id"]]["status"] == "running"
        assert items_by_id[other["id"]]["status"] == "idle"
    finally:
        sessions_module._session_status_cache.pop(parent["id"], None)
        sessions_module._session_status_cache.pop(other["id"], None)
        sessions_module._session_status_cache.pop(child.id, None)


async def test_session_snapshot_defaults_terminal_pending_false(
    client: httpx.AsyncClient,
) -> None:
    """A session with no entry in ``_session_terminal_pending_cache``
    snapshots ``terminal_pending=False``.

    This is the steady state for every non-terminal-first session and
    for a terminal-first session once its terminal has landed — the
    Web UI must not show a spinner. Asserting the concrete ``False``
    (not just truthiness) proves the snapshot builder ships the field
    with the right default rather than omitting it.
    """
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sessions_module._session_terminal_pending_cache.pop(session["id"], None)

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    assert snap.json()["terminal_pending"] is False


async def test_session_snapshot_reflects_terminal_pending_cache(
    client: httpx.AsyncClient,
) -> None:
    """The GET snapshot reads ``_session_terminal_pending_cache`` so a
    client connecting mid-spin-up sees ``terminal_pending=True``.

    This is the reconnect channel: the Omnigent session stream has no replay
    buffer, so a client that connects after the runner emitted the
    pending event would otherwise miss it. Re-reading via GET proves
    the value travels cache → response builder → snapshot.
    """
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    sessions_module._session_terminal_pending_cache[session["id"]] = True
    try:
        snap = await client.get(f"/v1/sessions/{session['id']}")
        assert snap.status_code == 200
        assert snap.json()["terminal_pending"] is True
    finally:
        sessions_module._session_terminal_pending_cache.pop(session["id"], None)


async def test_external_session_status_event_lands_in_status_cache(
    client: httpx.AsyncClient,
) -> None:
    """Posting ``external_session_status`` (the claude-native forwarder's
    only signal that a Claude turn is running) must update
    ``_session_status_cache`` so the list endpoint reflects it. Before
    this fix the handler only published to the SSE pub-sub, leaving the
    sidebar stuck on "idle" while the chat showed "Working…".
    """
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sessions_module._session_status_cache.pop(session["id"], None)

    try:
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "running"},
            },
        )
        # 202 Accepted (background) is the route's success contract;
        # 200 OK acceptable too.
        assert resp.status_code in (200, 202)

        list_resp = await client.get("/v1/sessions")
        item = next(s for s in list_resp.json()["data"] if s["id"] == session["id"])
        assert item["status"] == "running"
    finally:
        sessions_module._session_status_cache.pop(session["id"], None)


# ── POST /v1/sessions/{id}/events external_subagent_start ─────────


async def test_external_subagent_start_mints_child_session(
    client: httpx.AsyncClient,
) -> None:
    """
    Posting ``external_subagent_start`` to a claude-native parent
    creates a ``kind="sub_agent"`` child Conversation, returns its
    id, and the child surfaces in
    ``GET /v1/sessions/{parent_id}/child_sessions``.

    The forwarder uses this signal to register Claude Code's own
    Task-tool subagents (which never POST to Omnigent themselves) so the
    Subagents rail can show them.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )

    resp = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": "a5c7effac5a9a35ab",
                "agent_type": "Explore",
                "description": "Trace the auth flow",
                "tool_use_id": "toolu_016PwCnwmHv8h7kVpWLVd9YX",
            },
        },
    )
    assert resp.status_code in (200, 202), f"unexpected status {resp.status_code}: {resp.text}"
    body = resp.json()
    child_id = body["child_session_id"]
    assert child_id.startswith("conv_")
    assert body["queued"] is False

    # The child should appear under the parent in child_sessions.
    # Fields proven elsewhere; here we just pin the linkage + identity.
    children_resp = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    children = children_resp.json()["data"]
    matching = [c for c in children if c["id"] == child_id]
    assert len(matching) == 1, f"child {child_id} not in {children!r}"
    child = matching[0]
    assert child["parent_session_id"] == parent["id"]
    assert child["kind"] == "sub_agent"
    # Title format mirrors omnigent-spawned children (``tool:name``).
    # The session_name half encodes ``subagent_id`` so two children
    # with the same agent_type + description don't collide on the
    # ``(parent, title)`` unique index (the LLM routinely emits the
    # same description for parallel Task spawns).
    assert child["tool"] == "Explore"
    assert child["session_name"] == "a5c7effac5a9a35ab"
    # Description is preserved on the row's labels for surfaces that
    # want it; the rail's row UI ignores ``session_name``.
    assert child["labels"]["omnigent.claude_native.description"] == "Trace the auth flow"


async def test_external_subagent_start_handles_duplicate_agent_type_and_description(
    client: httpx.AsyncClient,
) -> None:
    """
    Two distinct sub-agents with the same ``agent_type`` +
    ``description`` (but different ``subagent_id``) must both
    register successfully. Claude's Task tool routinely spawns
    parallel sub-agents with identical ``agentType`` /
    ``description`` strings (e.g. three "general-purpose: Tell a
    joke"), and a naïve ``title = f"{agent_type}:{description}"``
    collides on the conversation store's
    ``(parent_conversation_id, title)`` unique index. Pinning this
    case so the title format stays per-sub-agent-id-unique.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )

    common_data = {
        "agent_type": "general-purpose",
        "description": "Tell a joke",
    }
    first = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                **common_data,
                "subagent_id": "a03186614301289fb",
                "tool_use_id": "toolu_first",
            },
        },
    )
    second = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                **common_data,
                "subagent_id": "aefb9a13a81715740",
                "tool_use_id": "toolu_second",
            },
        },
    )
    assert first.status_code in (200, 202), f"first registration failed: {first.text}"
    assert second.status_code in (200, 202), f"second registration failed: {second.text}"
    first_id = first.json()["child_session_id"]
    second_id = second.json()["child_session_id"]
    assert first_id != second_id

    # Both children appear under the parent.
    children = (await client.get(f"/v1/sessions/{parent['id']}/child_sessions")).json()["data"]
    child_ids = {c["id"] for c in children}
    assert {first_id, second_id} <= child_ids


async def test_external_subagent_start_is_idempotent_on_subagent_id(
    client: httpx.AsyncClient,
) -> None:
    """
    Two POSTs carrying the same ``subagent_id`` resolve to the same
    child row. The forwarder retries on transient HTTP errors so the
    handler must NOT mint a duplicate when the cursor hasn't yet
    been written.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )
    payload = {
        "type": "external_subagent_start",
        "data": {
            "subagent_id": "a5c7effac5a9a35ab",
            "agent_type": "Explore",
            "description": "Trace the auth flow",
            "tool_use_id": "toolu_016PwCnwmHv8h7kVpWLVd9YX",
        },
    }

    first = await client.post(f"/v1/sessions/{parent['id']}/events", json=payload)
    second = await client.post(f"/v1/sessions/{parent['id']}/events", json=payload)
    assert first.json()["child_session_id"] == second.json()["child_session_id"]

    children = (await client.get(f"/v1/sessions/{parent['id']}/child_sessions")).json()["data"]
    # Pin "exactly one" rather than ">= 1" — a duplicate would slip
    # past >= without a failure here.
    assert len(children) == 1


async def test_external_subagent_start_adopts_unlabeled_title_collision(
    client: httpx.AsyncClient,
) -> None:
    """
    Redelivery adopts (and heals) an existing child row that carries the
    colliding title but no ``subagent_id`` label.

    This is the production wedge behind recurring POST /events 500s: an
    earlier registration died between ``create_conversation`` and
    ``set_labels``, leaving a row the label-based idempotency lookup
    can't see. Every forwarder retry then tripped the
    ``(parent, title)`` unique index, the unhandled
    ``NameAlreadyExistsError`` became a 500, and after retry exhaustion
    the forwarder parked the sub-agent — it silently never appeared in
    the rail. The handler must adopt the row by title, re-stamp its
    labels, and return its id instead.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )

    # Seed the wedge state through the public create endpoint: a
    # ``sub_agent`` child carrying the exact collision title but none of
    # the claude-native labels — exactly what a crash between
    # ``create_conversation`` and ``set_labels`` leaves behind.
    seeded = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent["id"],
            "title": "Explore:a5c7effac5a9a35ab",
        },
    )
    assert seeded.status_code == 201, seeded.text
    seeded_id = seeded.json()["id"]

    payload = {
        "type": "external_subagent_start",
        "data": {
            "subagent_id": "a5c7effac5a9a35ab",
            "agent_type": "Explore",
            "description": "Trace the auth flow",
            "tool_use_id": "toolu_016PwCnwmHv8h7kVpWLVd9YX",
        },
    }
    resp = await client.post(f"/v1/sessions/{parent['id']}/events", json=payload)
    # 200/202, NOT 500 — the duplicate-title collision must be adopted,
    # not escape as an unhandled NameAlreadyExistsError.
    assert resp.status_code in (200, 202), f"unexpected status {resp.status_code}: {resp.text}"
    assert resp.json()["child_session_id"] == seeded_id

    children = (await client.get(f"/v1/sessions/{parent['id']}/child_sessions")).json()["data"]
    # Exactly one child: adoption must not mint a sibling row.
    assert len(children) == 1
    child = children[0]
    assert child["id"] == seeded_id
    # Labels are healed so the NEXT delivery resolves via the fast
    # label lookup instead of re-tripping the unique index.
    assert child["labels"]["omnigent.claude_native.subagent_id"] == "a5c7effac5a9a35ab"
    assert child["labels"]["omnigent.claude_native.description"] == "Trace the auth flow"

    # Redelivery now takes the label-lookup path to the same id.
    again = await client.post(f"/v1/sessions/{parent['id']}/events", json=payload)
    assert again.json()["child_session_id"] == seeded_id


async def test_external_subagent_start_idempotency_pages_beyond_first_100(
    client: httpx.AsyncClient,
) -> None:
    """
    Idempotency must page through all children, not just the newest 100.
    A parent with > 100 sub-agents that retries an older ``subagent_id``
    would otherwise miss the existing labeled row and trip the
    ``(parent, title)`` unique constraint on re-create.

    We mint the older sub-agent first, push more than ``limit`` newer
    ones on top, then re-post the older payload and assert the same
    child id comes back (one row total for that id).
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )

    older = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": "older-subagent",
                "agent_type": "Explore",
                "description": "First one",
                "tool_use_id": "toolu_older",
            },
        },
    )
    older_child_id = older.json()["child_session_id"]

    # 100 mirrors the page limit; push enough to require a second page.
    for index in range(101):
        await client.post(
            f"/v1/sessions/{parent['id']}/events",
            json={
                "type": "external_subagent_start",
                "data": {
                    "subagent_id": f"newer-{index}",
                    "agent_type": "Explore",
                    "description": f"Newer {index}",
                    "tool_use_id": f"toolu_newer_{index}",
                },
            },
        )

    repeat = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": "older-subagent",
                "agent_type": "Explore",
                "description": "First one",
                "tool_use_id": "toolu_older",
            },
        },
    )
    # Without pagination the older sub-agent would fall off the first
    # 100 results, the handler would fall through to ``create_conversation``,
    # and the ``(parent, title)`` unique constraint would either fail the
    # request or mint a fresh id. Either way ``child_session_id`` would
    # diverge from the original.
    assert repeat.json()["child_session_id"] == older_child_id


@pytest.mark.parametrize(
    "missing_key",
    ["subagent_id", "agent_type", "description", "tool_use_id"],
)
async def test_external_subagent_start_rejects_missing_required_keys(
    client: httpx.AsyncClient,
    missing_key: str,
) -> None:
    """
    A POST missing any of the four required ``data`` keys returns
    400 — payload validation is at the route boundary so the handler
    body always sees a complete record.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )
    data = {
        "subagent_id": "a5c7effac5a9a35ab",
        "agent_type": "Explore",
        "description": "Trace the auth flow",
        "tool_use_id": "toolu_016PwCnwmHv8h7kVpWLVd9YX",
    }
    data.pop(missing_key)
    resp = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={"type": "external_subagent_start", "data": data},
    )
    assert resp.status_code == 400
    # Error message must name the missing field so the forwarder's
    # logs point at the bug rather than reading "Invalid payload".
    assert missing_key in resp.text


# ── POST /v1/sessions/{id}/events skill slash_command ─────


async def test_skill_slash_command_persists_visible_item_and_hidden_meta_message(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Structured skill slash commands persist two durable records.

    The visible ``slash_command`` item is what UI/TUI transcripts
    render. The hidden ``message`` item carries the full skill body
    with ``is_meta=True`` so runner resume/history replay still has
    the skill context without showing raw instructions to users.
    """
    from omnigent.server.routes import sessions as sessions_module

    forwarded: list[dict[str, Any]] = []
    published: list[tuple[str, dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Emulate the runner: resolve skills and capture event posts.

        Skill content is runner-owned, so the server resolves the
        ``<skill>`` meta text via ``POST /skills/resolve`` before
        persisting/forwarding. This fake mirrors that endpoint with the
        real :func:`format_skill_meta_text`, then captures the forwarded
        turn input on ``/events``.

        :param request: Request sent to the fake runner.
        :returns: Resolved meta text (``/skills/resolve``) or an
            accepted response (``/events``).
        """
        if request.method == "POST" and request.url.path.endswith("/skills/resolve"):
            payload = json.loads(request.content)
            skill = SkillSpec(
                name="grill-me",
                description="Stress-test a plan.",
                content="Ask sharp questions one at a time.",
            )
            return httpx.Response(
                200,
                json={"meta_text": format_skill_meta_text(skill, payload.get("arguments", ""))},
            )
        if request.method == "POST" and request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> httpx.AsyncClient:
        """
        Resolve every session to the fake runner client.

        :param session_id: Session id being routed.
        :param runner_router: Real app runner router, unused here.
        :returns: The fake runner client.
        """
        del session_id, runner_router
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    try:
        agent = await create_test_agent(
            client,
            name="skill-agent",
            skills=[
                {
                    "name": "grill-me",
                    "description": "Stress-test a plan.",
                    "content": "Ask sharp questions one at a time.",
                }
            ],
        )
        session = await _create_session(client, agent["id"])

        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "slash_command",
                "data": {
                    "kind": "skill",
                    "name": "grill-me",
                    "arguments": "review this rollout",
                },
            },
        )
        assert resp.status_code == 202, resp.text
    finally:
        await fake_runner.aclose()

    assert resp.json()["queued"] is True
    assert resp.json()["item_id"].startswith("sc_")

    items_resp = await client.get(f"/v1/sessions/{session['id']}/items")
    assert items_resp.status_code == 200, items_resp.text
    items = items_resp.json()["data"]
    visible = next(item for item in items if item["type"] == "slash_command")
    meta = next(item for item in items if item["type"] == "message" and item.get("is_meta"))
    assert visible["name"] == "grill-me"
    assert visible["kind"] == "skill"
    assert visible["arguments"] == "review this rollout"
    assert visible["response_id"] == meta["response_id"]
    text = meta["content"][0]["text"]
    assert "<skill>" in text
    assert "<name>grill-me</name>" in text
    assert "Ask sharp questions one at a time." in text
    assert "<user_request>\nreview this rollout\n</user_request>" in text
    assert "Use the load_skill tool" not in text

    assert forwarded == [
        {
            "type": "message",
            "role": "user",
            "content": meta["content"],
            "agent_id": agent["id"],
            "model": "skill-agent",
            "has_mcp_servers": False,
            # The forwarded message is the meta item; its store id lets the
            # runner dedup it on a cold-cache history reload.
            "persisted_item_id": meta["id"],
        }
    ]
    event_types = [event["type"] for _, event in published]
    assert event_types == ["response.output_item.done"]
    assert published[0][1]["item"]["type"] == "slash_command"
    assert published[0][1]["item"]["id"] == visible["id"]

    # The dispatch also seeds the sidebar title from the typed command,
    # mirroring the plain-message path. Without it, a session whose FIRST
    # message is a skill invocation (web landing composer, REPL) keeps a
    # NULL title and the UI falls back to the conversation id. The title
    # must come from the visible "/name args" text — the hidden meta
    # item's SKILL.md blob leaking here would show skill instructions in
    # the sidebar.
    session_resp = await client.get(f"/v1/sessions/{session['id']}")
    assert session_resp.status_code == 200, session_resp.text
    assert session_resp.json()["title"] == "/grill-me review this rollout"


async def test_skill_slash_command_keeps_existing_title(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Skill title seeding fills only the empty slot.

    A session that already has a title (user-assigned at create, or
    seeded from an earlier first message) must keep it when a skill is
    invoked later — otherwise every mid-conversation ``/skill`` send
    would rename the session in the sidebar.
    """
    from omnigent.server.routes import sessions as sessions_module

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Emulate the runner: resolve the skill, accept event posts.

        :param request: Request sent to the fake runner.
        :returns: Resolved meta text (``/skills/resolve``) or an
            accepted response (``/events``).
        """
        if request.method == "POST" and request.url.path.endswith("/skills/resolve"):
            skill = SkillSpec(
                name="grill-me",
                description="Stress-test a plan.",
                content="Ask sharp questions one at a time.",
            )
            return httpx.Response(200, json={"meta_text": format_skill_meta_text(skill, "")})
        return httpx.Response(202, json={"queued": True})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> httpx.AsyncClient:
        """
        Resolve every session to the fake runner client.

        :param session_id: Session id being routed.
        :param runner_router: Real app runner router, unused here.
        :returns: The fake runner client.
        """
        del session_id, runner_router
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    try:
        agent = await create_test_agent(
            client,
            name="skill-agent-titled",
            skills=[
                {
                    "name": "grill-me",
                    "description": "Stress-test a plan.",
                    "content": "Ask sharp questions one at a time.",
                }
            ],
        )
        session = await _create_session(client, agent["id"], title="my rollout session")

        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "slash_command",
                "data": {"kind": "skill", "name": "grill-me", "arguments": ""},
            },
        )
        assert resp.status_code == 202, resp.text
    finally:
        await fake_runner.aclose()

    session_resp = await client.get(f"/v1/sessions/{session['id']}")
    assert session_resp.status_code == 200, session_resp.text
    # Unchanged: the seed is a no-op when a title exists. "/grill-me"
    # appearing here would mean the skill path overwrites user titles.
    assert session_resp.json()["title"] == "my rollout session"


async def test_skill_slash_command_non_json_resolve_surfaces_controlled_error(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A non-JSON ``/skills/resolve`` body (e.g. an HTML error page injected
    by a proxy) surfaces as a controlled ``OmnigentError`` (HTTP 500
    with our message), not an uncaught crash with the generic
    "An internal error occurred." body.
    """
    from omnigent.server.routes import sessions as sessions_module

    def _handler(request: httpx.Request) -> httpx.Response:
        """Return a non-JSON body for resolve; 202 otherwise."""
        if request.url.path.endswith("/skills/resolve"):
            return httpx.Response(200, content=b"<html>not json</html>")
        return httpx.Response(202, json={"queued": True})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(session_id: str, runner_router: object) -> httpx.AsyncClient:
        """Resolve every session to the fake runner."""
        del session_id, runner_router
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    try:
        agent = await create_test_agent(
            client,
            name="skill-agent-badjson",
            skills=[{"name": "grill-me", "description": "Stress-test a plan.", "content": "Ask."}],
        )
        session = await _create_session(client, agent["id"])
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "slash_command",
                "data": {"kind": "skill", "name": "grill-me", "arguments": ""},
            },
        )
    finally:
        await fake_runner.aclose()

    assert resp.status_code == 500, resp.text
    # Our controlled message, not the generic "An internal error occurred."
    assert "malformed skill resolution" in resp.json()["error"]["message"]


async def test_external_meta_user_message_persists_without_live_input_event(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    External bridge meta messages are durable but hidden from live UI.

    Codex-native mirrors ``<skill>`` wrappers via
    ``external_conversation_item``. The server must store those
    messages so resume has the context, while suppressing
    ``session.input.consumed`` so subscribers do not render raw skill
    text.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "<skill>hidden</skill>"}],
                    "is_meta": True,
                },
                "response_id": "codex_turn_123",
                "source_id": "codex-skill-meta",
            },
        },
    )
    assert resp.status_code == 202, resp.text

    items = (await client.get(f"/v1/sessions/{session['id']}/items")).json()["data"]
    meta = next(item for item in items if item["type"] == "message")
    assert meta["is_meta"] is True
    assert meta["content"][0]["text"] == "<skill>hidden</skill>"
    assert published == []


async def test_external_user_message_folds_pending_image_into_durable_item(
    client: httpx.AsyncClient,
) -> None:
    """
    Regression: a native web message's image survives in durable history.

    The native transcript mirrors a user message back text-only
    (``external_conversation_item`` with no image block), so the image —
    which the client uploaded and POSTed — would be dropped from
    conversation history and vanish on every reload. The pending-input
    entry recorded at web-POST time still carries the image block (with
    its real ``file_id``); when the transcript item persists, the server
    folds that block into the durable item.

    Here we seed the pending entry directly (standing in for the prior
    native web POST), then post the text-only transcript mirror and
    assert the persisted item carries BOTH the image block and the text.
    Without the fold, the stored item is text-only and the image is gone.
    """
    from omnigent.runtime import pending_inputs

    pending_inputs.reset_for_tests()
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    # The optimistic pending entry the web composer recorded on POST:
    # image (real upload id) + caption text.
    pending_inputs.record(
        session["id"],
        [
            {"type": "input_image", "file_id": "file_real_99", "filename": "diagram.png"},
            {"type": "input_text", "text": "explain this diagram"},
        ],
    )
    try:
        # The transcript forwarder mirrors the message back TEXT-ONLY.
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "[Attached: /tmp/diagram.png]\n\nexplain this diagram",
                            }
                        ],
                    },
                    "response_id": "native_turn_1",
                },
            },
        )
        assert resp.status_code == 202, resp.text

        items = (await client.get(f"/v1/sessions/{session['id']}/items")).json()["data"]
        user_msg = next(item for item in items if item["type"] == "message")
        # The image block was folded back in, ahead of the transcript text
        # — so reloading history shows the image, not just the caption.
        assert user_msg["content"][0] == {
            "type": "input_image",
            "file_id": "file_real_99",
            "filename": "diagram.png",
        }
        expected_text = "[Attached: /tmp/diagram.png]\n\nexplain this diagram"
        assert user_msg["content"][1]["text"] == expected_text
        # The pending entry was drained — it won't double-render on rebind.
        assert pending_inputs.snapshot_for(session["id"]) == []
    finally:
        pending_inputs.reset_for_tests()


async def test_external_user_message_drain_publishes_cleared_pending_id(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Draining a pending entry publishes its id on session.input.consumed.

    The client's drop-by-id promotion depends on the server echoing the
    drained entry's id as ``cleared_pending_id`` on the consumed event.
    This pins that wire contract end-to-end: record a pending entry,
    post the (text-only) transcript mirror, and assert the published
    ``session.input.consumed`` carries ``cleared_pending_id`` equal to
    the recorded id. A regression here silently breaks the bubble swap
    (the client falls back to FIFO and the entry can strand).
    """
    from omnigent.runtime import pending_inputs

    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    pending_inputs.reset_for_tests()
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    pid = pending_inputs.record(session["id"], [{"type": "input_text", "text": "hi there"}])
    try:
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "item_data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi there"}],
                    },
                    "response_id": "native_turn_1",
                },
            },
        )
        assert resp.status_code == 202, resp.text

        consumed = [ev for _sid, ev in published if ev.get("type") == "session.input.consumed"]
        assert len(consumed) == 1, f"expected one consumed event, got {published}"
        # The drained entry's id is echoed so the client drops that bubble by id.
        assert consumed[0]["data"]["cleared_pending_id"] == pid
        # And the entry is gone from the index.
        assert pending_inputs.snapshot_for(session["id"]) == []
    finally:
        pending_inputs.reset_for_tests()


# ── PATCH /v1/sessions/{id} ─────────────────────────────


async def test_patch_session_updates_title(
    client: httpx.AsyncClient,
) -> None:
    """PATCH updates title and returns the updated snapshot."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"], title="old")

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"title": "new title"},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "new title"


async def test_patch_session_updates_labels(
    client: httpx.AsyncClient,
) -> None:
    """PATCH upserts labels (merges, doesn't replace)."""
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        labels={"a": "1"},
    )

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"labels": {"b": "2"}},
    )
    assert resp.status_code == 200
    labels = resp.json()["labels"]
    assert labels["a"] == "1"
    assert labels["b"] == "2"


async def test_patch_session_archive_hides_from_default_list(
    client: httpx.AsyncClient,
) -> None:
    """
    Archiving via PATCH drops the session from the default
    ``GET /v1/sessions`` list and surfaces it only behind
    ``include_archived=true``; unarchiving restores it.

    This is the full round-trip the sidebar depends on. A failure on
    the "hidden" assertion means archived sessions leak into the
    default view; a failure on the "include_archived" assertion means
    the toggle can never surface them; a failure on the restore means
    unarchive is broken.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"], title="to-archive")
    sid = session["id"]

    # Archive it. The PATCH response reflects the new state.
    resp = await client.patch(f"/v1/sessions/{sid}", json={"archived": True})
    assert resp.status_code == 200
    assert resp.json()["archived"] is True

    # Default listing excludes it.
    default_ids = {s["id"] for s in (await client.get("/v1/sessions")).json()["data"]}
    assert sid not in default_ids, "archived session must be hidden from the default list"

    # include_archived=true surfaces it, flagged archived.
    archived_list = (await client.get("/v1/sessions", params={"include_archived": "true"})).json()[
        "data"
    ]
    archived_row = next((s for s in archived_list if s["id"] == sid), None)
    assert archived_row is not None, "include_archived=true must return the archived session"
    assert archived_row["archived"] is True, "list item must carry archived=true"

    # Unarchive restores it to the default list.
    resp = await client.patch(f"/v1/sessions/{sid}", json={"archived": False})
    assert resp.status_code == 200
    assert resp.json()["archived"] is False
    default_ids_after = {s["id"] for s in (await client.get("/v1/sessions")).json()["data"]}
    assert sid in default_ids_after, "unarchived session must reappear in the default list"


@pytest.mark.parametrize("reasoning_effort", ["high", "xhigh", "max"])
async def test_patch_session_updates_reasoning_effort(
    client: httpx.AsyncClient,
    reasoning_effort: str,
) -> None:
    """PATCH sets reasoning_effort on the session."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"reasoning_effort": reasoning_effort},
    )
    assert resp.status_code == 200
    assert resp.json()["reasoning_effort"] == reasoning_effort


async def test_create_session_sets_terminal_launch_args(
    client: httpx.AsyncClient,
) -> None:
    """
    JSON ``POST /v1/sessions`` persists terminal_launch_args, and the
    value round-trips through a later GET snapshot.

    This is the web permission-mode path: the New Chat dialog sends
    ``["--permission-mode", "bypassPermissions"]`` at create time so the
    runner has it on the session row before it auto-launches the
    terminal. Re-reading via GET (not just the create echo) proves the
    value travelled route → create_conversation → DB → response
    builder. The two-token list also exercises multi-element ordering.
    Before this change the JSON create path dropped the field entirely,
    so the GET would carry ``None`` and this would fail.
    """
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        terminal_launch_args=["--permission-mode", "bypassPermissions"],
    )
    # Create echo carries the persisted args.
    assert session["terminal_launch_args"] == ["--permission-mode", "bypassPermissions"]

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    # Round-trip from the DB confirms it wasn't only on the in-memory echo,
    # with order preserved (a reordered pair would break the CLI invocation).
    assert snap.json()["terminal_launch_args"] == ["--permission-mode", "bypassPermissions"]


async def test_create_session_without_terminal_launch_args_is_null(
    client: httpx.AsyncClient,
) -> None:
    """
    Omitting terminal_launch_args on JSON create leaves the column
    NULL — a non-native / auto-mode-off session stores nothing.

    A non-None value here would mean the create path invented args,
    which would silently change how the runner launches the terminal.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    assert session["terminal_launch_args"] is None


async def test_create_session_rejects_oversized_terminal_launch_args(
    client: httpx.AsyncClient,
) -> None:
    """
    JSON create rejects a terminal_launch_args list past the count cap.

    Pins the same server-side bound (``_MAX_TERMINAL_LAUNCH_ARGS``)
    that the PATCH path enforces, on the create path too. A success
    here would mean the create route skipped the shared validator,
    letting a caller bloat a session row arbitrarily.
    """
    agent = await create_test_agent(client)
    # 257 entries: one past the 256 cap, so the count check fires.
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "terminal_launch_args": ["--x"] * 257},
    )
    assert resp.status_code == 400
    assert "terminal_launch_args" in resp.text


async def test_patch_session_sets_terminal_launch_args(
    client: httpx.AsyncClient,
) -> None:
    """
    PATCH persists terminal_launch_args and it surfaces in a later
    GET snapshot.

    Re-reading via GET (not just trusting the PATCH echo) proves the
    value travelled route → store → DB → response builder. If the
    builder dropped the field or the store didn't persist it, the GET
    snapshot would carry ``None`` and this would fail — the exact gap
    that would leave a daemon-launched runner with no args.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"terminal_launch_args": ["--dangerously-skip-permissions", "--model", "opus"]},
    )
    assert resp.status_code == 200
    assert resp.json()["terminal_launch_args"] == [
        "--dangerously-skip-permissions",
        "--model",
        "opus",
    ]

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    assert snap.json()["terminal_launch_args"] == [
        "--dangerously-skip-permissions",
        "--model",
        "opus",
    ]


async def test_patch_session_replaces_terminal_launch_args(
    client: httpx.AsyncClient,
) -> None:
    """
    A second PATCH replaces terminal_launch_args wholesale rather than
    appending — the resume last-write-wins contract
    (designs/NATIVE_RUNNER_SERVER_LAUNCH.md).

    If the route/store appended, the final value would be
    ``["--model", "opus", "--verbose"]`` and this would fail — which
    is the bug that would make repeated resumes accumulate stale
    flags.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    first = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"terminal_launch_args": ["--model", "opus"]},
    )
    assert first.status_code == 200

    second = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"terminal_launch_args": ["--verbose"]},
    )
    assert second.status_code == 200
    assert second.json()["terminal_launch_args"] == ["--verbose"]


async def test_patch_session_rejects_oversized_terminal_launch_args(
    client: httpx.AsyncClient,
) -> None:
    """
    PATCH rejects a terminal_launch_args list past the count cap with
    a 400.

    This pins the server-side bound (``_MAX_TERMINAL_LAUNCH_ARGS``):
    without it a caller could bloat a single session row arbitrarily.
    A 200 here would mean the validator was bypassed.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    # 257 entries: one past the 256 cap, so the count check fires.
    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"terminal_launch_args": ["--x"] * 257},
    )
    assert resp.status_code == 400
    assert "terminal_launch_args" in resp.text


async def test_patch_session_clears_extended_reasoning_effort(
    client: httpx.AsyncClient,
) -> None:
    """PATCH clear aliases clear an extended reasoning_effort value."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    seed = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"reasoning_effort": "max"},
    )
    assert seed.status_code == 200
    assert seed.json()["reasoning_effort"] == "max"

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"reasoning_effort": "default"},
    )
    assert resp.status_code == 200
    assert resp.json()["reasoning_effort"] is None


async def test_patch_session_rejects_invalid_reasoning_effort(
    client: httpx.AsyncClient,
) -> None:
    """
    PATCH with an unsupported ``reasoning_effort`` value fails loud.

    The route validates the body against
    ``_SESSION_METADATA_REASONING_EFFORTS`` before any DB write, so the
    request is rejected with a typed 400 and the session row is left
    unchanged. Mirrors the POST-create validation contract — the two
    write paths must reject the same set of values.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    # Seed a known prior value so we can prove the row was untouched
    # by the rejected PATCH.
    seed = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"reasoning_effort": "low"},
    )
    assert seed.status_code == 200
    assert seed.json()["reasoning_effort"] == "low"

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"reasoning_effort": "bogus"},
    )
    # 400 + typed code: required by DESIGN_PRINCIPLES.md §5 "fail
    # loud." If this fails with 200, the route silently persisted
    # an unsupported value.
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    # Message surfaces the offending value and the supported set so
    # the caller can fix the request without reading server logs.
    assert "bogus" in error["message"]
    assert "none, minimal, low, medium, high, xhigh, max" in error["message"]

    # The DB row keeps the prior value — validation runs before any
    # write. If this assertion fails, the route mutated state on a
    # rejected request (partial-update bug).
    after = await client.get(f"/v1/sessions/{session['id']}")
    assert after.status_code == 200
    assert after.json()["reasoning_effort"] == "low"


async def test_patch_session_updates_multiple_fields(
    client: httpx.AsyncClient,
) -> None:
    """PATCH updates title, labels, and effort together."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={
            "title": "updated",
            "labels": {"k": "v"},
            "reasoning_effort": "low",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "updated"
    assert body["labels"]["k"] == "v"
    assert body["reasoning_effort"] == "low"


async def test_patch_session_404_for_nonexistent(
    client: httpx.AsyncClient,
) -> None:
    """PATCH returns 404 for a session that doesn't exist."""
    resp = await client.patch(
        "/v1/sessions/conv_nonexistent",
        json={"title": "nope"},
    )
    assert resp.status_code == 404


async def test_patch_session_sets_external_session_id(
    client: httpx.AsyncClient,
) -> None:
    """PATCH persists external_session_id and returns it in the snapshot."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"external_session_id": "a1b2c3d4-1234-5678-9abc-def012345678"},
    )
    assert resp.status_code == 200, resp.text
    # Response body is the updated snapshot — the wrapper bridge
    # reads this directly without a follow-up GET. If this fails
    # the route built the response from a stale entity.
    assert resp.json()["external_session_id"] == "a1b2c3d4-1234-5678-9abc-def012345678"

    after = await client.get(f"/v1/sessions/{session['id']}")
    assert after.status_code == 200
    # Independent GET proves the value was persisted (not just held
    # in the PATCH response).
    assert after.json()["external_session_id"] == "a1b2c3d4-1234-5678-9abc-def012345678"


async def test_patch_session_external_session_id_idempotent_same_value(
    client: httpx.AsyncClient,
) -> None:
    """Writing the same external_session_id twice is a no-op (200, no error)."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    first = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"external_session_id": "sid-1"},
    )
    assert first.status_code == 200, first.text
    second = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"external_session_id": "sid-1"},
    )
    # Both writes succeed; the second is a server-side no-op. If
    # this returned 400 we'd be tripping the overwrite guard on
    # legitimate re-mirrors (e.g. server bounce + fresh forwarder
    # process).
    assert second.status_code == 200, second.text
    assert second.json()["external_session_id"] == "sid-1"


async def test_patch_session_external_session_id_rejects_overwrite(
    client: httpx.AsyncClient,
) -> None:
    """
    Overwriting an already-set external_session_id fails 400.

    The store raises ValueError on overwrite; the route translates
    it to ``invalid_input``. The original value must survive — a
    silent overwrite would destroy the mapping the original
    wrapper bridge captured and make ``--resume`` unable to recover
    the prior external transcript.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    seed = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"external_session_id": "sid-1"},
    )
    assert seed.status_code == 200, seed.text

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"external_session_id": "sid-2"},
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    # Message names both ids so the caller's logs make the conflict
    # obvious without reading server-side state.
    assert "sid-1" in error["message"]
    assert "sid-2" in error["message"]

    after = await client.get(f"/v1/sessions/{session['id']}")
    assert after.status_code == 200
    # First-writer-wins after the rejected PATCH.
    assert after.json()["external_session_id"] == "sid-1"


async def test_create_session_returns_null_external_session_id(
    client: httpx.AsyncClient,
) -> None:
    """
    A freshly created session has external_session_id = null.

    Wrapper bridges depend on this default to tell "not yet
    observed" from "set to empty" — a non-null default would mean
    the bridge can't distinguish those states.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.get(f"/v1/sessions/{session['id']}")
    assert resp.status_code == 200
    assert resp.json()["external_session_id"] is None


async def test_list_sessions_includes_external_session_id(
    client: httpx.AsyncClient,
) -> None:
    """List items expose external_session_id so the sidebar can badge runtime."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"external_session_id": "sid-1"},
    )

    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    data = resp.json()["data"]
    found = next((row for row in data if row["id"] == session["id"]), None)
    assert found is not None, f"created session not in list: {data}"
    # Without this field on SessionListItem the picker/sidebar
    # would need a follow-up GET per row to know the runtime kind.
    assert found["external_session_id"] == "sid-1"


# ── claude-native session discovery (list + snapshot) ────────────


async def test_claude_native_session_discoverable_with_terminal_metadata(
    client: httpx.AsyncClient,
) -> None:
    """A claude-native session exposes the full identity bundle the Web
    UI needs to find and open it — on both the list and the snapshot."""
    agent = await create_test_agent(client, name="claude-native-ui")
    session = await _create_session(
        client,
        agent["id"],
        title="universe @ lakebox",
        # Labels the route's _is_claude_native_terminal_session and the
        # UI's terminal-first layout key off of.
        labels={
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "claude-code-native-ui",
        },
    )
    session_id = session["id"]

    # Forwarder mirrors Claude's runtime session uuid once Claude starts.
    patch = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"external_session_id": "11111111-2222-3333-4444-555555555555"},
    )
    assert patch.status_code == 200, patch.text

    # Sidebar list surface.
    list_resp = await client.get("/v1/sessions")
    assert list_resp.status_code == 200
    row = next((r for r in list_resp.json()["data"] if r["id"] == session_id), None)
    assert row is not None, f"session not in list: {list_resp.json()}"
    assert row["title"] == "universe @ lakebox"
    assert row["agent_name"] == "claude-native-ui"
    assert row["labels"]["omnigent.wrapper"] == "claude-code-native-ui"
    assert row["labels"]["omnigent.ui"] == "terminal"
    assert row["external_session_id"] == "11111111-2222-3333-4444-555555555555"
    assert row["status"] in ("idle", "running", "failed")

    # Snapshot surface (opening the session) carries the same bundle.
    snap = (await client.get(f"/v1/sessions/{session_id}")).json()
    assert snap["title"] == "universe @ lakebox"
    assert snap["agent_name"] == "claude-native-ui"
    assert snap["labels"]["omnigent.wrapper"] == "claude-code-native-ui"
    assert snap["labels"]["omnigent.ui"] == "terminal"
    assert snap["external_session_id"] == "11111111-2222-3333-4444-555555555555"


async def test_get_session_agent_name_is_spec_name_after_switch(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """After an in-place agent switch the snapshot reports the spec's name.

    The switch route binds the session to a clone row named
    ``"<builtin> (switch ag_…)"`` for agent-store disambiguation, but
    clients (REPL toolbar, web sidebar) display ``agent_name``
    verbatim — the snapshot must surface the spec's clean identity
    (e.g. ``"claude-native-ui"``), not the clone row's name.

    Drives the REAL switch route end-to-end: source session → seeded
    bindable built-in → ``POST .../switch-agent`` → ``GET`` snapshot.
    """
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore

    # Source session bound to a session-scoped "nessie" agent.
    source_agent = await create_test_agent(client, name="nessie")
    session_id = source_agent["_session_id"]

    # Materialize a real claude-native-ui bundle in the artifact store
    # (via a throwaway session-scoped agent), then register a TEMPLATE
    # (built-in, the only kind the switch route binds) sharing it.
    target_agent = await create_test_agent(client, name="claude-native-ui")
    agent_store = SqlAlchemyAgentStore(db_uri)
    target_row = agent_store.get(target_agent["id"])
    assert target_row is not None and target_row.bundle_location is not None
    builtin = agent_store.create(
        "ag_builtin_claude_test",
        "claude-native-ui",
        target_row.bundle_location,
    )

    resp = await client.post(
        f"/v1/sessions/{session_id}/switch-agent",
        json={"agent_id": builtin.id},
    )
    assert resp.status_code == 200, resp.text

    snap = (await client.get(f"/v1/sessions/{session_id}")).json()
    # Preconditions that make this test meaningful: the session is
    # bound to a freshly created CLONE whose row name carries the
    # "(switch …)" disambiguation suffix — i.e. row name ≠ spec name.
    clone_row = agent_store.get(snap["agent_id"])
    assert clone_row is not None
    assert clone_row.name.startswith("claude-native-ui (switch "), (
        f"Expected the switch route to bind a suffixed clone row; got "
        f"{clone_row.name!r}. If unsuffixed, this test no longer covers "
        f"the row-name/spec-name divergence and needs a new setup."
    )
    # The snapshot prefers the spec's clean name over the clone row's.
    # The suffixed name here means clients (REPL toolbar, sidebar)
    # would display "claude-native-ui (switch ag_…)" to the user.
    assert snap["agent_name"] == "claude-native-ui"


async def test_list_sessions_exposes_pending_elicitations_count(
    client: httpx.AsyncClient,
) -> None:
    """
    ``pending_elicitations_count`` reflects outstanding approval
    prompts so the sidebar can badge sessions whose chat isn't open.

    Drives the index through its public API
    (``record_publish`` + ``resolve``) — the route handler reads
    the same in-process dict the publish-time side-channel
    populates, so this exercises the full wiring without needing
    a runner / harness in the loop.

    A regression that drops the field off ``SessionListItem``,
    fails to populate it in ``list_sessions``, or breaks the
    index's lookup would surface as a missing or stale badge —
    invisible to users until they realize sessions are blocked
    and they had no idea.
    """
    from omnigent.runtime import pending_elicitations

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    session_id = session["id"]

    # Sanity: a freshly created session has no outstanding prompts.
    # Without this baseline, a leaked entry from another test could
    # mask the assertions below.
    pending_elicitations.reset_for_tests()
    resp = await client.get("/v1/sessions")
    assert resp.status_code == 200
    row = next((r for r in resp.json()["data"] if r["id"] == session_id), None)
    assert row is not None, f"created session not in list: {resp.json()}"
    # Field present, default 0 — guards against accidentally
    # dropping the field with ``model_dump(exclude_none=True)``
    # because 0 is not None and must serialize through.
    assert row["pending_elicitations_count"] == 0

    # Simulate the AP-server side-channel registering an outstanding
    # elicitation (the same call ``session_stream.publish`` makes
    # internally for a real ``response.elicitation_request`` event).
    pending_elicitations.record_publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_route_test",
        },
    )
    resp = await client.get("/v1/sessions")
    row = next((r for r in resp.json()["data"] if r["id"] == session_id), None)
    assert row is not None
    # 1 = the route handler invoked ``counts_for`` against the index
    # and populated the field. If still 0, the handler isn't reading
    # the index; if absent, the schema dropped the field on the wire.
    assert row["pending_elicitations_count"] == 1, (
        f"Expected 1 pending after record_publish, got "
        f"{row['pending_elicitations_count']!r}. If 0, the handler "
        f"isn't reading pending_elicitations.counts_for; if KeyError, "
        f"the field is missing from SessionListItem."
    )

    # Verdict accepted → count clears.
    pending_elicitations.resolve(session_id, "elicit_route_test")
    resp = await client.get("/v1/sessions")
    row = next((r for r in resp.json()["data"] if r["id"] == session_id), None)
    assert row is not None
    # 0 = decrement landed. If still 1, the approval-dispatch path
    # in production won't clear the badge either — the sidebar
    # would show a stale prompt forever.
    assert row["pending_elicitations_count"] == 0

    pending_elicitations.reset_for_tests()


async def test_get_session_includes_runner_online(
    client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/sessions/{id}`` carries session-scoped runner liveness.

    Direct attach/resume clients use the single-session snapshot, not the
    sidebar list. If this field is absent there, they fall back to worse
    liveness sources and can misclassify a shared live session as offline.

    :param client: Test HTTP client backed by the real app.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.get(f"/v1/sessions/{session['id']}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == session["id"]
    assert body["runner_online"] is True


async def test_get_session_slim_skips_items_and_liveness(
    client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/sessions/{id}?include_items=false&include_liveness=false``
    returns the snapshot without the committed-items read and without
    the liveness lookup.

    The web chat surface hydrates the transcript via the paginated
    items endpoint and sources liveness from the /health poll, so the
    slim snapshot lets it skip the two most expensive build steps. The
    default (no params) must keep returning both so existing clients
    (REPL resume, direct attach) are unaffected.

    :param client: Test HTTP client backed by the real app.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"], initial_message="hello")
    session_id = session["id"]
    await _wait_for_idle(client, session_id)

    slim = await client.get(
        f"/v1/sessions/{session_id}",
        params={"include_items": "false", "include_liveness": "false"},
    )
    assert slim.status_code == 200
    slim_body = slim.json()
    assert slim_body["id"] == session_id
    # Items read skipped — empty list, not an absent field, so clients
    # that parse `items ?? []` see the same shape either way.
    assert slim_body["items"] == []
    # Liveness lookup skipped — both fields unset (serialized as absent
    # or null, never a fabricated False).
    assert slim_body.get("runner_online") is None
    assert slim_body.get("host_online") is None

    # Default behavior unchanged: the seeded message is in the snapshot
    # and liveness is computed.
    full = await client.get(f"/v1/sessions/{session_id}")
    assert full.status_code == 200
    full_body = full.json()
    # Snapshot items use the nested shape ({"data": {"content": [...]}}),
    # unlike the flat to_api_dict shape of GET /sessions/{id}/items.
    item_texts = [
        block.get("text")
        for item in full_body["items"]
        if item.get("type") == "message"
        for block in item.get("data", {}).get("content", [])
    ]
    assert "hello" in item_texts, (
        f"seeded message missing from full snapshot items: {full_body['items']}"
    )
    assert full_body["runner_online"] is True


async def test_get_session_replays_pending_elicitations(
    client: httpx.AsyncClient,
) -> None:
    """
    ``GET /v1/sessions/{id}`` includes outstanding elicitation event
    payloads in ``pending_elicitations`` so the UI can render the
    ApprovalCard on cold load.

    Without snapshot replay, the live SSE stream's no-buffer design
    means a prompt emitted before the user opened the chat would
    never render — clicking the sidebar-badged row would land on a
    chat with no card and no way to approve. The fix funnels the
    same event payload through the same parser the live stream uses,
    so the rendered card is byte-identical to what the live path
    would have produced.
    """
    from omnigent.runtime import pending_elicitations

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    session_id = session["id"]
    pending_elicitations.reset_for_tests()

    # Register an outstanding prompt with a real params block so the
    # snapshot returns a payload the UI's parser can consume end-to-end.
    pending_elicitations.record_publish(
        session_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_snapshot_test",
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": "Allow tool **Bash**?",
                "requestedSchema": None,
                "phase": "tool_call",
                "policy_name": "guardrails_default",
                "content_preview": "Bash(ls -la)",
            },
        },
    )

    resp = await client.get(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    # Field present even when empty — keeps the wire shape stable.
    # The list serializer uses model_dump (not exclude_none); an
    # empty list must still round-trip.
    assert "pending_elicitations" in body, (
        f"snapshot must include pending_elicitations field; got keys {sorted(body.keys())}. "
        f"If absent, SessionResponse dropped the field or _build_session_response "
        f"isn't reading the index."
    )
    pending_payloads = body["pending_elicitations"]
    # 1 = the outstanding event was carried into the snapshot. If
    # 0, _build_session_response isn't calling snapshot_for; if
    # > 1, the snapshot is bleeding across sessions.
    assert len(pending_payloads) == 1
    payload = pending_payloads[0]
    # Full event shape survives — UI re-parses through the same SSE
    # parser, which reads top-level fields and params.* literals.
    # If `params` is missing/empty, the card renders with no prompt
    # text; if `elicitation_id` is missing, the user couldn't
    # submit a verdict back even if they tried.
    assert payload["elicitation_id"] == "elicit_snapshot_test"
    assert payload["type"] == "response.elicitation_request"
    assert payload["params"]["message"] == "Allow tool **Bash**?"
    assert payload["params"]["content_preview"] == "Bash(ls -la)"

    pending_elicitations.reset_for_tests()


# ── GET /v1/sessions/{id}/items ──────────────────────────


async def test_list_session_items_returns_items(
    client: httpx.AsyncClient,
) -> None:
    """Items endpoint returns the user message from session creation."""
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        initial_message="hello items",
    )
    await _wait_for_idle(client, session["id"])

    resp = await client.get(f"/v1/sessions/{session['id']}/items")
    assert resp.status_code == 200
    items = resp.json()["data"]
    assert len(items) >= 1
    user_msgs = [i for i in items if i.get("type") == "message" and i.get("role") == "user"]
    assert len(user_msgs) >= 1


async def test_list_session_items_pagination(
    client: httpx.AsyncClient,
) -> None:
    """Items endpoint supports limit and after cursor."""
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        initial_message="page test",
    )
    await _wait_for_idle(client, session["id"])

    resp = await client.get(
        f"/v1/sessions/{session['id']}/items",
        params={"limit": 1},
    )
    assert resp.status_code == 200
    page = resp.json()
    assert len(page["data"]) <= 1


async def test_list_session_items_404_for_nonexistent(
    client: httpx.AsyncClient,
) -> None:
    """Items endpoint returns 404 for a session that doesn't exist."""
    resp = await client.get("/v1/sessions/conv_nonexistent/items")
    assert resp.status_code == 404


# ── GET /v1/sessions/{id} snapshot fields ────────────────


async def test_get_session_includes_title_labels_effort(
    client: httpx.AsyncClient,
) -> None:
    """GET snapshot returns title, labels, reasoning_effort, instructions."""
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        title="snap-test",
        labels={"mode": "debug"},
    )
    await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"reasoning_effort": "medium"},
    )

    resp = await client.get(f"/v1/sessions/{session['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "snap-test"
    assert body["labels"]["mode"] == "debug"
    assert body["reasoning_effort"] == "medium"


async def test_get_session_labels_returns_labels_only(
    client: httpx.AsyncClient,
) -> None:
    """
    GET labels endpoint returns the session id and labels only.

    :param client: Test HTTP client backed by the real server routes.
    :returns: None.
    """
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        labels={"mode": "debug", "team": "infra"},
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/labels")

    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json() == {
        "id": session["id"],
        "labels": {"mode": "debug", "team": "infra"},
    }


async def test_post_external_assistant_message_persists_and_streams(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    External assistant output appends history without starting a task.

    This is the path used by ``omnigent claude`` to mirror real
    Claude terminal transcript text into the web UI. It must publish
    a completed output item so connected clients render the text
    immediately without a duplicate synthetic text delta, while
    leaving the session idle.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_assistant_message",
            "data": {
                "agent": "claude-native-ui",
                "text": "hello from real claude",
                "response_id": "resp_claude_external_test",
            },
        },
    )

    assert resp.status_code == 202, resp.text
    ack = resp.json()
    assert ack["queued"] is False
    assert isinstance(ack["item_id"], str)

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    body = snap.json()
    assert body["status"] == "idle"
    assistant_items = [
        item
        for item in body["items"]
        if item["type"] == "message" and item["data"]["role"] == "assistant"
    ]
    assert len(assistant_items) == 1
    assert assistant_items[0]["data"]["model"] == "claude-native-ui"
    assert assistant_items[0]["data"]["content"] == [
        {"type": "output_text", "text": "hello from real claude"}
    ]

    assert [event["type"] for _, event in published] == ["response.output_item.done"]
    assert all(session_id == session["id"] for session_id, _ in published)
    assert published[0][1]["item"]["id"] == ack["item_id"]
    assert published[0][1]["item"]["model"] == "claude-native-ui"


async def test_post_external_conversation_item_persists_and_streams_visible_items(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    External transcript items mirror terminal Claude into the session.

    The native Claude forwarder posts user messages, Claude tool calls,
    tool results, and assistant messages through this route. None of
    those posts should start the placeholder session agent.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    events = [
        {
            "item_type": "message",
            "response_id": "resp_terminal_user",
            "source_id": "src_terminal_user_0",
            "item_data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "read TODO"}],
            },
        },
        {
            "item_type": "function_call",
            "response_id": "resp_terminal_assistant",
            "source_id": "src_terminal_assistant_0",
            "item_data": {
                "agent": "claude-native-ui",
                "name": "Read",
                "arguments": '{"file_path":"TODO.md"}',
                "call_id": "toolu_read_1",
            },
        },
        {
            "item_type": "function_call_output",
            "response_id": "resp_terminal_assistant",
            "source_id": "src_terminal_assistant_1",
            "item_data": {"call_id": "toolu_read_1", "output": "todo contents"},
        },
        {
            "item_type": "message",
            "response_id": "resp_terminal_assistant",
            "source_id": "src_terminal_assistant_2",
            "item_data": {
                "role": "assistant",
                "agent": "claude-native-ui",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
        {
            "item_type": "terminal_command",
            "response_id": "resp_terminal_shell",
            "source_id": "src_terminal_shell_0",
            "item_data": {"kind": "input", "input": "pwd"},
        },
        {
            "item_type": "terminal_command",
            "response_id": "resp_terminal_shell",
            "source_id": "src_terminal_shell_1",
            "item_data": {"kind": "output", "stdout": "/tmp/project", "stderr": ""},
        },
    ]

    for event_data in events:
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={"type": "external_conversation_item", "data": event_data},
        )
        assert resp.status_code == 202, resp.text
        assert resp.json()["queued"] is False

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    body = snap.json()
    assert body["status"] == "idle"
    assert [item["type"] for item in body["items"]] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
        "terminal_command",
        "terminal_command",
    ]
    assert body["items"][0]["data"]["role"] == "user"
    assert body["items"][1]["data"]["name"] == "Read"
    assert body["items"][2]["data"] == {"call_id": "toolu_read_1", "output": "todo contents"}
    assert body["items"][3]["data"]["role"] == "assistant"
    assert body["items"][4]["data"] == {
        "kind": "input",
        "input": "pwd",
        "stdout": None,
        "stderr": None,
    }
    assert body["items"][5]["data"] == {
        "kind": "output",
        "input": None,
        "stdout": "/tmp/project",
        "stderr": "",
    }

    assert [event["type"] for _, event in published] == [
        "session.input.consumed",
        "response.output_item.done",
        "response.output_item.done",
        "response.output_item.done",
        "response.output_item.done",
        "response.output_item.done",
    ]
    assert published[0][1]["data"]["data"]["content"][0]["text"] == "read TODO"
    assert published[1][1]["item"]["type"] == "function_call"
    assert published[2][1]["item"]["type"] == "function_call_output"
    assert published[3][1]["item"]["type"] == "message"
    assert published[4][1]["item"]["type"] == "terminal_command"
    assert published[5][1]["item"]["type"] == "terminal_command"


async def test_post_external_function_call_output_caps_oversized_output(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-MB native tool result is capped before persist + broadcast.

    The native (tmux-driven) forwarders read a tool result straight from the
    agent's transcript and POST it as an external function_call_output. Without
    a cap, a multi-MB result would be persisted and broadcast to the web UI as
    one giant SSE frame. This route must bound it — the native analog of the
    harness scaffold's source-side cap.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    # 2 MiB — comfortably over the 1 MiB cap so truncation is unambiguous.
    big_output = "x" * (2 * 1024 * 1024)
    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "response_id": "resp_native_tool",
                "item_data": {"call_id": "toolu_big_1", "output": big_output},
            },
        },
    )
    assert resp.status_code == 202, resp.text

    # Persisted copy is capped (carries the notice, far smaller than 2 MiB). A
    # failure here means the native ingest path bypassed the cap.
    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    persisted = snap.json()["items"][0]
    assert persisted["type"] == "function_call_output"
    assert "[output truncated by omnigent:" in persisted["data"]["output"]
    assert len(persisted["data"]["output"].encode("utf-8")) <= MAX_TOOL_OUTPUT_BYTES + 200
    assert len(persisted["data"]["output"].encode("utf-8")) < len(big_output)

    # The broadcast SSE frame is capped too (same item object) — this is the
    # giant-frame-to-the-browser case the cap exists to prevent.
    fco_events = [
        event["item"]["output"]
        for _, event in published
        if event.get("type") == "response.output_item.done"
        and event.get("item", {}).get("type") == "function_call_output"
    ]
    assert len(fco_events) == 1, (
        f"expected one broadcast function_call_output; got {len(fco_events)}"
    )
    assert "[output truncated by omnigent:" in fco_events[0]
    assert len(fco_events[0].encode("utf-8")) < len(big_output)


async def test_external_transcript_items_recoverable_via_snapshot_by_item_id(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconnect dedupe contract: the item ids the live stream emits
    equal the item ids the snapshot persists, so a client reconciling
    snapshot + live stream by item id sees each item exactly once."""
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    agent = await create_test_agent(client, name="claude-native-ui")
    session = await _create_session(client, agent["id"])
    session_id = session["id"]

    # A representative forwarder turn: user prompt, tool call + output,
    # assistant reply.
    transcript = [
        {
            "item_type": "message",
            "response_id": "resp_user",
            "source_id": "src_user_0",
            "item_data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "read TODO"}],
            },
        },
        {
            "item_type": "function_call",
            "response_id": "resp_asst",
            "source_id": "src_asst_0",
            "item_data": {
                "agent": "claude-native-ui",
                "name": "Read",
                "arguments": '{"file_path":"TODO.md"}',
                "call_id": "toolu_read_1",
            },
        },
        {
            "item_type": "function_call_output",
            "response_id": "resp_asst",
            "source_id": "src_asst_1",
            "item_data": {"call_id": "toolu_read_1", "output": "todo contents"},
        },
        {
            "item_type": "message",
            "response_id": "resp_asst",
            "source_id": "src_asst_2",
            "item_data": {
                "role": "assistant",
                "agent": "claude-native-ui",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
    ]
    for event_data in transcript:
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "external_conversation_item", "data": event_data},
        )
        assert resp.status_code == 202, resp.text

    # Item ids the live stream carried: session.input.consumed
    # (data.item_id) for inputs, response.output_item.done (item.id)
    # for outputs.
    streamed_ids: set[str] = set()
    for sid, ev in published:
        assert sid == session_id
        if ev["type"] == "session.input.consumed":
            streamed_ids.add(ev["data"]["item_id"])
        elif ev["type"] == "response.output_item.done":
            streamed_ids.add(ev["item"]["id"])
    assert len(streamed_ids) == 4, f"expected 4 streamed ids, got {streamed_ids!r}"

    snap = (await client.get(f"/v1/sessions/{session_id}")).json()
    snapshot_ids = [item["id"] for item in snap["items"]]
    assert len(snapshot_ids) == len(set(snapshot_ids)), (
        f"snapshot has duplicate item ids: {snapshot_ids!r}"
    )
    # Equal sets ⇒ deduping snapshot ∪ live-stream by id loses nothing
    # and duplicates nothing.
    assert streamed_ids == set(snapshot_ids), (
        f"streamed vs snapshot ids diverge — "
        f"streamed-only={streamed_ids - set(snapshot_ids)!r} "
        f"snapshot-only={set(snapshot_ids) - streamed_ids!r}"
    )


async def test_post_external_session_status_publishes_session_status(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_session_status`` posts a typed SessionStatusEvent.

    The native Claude forwarder posts this when Claude Code's Stop /
    StopFailure hooks fire so the web UI's idle/running indicator
    updates without going through the Omnigent task lifecycle.
    A regression here would break the idle indicator for
    ``omnigent claude`` sessions: Omnigent would never learn Claude
    finished and the UI would stay stuck on whatever transient
    state it last saw.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_status", "data": {"status": "idle"}},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}

    assert [event["type"] for _, event in published] == ["session.status"]
    assert published[0][0] == session["id"]
    assert published[0][1]["status"] == "idle"
    assert published[0][1]["conversation_id"] == session["id"]
    assert "response_id" not in published[0][1]


async def test_post_external_session_status_carries_response_id(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_session_status`` can bind a status edge to a response.

    Codex-native terminal turns do not emit ``response.created`` /
    ``response.completed`` through AP. The optional ``response_id`` lets
    web attach running / interrupted / idle lifecycle to the assistant
    bubble that will later be persisted with the same response id.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "running", "response_id": "codex_turn_123"},
        },
    )
    assert resp.status_code == 202, resp.text

    assert len(published) == 1
    published_session_id, event = published[0]
    assert published_session_id == session["id"]
    assert event["type"] == "session.status"
    assert event["conversation_id"] == session["id"]
    assert event["status"] == "running"
    assert event["response_id"] == "codex_turn_123"


async def test_publish_status_keeps_failed_sticky_against_trailing_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``failed`` session status is not downgraded by a trailing ``idle``.

    A claude-native turn error arrives via the ``StopFailure`` hook
    (→ ``failed``), but the now-quiet pane then makes the runner's
    PTY-activity watcher emit a trailing ``idle``. That ``idle`` must not
    erase the error state before the user can see it; only the next
    ``running`` edge (new activity) clears ``failed``. This pins the
    sticky-``failed`` guard in ``_publish_status`` — without it, the red
    error badge would flash for ~1s then revert to idle on every failed
    claude-native turn.
    """
    from omnigent.server.routes import sessions as sessions_module

    published: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda _session_id, event: published.append(event),
    )
    sid = "conv_sticky_failed"
    sessions_module._session_status_cache.pop(sid, None)
    try:
        sessions_module._publish_status(sid, "failed")
        # Trailing PTY ``idle`` must be suppressed while ``failed`` stands.
        sessions_module._publish_status(sid, "idle")
        # New activity clears ``failed``...
        sessions_module._publish_status(sid, "running")
        # ...after which ``idle`` publishes normally again.
        sessions_module._publish_status(sid, "idle")
        cache_after = sessions_module._session_status_cache.get(sid)
    finally:
        sessions_module._session_status_cache.pop(sid, None)

    # The idle posted right after ``failed`` is absent — the error
    # survived until ``running`` cleared it. A published sequence
    # containing ``[failed, idle, ...]`` would mean the sticky guard
    # regressed and the trailing PTY idle clobbered the error.
    assert [event["status"] for event in published] == ["failed", "running", "idle"]
    # The final idle (after running) is honored, so the cache is current.
    assert cache_after == "idle"


async def test_patch_runner_rebind_clears_stale_failed_status(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    CLI resume rebind clears a stale failed status after runner init.

    ``omnigent codex --resume`` binds a newly launched runner through
    ``PATCH /v1/sessions/{id}``. That path has its own runner-init POST,
    separate from ``_resume_session_on_host``. If it succeeds but does not
    publish the recovery transition, a stale ``failed`` cache entry keeps
    the session snapshot stuck on an old native-terminal startup error.
    """
    from omnigent.server.routes import sessions as sessions_module

    class _RecoveringRunnerClient:
        """Runner client that records PATCH-path session init."""

        def __init__(self) -> None:
            """:returns: None."""
            self.posts: list[dict[str, Any]] = []

        async def post(
            self,
            url: str,
            *,
            json: dict[str, Any],
            timeout: float,
        ) -> httpx.Response:
            """
            Record the runner init POST.

            :param url: Runner URL path, e.g. ``"/v1/sessions"``.
            :param json: Request body sent to the runner.
            :param timeout: HTTP timeout in seconds, e.g. ``10.0``.
            :returns: HTTP 200 response.
            """
            self.posts.append({"url": url, "json": json, "timeout": timeout})
            return httpx.Response(200, request=httpx.Request("POST", url))

    def _registered_runner_id(
        _runner_router: Any,
        raw_runner_id: str,
        *,
        user_id: str | None = None,
    ) -> str:
        """
        Accept the test runner id without a real tunnel registry.

        :param _runner_router: Ignored runner router placeholder.
        :param raw_runner_id: Requested runner id, e.g. ``"runner_recovered"``.
        :param user_id: Optional authenticated user id, e.g. ``"alice@example.com"``.
        :returns: The trimmed runner id.
        """
        return raw_runner_id.strip()

    async def _get_runner_client(
        _session_id: str,
        _runner_router: Any,
    ) -> _RecoveringRunnerClient:
        """
        Return the recovering runner client for the patched session.

        :param _session_id: Session id being rebound, e.g. ``"conv_abc123"``.
        :param _runner_router: Ignored runner router placeholder.
        :returns: Runner client stub.
        """
        return runner_client

    async def _ensure_runner_relay_ready(
        _session_id: str,
        _runner_id: str,
        _runner_client: _RecoveringRunnerClient,
        _conversation_store: Any,
    ) -> None:
        """
        Skip relay startup; this test targets the PATCH init branch.

        :param _session_id: Session id, e.g. ``"conv_abc123"``.
        :param _runner_id: Runner id, e.g. ``"runner_recovered"``.
        :param _runner_client: Runner client stub.
        :param _conversation_store: Conversation store from the route.
        :returns: None.
        """

    published: list[dict[str, Any]] = []
    runner_client = _RecoveringRunnerClient()
    monkeypatch.setattr(sessions_module, "_registered_runner_id", _registered_runner_id)
    monkeypatch.setattr(sessions_module, "_get_runner_client", _get_runner_client)
    monkeypatch.setattr(sessions_module, "_ensure_runner_relay_ready", _ensure_runner_relay_ready)
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda _session_id, event: published.append(event),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    # The monkeypatched runner client can see setup work during session
    # creation. This test targets the later PATCH rebind path only.
    runner_client.posts.clear()
    sessions_module._session_status_cache.pop(sid, None)
    try:
        sessions_module._publish_status(
            sid,
            "failed",
            sessions_module.ErrorDetail(
                code="native_terminal_start_failed",
                message="old startup failure",
            ),
        )

        resp = await client.patch(
            f"/v1/sessions/{sid}",
            json={"runner_id": "runner_recovered"},
        )
        cache_after = sessions_module._session_status_cache.get(sid)
    finally:
        sessions_module._session_status_cache.pop(sid, None)

    assert resp.status_code == 200, resp.text
    assert runner_client.posts == [
        {
            "url": "/v1/sessions",
            "json": {
                "session_id": sid,
                "agent_id": agent["id"],
                "sub_agent_name": None,
            },
            "timeout": 10.0,
        }
    ]
    assert [event["status"] for event in published] == ["failed", "idle"]
    assert cache_after == "idle"


async def test_post_external_session_status_idle_forwards_persisted_assistant_output(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Native idle status forwarding includes AP-persisted assistant text.

    The native forwarder posts transcript items to Omnigent server, then posts
    ``external_session_status: idle``. The runner's sub-agent registry
    needs the durable assistant text in that status forward because it
    may not have the Omnigent transcript in local memory.
    """
    from omnigent.server.routes import sessions as sessions_module

    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Capture forwarded runner events.

        :param request: Request sent to the fake runner.
        :returns: Accepted response.
        """
        forwarded.append(
            {
                "path": request.url.path,
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> httpx.AsyncClient:
        """
        Resolve the session to the fake runner client.

        :param session_id: Session id being routed.
        :param runner_router: Real app runner router, unused here.
        :returns: The fake runner client.
        """
        del session_id, runner_router
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    try:
        agent = await create_test_agent(client)
        parent = await _create_session(client, agent["id"])
        child_resp = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "parent_session_id": parent["id"],
                "sub_agent_name": "worker",
                "title": "worker:native",
            },
        )
        assert child_resp.status_code == 201, child_resp.text
        child = child_resp.json()

        item_resp = await client.post(
            f"/v1/sessions/{child['id']}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "message",
                    "response_id": "resp_native_done",
                    "source_id": "src_native_done",
                    "item_data": {
                        "role": "assistant",
                        "agent": "claude-native-ui",
                        "content": [
                            {"type": "output_text", "text": "AP_NATIVE_DONE"},
                        ],
                    },
                },
            },
        )
        assert item_resp.status_code == 202, item_resp.text

        forwarded.clear()
        status_resp = await client.post(
            f"/v1/sessions/{child['id']}/events",
            json={"type": "external_session_status", "data": {"status": "idle"}},
        )
    finally:
        await fake_runner.aclose()

    assert status_resp.status_code == 202, status_resp.text
    assert forwarded == [
        {
            "path": f"/v1/sessions/{child['id']}/events",
            "body": {
                "type": "external_session_status",
                "data": {"status": "idle", "output": "AP_NATIVE_DONE"},
                "model_override": None,
                "tools": None,
            },
        }
    ]


async def test_post_external_session_status_propagates_runner_delivery_failure(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Runner delivery failure for a non-Codex sub-agent is preserved by AP.

    Native child terminal status is only successful when the runner confirms
    delivery to the parent's inbox. If Omnigent returned ``{"queued": false}`` after
    a runner 503, the child forwarder would believe the result was ACKed while
    the parent never receives it.
    """
    from omnigent.server.routes import sessions as sessions_module

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Reject the forwarded terminal status like a runner missing parent state.

        :param request: Request sent to the fake runner.
        :returns: Session-create acceptance or 503 delivery-not-confirmed
            response.
        """
        if request.method == "POST" and request.url.path == "/v1/sessions":
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": body["session_id"]})
        assert request.url.path.endswith("/events")
        return httpx.Response(
            503,
            json={
                "error": "subagent_delivery_not_confirmed",
                "reason": "missing_parent_inbox",
            },
        )

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> httpx.AsyncClient:
        """
        Resolve every session to the fake runner client.

        :param session_id: Session id being routed.
        :param runner_router: Real app runner router, unused here.
        :returns: The fake runner client.
        """
        del session_id, runner_router
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    try:
        agent = await create_test_agent(client)
        parent = await _create_session(client, agent["id"])
        child_resp = await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "parent_session_id": parent["id"],
                "sub_agent_name": "worker",
                "title": "worker:native",
            },
        )
        assert child_resp.status_code == 201, child_resp.text
        child = child_resp.json()

        status_resp = await client.post(
            f"/v1/sessions/{child['id']}/events",
            json={"type": "external_session_status", "data": {"status": "idle"}},
        )
    finally:
        await fake_runner.aclose()

    assert status_resp.status_code == 503, status_resp.text
    error = status_resp.json()["error"]
    assert error["code"] == "runner_unavailable"
    # The runner's delivery-not-confirmed reason is retained in AP's error
    # message. If absent, Omnigent flattened the failure and made diagnosis harder.
    assert "missing_parent_inbox" in error["message"]


async def test_post_external_output_text_delta_publishes_transient_delta(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_output_text_delta`` emits a live text delta only.

    Codex-native uses this for app-server ``item/agentMessage/delta``
    notifications. The event must be visible on the session SSE stream
    but absent from persisted history; the completed assistant item is
    mirrored separately by ``external_conversation_item``.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_output_text_delta", "data": {"delta": "hel"}},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}

    assert published == [
        (
            session["id"],
            {"type": "response.output_text.delta", "delta": "hel"},
        )
    ]

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200, snap.text
    assert snap.json()["items"] == []


async def test_post_external_output_text_delta_rejects_malformed_delta(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_output_text_delta`` fails loud on non-string deltas.

    Without this validation, a malformed terminal-forwarder payload
    would publish a non-conforming ``response.output_text.delta`` event
    and strict SDK clients would drop it downstream.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture any accidental stream publish before validation fails.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_output_text_delta", "data": {"delta": {"text": "bad"}}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_output_text_delta requires string data.delta" in resp.text
    assert published == []


async def test_post_external_output_reasoning_delta_started_publishes_started_then_delta(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_output_reasoning_delta`` with ``started`` emits started + delta.

    The antigravity-native reader uses this for a Gemini Thinking-model
    ``plannerResponse.thinking`` stream. The first delta of a block sets
    ``started`` so the route precedes the ``response.reasoning_text.delta`` with
    one ``response.reasoning.started`` (the SPA new-block marker). Both events
    must be visible on the SSE stream and nothing persisted to history (reasoning
    has no completed item).
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_output_reasoning_delta",
            "data": {"delta": "Let me think", "started": True},
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}

    assert published == [
        (session["id"], {"type": "response.reasoning.started"}),
        (
            session["id"],
            {"type": "response.reasoning_text.delta", "delta": "Let me think"},
        ),
    ]

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200, snap.text
    assert snap.json()["items"] == []


async def test_post_external_output_reasoning_delta_continuation_publishes_delta_only(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A continuation reasoning delta (``started`` false/omitted) emits delta only.

    Only the first delta of a reasoning block opens it with
    ``response.reasoning.started``; later deltas publish a bare
    ``response.reasoning_text.delta``.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_output_reasoning_delta",
            "data": {"delta": " more thought", "started": False},
        },
    )
    assert resp.status_code == 202, resp.text

    assert published == [
        (
            session["id"],
            {"type": "response.reasoning_text.delta", "delta": " more thought"},
        )
    ]


async def test_post_external_output_reasoning_delta_rejects_malformed_delta(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_output_reasoning_delta`` fails loud on a non-string delta.

    Mirrors the text-delta validation: a malformed payload must not publish a
    non-conforming ``response.reasoning_text.delta`` that strict SDK clients drop.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture any accidental stream publish before validation fails.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_output_reasoning_delta", "data": {"delta": 123}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_output_reasoning_delta requires string data.delta" in resp.text
    assert published == []


async def test_post_external_session_interrupted_publishes_session_interrupted(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_session_interrupted`` emits a live interruption signal only.

    Codex-native uses this when app-server reports a terminal
    ``turn/completed`` status of ``interrupted``. The event must decorate
    the active web turn as cancelled, but must not persist a transcript item
    or start / steer an Omnigent task.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_interrupted",
            "data": {"response_id": "codex_turn_123"},
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}

    assert len(published) == 1
    published_session_id, event = published[0]
    assert published_session_id == session["id"]
    assert event["type"] == "session.interrupted"
    assert isinstance(event["data"]["requested_at"], int)
    assert event["data"]["response_id"] == "codex_turn_123"

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200, snap.text
    assert snap.json()["items"] == []


async def test_post_interrupt_without_data_field_is_accepted(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A bare ``{"type": "interrupt"}`` (no ``data`` key) is valid input.

    Control events carry no payload, and deployed runner-side clients
    (``sys_cancel_task``'s stop/interrupt forward) post them without
    ``data``. When ``SessionEventInput.data`` was required, those
    clients got ``422 missing body.data`` and sub-agent cancellation
    was broken in production — this pins the default.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "interrupt"},
    )
    # 202 (not 422) proves ``data`` defaults to ``{}`` at the schema
    # layer; a 422 here means the pydantic default regressed and every
    # deployed bare-control client breaks again.
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}
    # The interrupt actually took effect (not just validated): the
    # route published the cancellation signal for the web turn.
    interrupted = [e for _sid, e in published if e.get("type") == "session.interrupted"]
    assert len(interrupted) == 1, (
        f"expected one session.interrupted publish for a bare interrupt "
        f"event, got types {[e.get('type') for _sid, e in published]!r}"
    )


async def test_post_external_output_text_delta_carries_streaming_identifiers(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``message_id`` / ``index`` / ``final`` pass through to the SSE event.

    claude-native live streaming needs these so the web UI can scope an
    in-flight buffer to one assistant message, order its chunks, and know
    when the stream ends. Fails if any is dropped (UI can't reconcile the
    preview with the final item) — including the falsy ``index: 0`` /
    ``final: False`` values, which ``exclude_none`` must keep.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_output_text_delta",
            "data": {"delta": "Hel", "message_id": "msg-uuid", "index": 0, "final": False},
        },
    )
    assert resp.status_code == 202, resp.text

    # All four fields survive — including index 0 and final False, which a
    # naive `if value:` filter would have dropped.
    assert published == [
        (
            session["id"],
            {
                "type": "response.output_text.delta",
                "delta": "Hel",
                "message_id": "msg-uuid",
                "index": 0,
                "final": False,
            },
        )
    ]


@pytest.mark.parametrize(
    "bad_data,expected_msg",
    [
        (
            {"delta": "x", "message_id": 7},
            "external_output_text_delta data.message_id must be a string",
        ),
        (
            {"delta": "x", "index": "0"},
            "external_output_text_delta data.index must be an integer",
        ),
        (
            {"delta": "x", "index": True},
            "external_output_text_delta data.index must be an integer",
        ),
        (
            {"delta": "x", "final": "yes"},
            "external_output_text_delta data.final must be a boolean",
        ),
    ],
    ids=["non-string-message_id", "string-index", "bool-index", "non-bool-final"],
)
async def test_post_external_output_text_delta_rejects_malformed_identifiers(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    bad_data: dict[str, Any],
    expected_msg: str,
) -> None:
    """
    Wrong-typed streaming identifiers fail loud and publish nothing.

    A malformed terminal-forwarder payload must not produce a
    non-conforming ``response.output_text.delta`` (which strict SDK
    clients would drop). ``index: True`` is called out because ``bool``
    is an ``int`` subclass and would otherwise slip through.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture any accidental stream publish before validation fails.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_output_text_delta", "data": bad_data},
    )
    assert resp.status_code == 400, resp.text
    assert expected_msg in resp.text
    assert published == []


async def test_post_external_session_status_rejects_unknown_status(
    client: httpx.AsyncClient,
) -> None:
    """
    Unknown status values are rejected with a 400.

    Without this guard a typo in the forwarder (``"Idle"`` vs
    ``"idle"``) would propagate to the wire as a non-conforming
    ``session.status`` payload and the SDK's strict event adapter
    would silently drop the event downstream — exactly the kind of
    invisible failure rule 15 (fail loud) exists to prevent.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_status", "data": {"status": "Idle"}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_session_status" in resp.text


async def test_post_external_session_usage_publishes_session_usage(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_session_usage`` posts a typed SessionUsageEvent and
    persists the value on the conversation labels.

    The claude-native forwarder posts this whenever Claude's transcript
    grows a fresh ``message.usage`` block so the ap-web context ring
    updates without waiting for a ``response.completed`` event (Claude
    Code runs in a separate process and never produces one). Both the
    live SSE path and the snapshot-restore path read from this event:
    live via the broadcast, restore via the conversation label.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"context_tokens": 44568, "input_tokens": 6, "output_tokens": 554},
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}

    assert [event["type"] for _, event in published] == ["session.usage"]
    assert published[0][1]["conversation_id"] == session["id"]
    assert published[0][1]["context_tokens"] == 44568

    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["last_total_tokens"] == 44568


async def test_external_session_usage_broadcasts_parent_subtree_cost_not_own(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A parent's ``session.usage`` broadcast carries its SUBTREE cost, not own.

    A sub-agent persists its spend on its own child conversation. If the
    parent's own-cost flush broadcast only the parent's own cost, the live
    badge would drop back to own-cost on every parent flush and hide in-flight
    sub-agent spend until the next child flush (the badge would oscillate
    own ⇄ subtree). The broadcast must match the GET snapshot's subtree total.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    parent = await _create_session(client, agent["id"], title="parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="worker",
        parent_conversation_id=parent["id"],
        agent_id=agent["id"],
    )
    # Sub-agent has $2.50 of priced spend, persisted on the CHILD conversation.
    conv_store.set_session_usage(child.id, {"total_cost_usd": 2.5})

    # The parent flushes its OWN $1.00 cumulative cost.
    resp = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={"type": "external_session_usage", "data": {"cumulative_cost_usd": 1.0}},
    )
    assert resp.status_code == 202, resp.text

    parent_usage = [ev for sid, ev in published if ev.get("conversation_id") == parent["id"]]
    assert parent_usage, "expected a session.usage broadcast for the parent"
    # 3.5 = parent own $1.00 + sub-agent $2.50. A value of 1.0 means the parent's
    # live badge regressed to own-cost (the bug) — it would show $1.00 right
    # after the parent flushes and only jump back to $3.50 on the next child
    # flush, instead of staying at the subtree total like the snapshot.
    assert parent_usage[-1]["total_cost_usd"] == 3.5


async def test_post_external_session_usage_dynamic_context_window_overrides_snapshot(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A posted ``context_window`` overrides the spec's static value on snapshot.

    The claude-native forwarder resolves the *actual* context window
    from the user's selected Claude model + ``[1m]`` alias and pushes
    it through this event. The session snapshot must prefer that
    observed value over the spec's conservative static default so the
    ring reflects the user's real tier (e.g. 1M for opus[1m] users).
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"context_tokens": 250_000, "context_window": 1_000_000},
        },
    )
    assert resp.status_code == 202, resp.text

    assert [event["type"] for _, event in published] == ["session.usage"]
    assert published[0][1]["context_tokens"] == 250_000
    assert published[0][1]["context_window"] == 1_000_000

    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["last_total_tokens"] == 250_000
    assert snapshot["context_window"] == 1_000_000


async def test_post_external_session_usage_window_only_payload_persists_window(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A window-only post updates the window without zeroing tokens.

    The forwarder may post just ``context_window`` when the user
    switches models without producing a fresh ``message.usage`` block
    (e.g. via Claude Code's ``/model`` slash command between turns).
    The server must not require both fields.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_usage", "data": {"context_tokens": 100}},
    )
    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_usage", "data": {"context_window": 1_000_000}},
    )
    assert resp.status_code == 202, resp.text

    last = published[-1][1]
    assert last["context_window"] == 1_000_000
    assert "context_tokens" not in last

    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["last_total_tokens"] == 100
    assert snapshot["context_window"] == 1_000_000


async def test_post_external_session_usage_rejects_empty_payload(
    client: httpx.AsyncClient,
) -> None:
    """
    A payload missing both context_tokens and context_window 400s.

    Defends against a forwarder logic bug or partial deploy that would
    otherwise round-trip a no-op event to Omnigent every poll.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_usage", "data": {}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_session_usage" in resp.text


def _read_session_usage(db_uri: str, session_id: str) -> dict[str, Any]:
    """
    Read a conversation's persisted ``session_usage`` directly from the DB.

    The session snapshot API does not expose cost, so cumulative-usage tests
    read the ``session_usage`` column via a reader store on the same DB file
    the app writes to.

    :param db_uri: The per-test SQLite URI (same one the ``client`` app uses).
    :param session_id: Conversation id to read.
    :returns: The parsed ``session_usage`` dict (empty when unset).
    """
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    return dict(conv.session_usage) if conv and conv.session_usage else {}


async def test_external_session_usage_persists_cumulative_cost(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A claude-native ``cumulative_cost_usd`` is persisted to ``session_usage``.

    claude-native can't produce a ``response.completed`` (Claude Code is a
    separate process), so the Omnigent relay's ``_accumulate_session_usage`` never
    runs for it. Instead the forwarder sends Claude Code's own cumulative
    ``cost.total_cost_usd`` on this event; the server must persist it so a
    Cost-Ask policy reading ``event.context.usage.total_cost_usd`` sees a real
    value. A failure here means native cost stays 0 and the policy never fires.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"context_tokens": 44568, "cumulative_cost_usd": 0.42},
        },
    )
    assert resp.status_code == 202, resp.text
    usage = _read_session_usage(db_uri, session["id"])
    # Claude Code's own cumulative cost is stored verbatim.
    assert usage.get("total_cost_usd") == 0.42


async def test_external_session_usage_persists_display_and_policy_costs(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    claude-native's display (S) and policy (max(S,C)) costs persist separately.

    The forwarder posts ``cumulative_cost_usd`` = the statusLine total S
    (display, matches /cost) and ``policy_cost_usd`` = the real-time gate
    figure. They must land in distinct ``session_usage`` keys: storing the
    enforcement value in ``total_cost_usd`` would re-inflate the badge and
    the daily rollup, which is exactly what this split avoids.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"context_tokens": 100, "cumulative_cost_usd": 0.10, "policy_cost_usd": 0.30},
        },
    )
    assert resp.status_code == 202, resp.text
    usage = _read_session_usage(db_uri, session["id"])
    # Display cost = S verbatim (drives badge + daily rollup).
    assert usage.get("total_cost_usd") == 0.10
    # Enforcement cost stored separately (seeds the cost-budget gate).
    assert usage.get("policy_cost_usd") == 0.30


async def test_external_session_usage_policy_cost_only_post_accepted(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A post carrying only ``policy_cost_usd`` is accepted; display S unchanged.

    Mid-sub-agent-run the displayed statusLine total (``cumulative_cost_usd``)
    is frozen, so the forwarder posts only the advancing enforcement cost. The
    route must accept that payload (``policy_cost_usd`` counts as a cumulative
    usage field) and update ``policy_cost_usd`` while leaving ``total_cost_usd``
    at the last S — otherwise the gate would stop seeing in-flight spend, or
    the badge would jump.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    # Seed both at the turn start.
    seed = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"cumulative_cost_usd": 0.10, "policy_cost_usd": 0.10},
        },
    )
    assert seed.status_code == 202, seed.text

    # Mid-turn: only the enforcement cost advances (no cumulative_cost_usd).
    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_usage", "data": {"policy_cost_usd": 0.50}},
    )
    assert resp.status_code == 202, resp.text
    usage = _read_session_usage(db_uri, session["id"])
    # Display stays at the last S (frozen mid-turn).
    assert usage.get("total_cost_usd") == 0.10
    # Enforcement advanced so the gate sees in-flight sub-agent spend.
    assert usage.get("policy_cost_usd") == 0.50


async def test_external_session_usage_cumulative_cost_is_set_not_added(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Successive cumulative-cost posts SET (not accumulate) — native reports
    running totals, so two posts of 0.42 then 0.90 must leave 0.90, not 1.32.

    A failure (1.32) would mean the native path wrongly reused the Omnigent relay's
    add-delta semantics, double-counting the session cost.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    for cost in (0.42, 0.90):
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={
                "type": "external_session_usage",
                "data": {"context_tokens": 100, "cumulative_cost_usd": cost},
            },
        )
        assert resp.status_code == 202, resp.text
    usage = _read_session_usage(db_uri, session["id"])
    assert usage.get("total_cost_usd") == 0.90


async def test_external_session_usage_codex_tokens_priced(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    codex-native cumulative tokens are SET and priced into ``total_cost_usd``.

    codex reports cumulative token totals but no cost, so the server prices
    them via ``fetch_model_pricing(model)``. Catalog lookup is disabled in
    tests, so we stub pricing. A failure means codex sessions never get a cost
    even though the token totals are present.
    """
    # 1e-6 / 2e-6 USD per input / output token.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: ModelPricing(input_per_token=1e-6, output_per_token=2e-6),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "context_tokens": 1000,
                "cumulative_input_tokens": 1000,
                "cumulative_output_tokens": 500,
                "model": "databricks-gpt-5-5",
            },
        },
    )
    assert resp.status_code == 202, resp.text
    usage = _read_session_usage(db_uri, session["id"])
    # Token totals SET from the cumulative values.
    assert usage.get("input_tokens") == 1000
    assert usage.get("output_tokens") == 500
    assert usage.get("total_tokens") == 1500
    # cost = 1000*1e-6 + 500*2e-6 = 0.001 + 0.001 = 0.002
    assert usage.get("total_cost_usd") == pytest.approx(0.002)


async def test_external_session_usage_codex_cached_tokens_priced_at_cache_rate(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    codex-native cached input is split out and priced at the cache-read rate.

    Codex's ``cumulative_input_tokens`` is INCLUSIVE of cached tokens; the
    server must subtract ``cumulative_cache_read_input_tokens`` and price that
    portion at the (cheaper) cache-read rate. A failure means cached tokens are
    billed at the full input rate — the over-report this fix targets.
    """
    # Cache read is 10x cheaper than fresh input (1e-7 vs 1e-6).
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: ModelPricing(
            input_per_token=1e-6,
            output_per_token=2e-6,
            cache_read_per_token=1e-7,
        ),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "context_tokens": 1000,
                "cumulative_input_tokens": 1000,
                "cumulative_cache_read_input_tokens": 800,
                "cumulative_output_tokens": 500,
                "model": "gpt-5",
            },
        },
    )
    assert resp.status_code == 202, resp.text
    usage = _read_session_usage(db_uri, session["id"])
    # input_tokens is the NON-cached remainder (1000 - 800); cache_read holds
    # the split-out cached portion. If input_tokens were 1000 the split didn't
    # happen and cached tokens would be billed at the full input rate.
    assert usage.get("input_tokens") == 200
    assert usage.get("cache_read_input_tokens") == 800
    assert usage.get("output_tokens") == 500
    # total reflects the FULL input (non-cached + cached) + output, unchanged
    # by the split: 200 + 800 + 500 = 1500.
    assert usage.get("total_tokens") == 1500
    # cost = 200*1e-6 (non-cached) + 800*1e-7 (cache read) + 500*2e-6 (output)
    #      = 0.0002 + 0.00008 + 0.001 = 0.00128.
    # Without the split it would be 0.002 (1000*1e-6 + 500*2e-6) — so this
    # value being strictly below 0.002 proves the cache discount applied.
    assert usage.get("total_cost_usd") == pytest.approx(0.00128)


async def test_external_session_usage_codex_cached_tokens_no_catalog_cache_rate(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no published cache rate (today's ``databricks-*`` catalog entries),
    the cached portion is split out AND priced at the derived ratio default
    (0.10x input), so the dollar cost drops even though the catalog omits a
    cache rate.

    This is the ``databricks-*`` path: the catalog has input/output rates but
    no ``cache_read_per_million_tokens``, so ``compute_llm_cost`` derives the
    cache-read rate from the input rate. Before the ratio fallback this billed
    cache reads at the full input rate (cost 0.002); now it bills 0.10x (cost
    0.00128) — the over-charge this fixes.
    """
    # No cache_read_per_token ⇒ compute_llm_cost derives it as 0.10x input.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: ModelPricing(input_per_token=1e-6, output_per_token=2e-6),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "context_tokens": 1000,
                "cumulative_input_tokens": 1000,
                "cumulative_cache_read_input_tokens": 800,
                "cumulative_output_tokens": 500,
                "model": "databricks-gpt-5-5",
            },
        },
    )
    assert resp.status_code == 202, resp.text
    usage = _read_session_usage(db_uri, session["id"])
    assert usage.get("input_tokens") == 200
    assert usage.get("cache_read_input_tokens") == 800
    assert usage.get("total_tokens") == 1500
    # cost = 200*1e-6 (non-cached) + 800*(1e-6*0.10) (cache read at the derived
    #        ratio) + 500*2e-6 (output) = 0.0002 + 0.00008 + 0.001 = 0.00128.
    # The old full-input fallback gave 0.002; 0.002 here means the ratio
    # fallback regressed.
    assert usage.get("total_cost_usd") == pytest.approx(0.00128)


async def test_accumulate_session_usage_prices_from_usage_model(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relay turn is priced from ``usage.model`` even when the spec pins no
    priceable model.

    This is the fix for delegating supervisors (e.g. debbie on claude-sdk) that
    pin no ``llm.model``: the harness reports the model it actually used in
    ``usage.model`` and the relay cost path prices from it. Here only
    ``"harness-model"`` is priceable — the agent's spec model (its name) is
    NOT — so a recorded cost proves the harness-reported model was used, not
    the spec model.
    """
    from omnigent.server.routes import sessions as sessions_routes

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: (
            ModelPricing(input_per_token=1e-6, output_per_token=2e-6)
            if model == "harness-model"
            else None
        ),
    )
    agent = await create_test_agent(client)  # spec llm.model == agent name (unpriced here)
    session = await _create_session(client, agent["id"])

    sessions_routes._accumulate_session_usage(
        {"usage": {"input_tokens": 1000, "output_tokens": 500, "model": "harness-model"}},
        session["id"],
        SqlAlchemyConversationStore(db_uri),
    )
    usage = _read_session_usage(db_uri, session["id"])
    # cost = 1000*1e-6 + 500*2e-6 = 0.002 — priced via usage.model despite the
    # spec model being absent from the catalog.
    assert usage.get("total_cost_usd") == pytest.approx(0.002)


async def test_accumulate_session_usage_unpriced_without_usage_model(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``usage.model`` and an unpriceable spec model ⇒ no cost recorded.

    Guards the fallback chain and the "unpriced ⇒ no ``total_cost_usd`` key"
    contract: when neither the harness nor the catalog can price the turn, the
    cost key stays absent (so the UI shows "—", not a misleading $0.00).
    """
    from omnigent.server.routes import sessions as sessions_routes

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,  # nothing is priceable
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    sessions_routes._accumulate_session_usage(
        {"usage": {"input_tokens": 1000, "output_tokens": 500}},  # no model in usage
        session["id"],
        SqlAlchemyConversationStore(db_uri),
    )
    usage = _read_session_usage(db_uri, session["id"])
    assert "total_cost_usd" not in usage


async def test_accumulate_session_usage_records_per_model_breakdown(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relay turns are attributed per model; per-model costs sum to the flat total.

    Two turns on two different models must each land in their own ``by_model``
    bucket with their own tokens, and the sum of per-model costs must equal the
    flat session ``total_cost_usd`` — the no-double-count invariant the UI
    relies on. If the per-model cost were attributed to the wrong model or
    double-counted, this sum would diverge from the flat total.
    """
    from omnigent.server.routes import sessions as sessions_routes

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: (
            ModelPricing(input_per_token=1e-6, output_per_token=2e-6)
            if model in {"model-a", "model-b"}
            else None
        ),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    store = SqlAlchemyConversationStore(db_uri)

    sessions_routes._accumulate_session_usage(
        {"usage": {"input_tokens": 1000, "output_tokens": 500, "model": "model-a"}},
        session["id"],
        store,
    )
    sessions_routes._accumulate_session_usage(
        {"usage": {"input_tokens": 200, "output_tokens": 100, "model": "model-b"}},
        session["id"],
        store,
    )

    usage = _read_session_usage(db_uri, session["id"])
    by_model = usage["by_model"]
    # Each turn's tokens land in its own model bucket.
    assert by_model["model-a"]["input_tokens"] == 1000
    assert by_model["model-a"]["output_tokens"] == 500
    assert by_model["model-b"]["input_tokens"] == 200
    assert by_model["model-b"]["output_tokens"] == 100
    # model-a: 1000*1e-6 + 500*2e-6 = 0.002 ; model-b: 200*1e-6 + 100*2e-6 = 0.0004.
    assert by_model["model-a"]["total_cost_usd"] == pytest.approx(0.002)
    assert by_model["model-b"]["total_cost_usd"] == pytest.approx(0.0004)
    # Per-model costs sum to the flat total — proves no double-count / drop.
    assert by_model["model-a"]["total_cost_usd"] + by_model["model-b"][
        "total_cost_usd"
    ] == pytest.approx(usage["total_cost_usd"])


async def test_accumulate_session_usage_unpriced_model_has_tokens_no_cost(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpriced relay model still records its tokens but no per-model cost key.

    Mirrors the flat "priced ⟺ ``total_cost_usd`` key present" contract at the
    per-model level: tokens are attributed even when the model isn't priceable
    (so the token view is complete), but the model's bucket carries no cost key.
    """
    from omnigent.server.routes import sessions as sessions_routes

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,  # nothing priceable
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    sessions_routes._accumulate_session_usage(
        {"usage": {"input_tokens": 1000, "output_tokens": 500, "model": "free-model"}},
        session["id"],
        SqlAlchemyConversationStore(db_uri),
    )

    usage = _read_session_usage(db_uri, session["id"])
    assert usage["by_model"]["free-model"]["input_tokens"] == 1000
    assert usage["by_model"]["free-model"]["output_tokens"] == 500
    assert "total_cost_usd" not in usage["by_model"]["free-model"]


async def test_external_session_usage_records_per_model_breakdown(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A native cumulative usage POST attributes its buckets to the event's model.

    Native harnesses report cumulative SESSION totals (SET semantics), so the
    per-model bucket for the event's ``model`` mirrors the flat cumulative
    buckets. Covers the native write path's per-model capture end-to-end
    through the HTTP endpoint.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "cumulative_input_tokens": 1000,
                "cumulative_output_tokens": 500,
                "cumulative_cost_usd": 0.42,
                "model": "native-model",
            },
        },
    )
    assert resp.status_code == 202, resp.text

    usage = _read_session_usage(db_uri, session["id"])
    bucket = usage["by_model"]["native-model"]
    # No cache split here, so input_tokens == cumulative input; total folds
    # input + output. Cost mirrors the flat (display) total.
    assert bucket["input_tokens"] == 1000
    assert bucket["output_tokens"] == 500
    assert bucket["total_tokens"] == 1500
    assert bucket["total_cost_usd"] == pytest.approx(0.42)


async def test_external_session_usage_cost_only_attributes_to_model(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A claude-native COST-ONLY broadcast attributes its cost to ``by_model``.

    claude-native forwards Claude Code's statusLine total (S) with NO token
    counts, tagging it with the active ``model``. Before this fix the per-model
    view was gated on tokens, so a cost-only broadcast dropped its cost from
    ``by_model`` entirely — the TOKEN USAGE panel undercounted the session
    total by every native (sub-)agent's spend while the flat ``total_cost_usd``
    still included it. The cost must now land in the model's bucket so the
    per-model costs reconcile with the flat total (the UI's no-drop invariant).
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"cumulative_cost_usd": 0.42, "model": "claude-opus-4-8"},
        },
    )
    assert resp.status_code == 202, resp.text

    usage = _read_session_usage(db_uri, session["id"])
    bucket = usage["by_model"]["claude-opus-4-8"]
    # Cost attributed to the model; no token counts (claude-native reports none).
    assert bucket["total_cost_usd"] == pytest.approx(0.42)
    assert "input_tokens" not in bucket
    # Per-model cost reconciles with the flat session total — the exact gap
    # this fix closes ($Session-cost == sum of per-model costs).
    assert bucket["total_cost_usd"] == pytest.approx(usage["total_cost_usd"])


async def test_external_session_usage_cost_only_falls_back_to_model_override(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Cost-only attribution falls back to the session's ``model_override``.

    claude-native's cost broadcast omits ``model`` until the statusLine has
    captured it, but the forwarder mirrors the in-pane active model to
    ``model_override`` each poll. The native write path must consult it (as the
    relay path does) so the cost is still attributed per-model rather than
    silently dropped from the TOKEN USAGE view.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    # Simulate the forwarder having mirrored the active model to model_override.
    SqlAlchemyConversationStore(db_uri).update_conversation(
        session["id"], model_override="claude-sonnet-4-6"
    )

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"cumulative_cost_usd": 0.10},
        },
    )
    assert resp.status_code == 202, resp.text

    usage = _read_session_usage(db_uri, session["id"])
    assert usage["by_model"]["claude-sonnet-4-6"]["total_cost_usd"] == pytest.approx(0.10)


async def test_external_session_usage_policy_cost_only_skips_attribution(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A ``policy_cost_usd``-only mid-turn post records no per-model bucket.

    Mid-sub-agent-run the displayed statusLine total is frozen, so the
    forwarder posts only the advancing enforcement cost (no
    ``cumulative_cost_usd``, no tokens). There is no DISPLAY cost to attribute,
    so attribution is skipped — only the priced display cost flows into
    ``by_model`` (and the badge), keeping the per-model view = the flat
    ``total_cost_usd`` rather than the higher in-flight gate estimate.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_usage", "data": {"policy_cost_usd": 0.50}},
    )
    assert resp.status_code == 202, resp.text

    usage = _read_session_usage(db_uri, session["id"])
    assert "by_model" not in usage
    assert usage.get("policy_cost_usd") == pytest.approx(0.50)


async def test_external_session_usage_event_carries_priced_cost(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A priced session's ``session.usage`` event carries ``total_cost_usd``.

    The web cost indicator reads the cumulative spend off this event. When
    the session is priced (here: claude-native exact billing), the
    server-computed total must ride on the broadcast so the indicator
    updates live without waiting for a snapshot reload.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"context_tokens": 100, "cumulative_cost_usd": 0.42},
        },
    )
    assert resp.status_code == 202, resp.text
    assert [event["type"] for _, event in published] == ["session.usage"]
    assert published[0][1]["total_cost_usd"] == 0.42


async def test_external_session_usage_event_carries_token_breakdown(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A native ``session.usage`` event carries the per-bucket token breakdown.

    The web token-breakdown rows read these cumulative subtree counts off the
    broadcast so they update live alongside the USD cost. The buckets are
    persisted from the ``cumulative_*`` fields, then surfaced under their
    canonical keys (``input_tokens`` etc.).
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "context_tokens": 1000,
                "cumulative_input_tokens": 1000,
                "cumulative_output_tokens": 500,
                "cumulative_cost_usd": 0.42,
                "model": "test-model",
            },
        },
    )
    assert resp.status_code == 202, resp.text
    assert [event["type"] for _, event in published] == ["session.usage"]
    event = published[0][1]
    # The per-model breakdown rides the broadcast, keyed by the event's model.
    assert event["usage_by_model"]["test-model"]["input_tokens"] == 1000
    assert event["usage_by_model"]["test-model"]["output_tokens"] == 500


async def test_external_session_usage_unpriced_omits_cost(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An unpriced session omits ``total_cost_usd`` everywhere — event and store.

    When the model isn't in the pricing catalog, the server records tokens
    but no cost. The ``total_cost_usd`` key must stay **absent** from
    ``session_usage`` (its presence is the "priced" signal) and the
    ``session.usage`` event must not carry it, so the web UI renders "—"
    rather than a misleading ``$0.00``. Contrast with the policy gate,
    which reads ``total_cost_usd`` with a ``0.0`` default and is unaffected
    by the absence.
    """
    # Pricing unavailable for the model — the unpriced path.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {
                "context_tokens": 1000,
                "cumulative_input_tokens": 1000,
                "cumulative_output_tokens": 500,
                "model": "an-unpriced-model",
            },
        },
    )
    assert resp.status_code == 202, resp.text
    # Tokens recorded, but no cost key written.
    usage = _read_session_usage(db_uri, session["id"])
    assert usage.get("total_tokens") == 1500
    assert "total_cost_usd" not in usage
    # The broadcast carries no cost field either.
    assert [event["type"] for _, event in published] == ["session.usage"]
    assert "total_cost_usd" not in published[0][1]


async def test_session_snapshot_includes_priced_cost(
    client: httpx.AsyncClient,
) -> None:
    """
    The session snapshot seeds the cost indicator with the priced total.

    On reload the web client reads ``total_cost_usd`` off the
    ``GET /v1/sessions/{id}`` snapshot (the ``session.usage`` stream has no
    replay). A priced session must surface its cumulative cost there.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"context_tokens": 100, "cumulative_cost_usd": 0.42},
        },
    )
    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["total_cost_usd"] == 0.42


async def test_session_snapshot_unpriced_cost_is_none(
    client: httpx.AsyncClient,
) -> None:
    """
    An unpriced session's snapshot reports ``total_cost_usd`` as ``None``.

    A freshly created session has recorded no priced spend, so the snapshot
    must carry ``None`` (rendered "—" by the client) rather than ``0.0`` —
    the explicit unpriced signal that keeps the UI from implying the
    session was free.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["total_cost_usd"] is None


async def test_external_session_usage_rejects_malformed_cumulative(
    client: httpx.AsyncClient,
) -> None:
    """
    A non-numeric ``cumulative_cost_usd`` is rejected with 400 (fail loud).

    Guards against a forwarder bug silently corrupting persisted cost.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"context_tokens": 100, "cumulative_cost_usd": "lots"},
        },
    )
    assert resp.status_code == 400, resp.text
    assert "cumulative_cost_usd" in resp.text


# Cost-budget policy. Enforcement moved to the ``tool_call`` gate (the
# PreToolUse hook) — see omnigent/policies/builtins/cost.py. The old
# post-hoc ``output_logged`` path that stopped the whole session is gone:
# an over-budget tool call is DENYed (prompting a /model downgrade), while
# logged usage on its own never stops the session.
_COST_GUARD_GUARDRAILS = {
    "policies": {
        "cost_guard": {
            "type": "function",
            "function": {
                "path": "omnigent.policies.builtins.cost.cost_budget",
                "arguments": {"max_cost_usd": 1.0},
            },
        },
    },
}


async def test_external_session_usage_over_budget_does_not_stop_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Over-budget cumulative usage is recorded but never stops the session.

    The cost budget enforces at the ``tool_call`` gate now, not via the
    removed post-hoc ``output_logged`` hook. Posting
    ``external_session_usage`` over the limit must still 202 and persist
    the cost (so the tool-call gate can read it on the next tool call) but
    must NOT set the deliberate-stop label — a regression re-adding a
    session-stop here would silently kill sessions on spend.
    """
    agent = await create_test_agent(client, guardrails=_COST_GUARD_GUARDRAILS)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_session_usage",
            "data": {"cumulative_cost_usd": 2.5},  # over the $1.00 limit
        },
    )
    assert resp.status_code == 202, resp.text
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(session["id"])
    assert conv is not None
    # Cost persisted so the tool-call gate can read it on the next tool call.
    assert conv.session_usage.get("total_cost_usd") == 2.5
    # ...but the session is NOT stopped (the post-hoc cost stop is gone).
    assert conv.labels.get("omnigent.stopped") != "true"


# Cost guard with a soft warning checkpoint, for the relay tool-call ASK
# (non-native) approval path: crossing $0.05 ASKs once; approving must record
# it so it does not re-prompt.
_COST_GUARD_SOFT_GUARDRAILS = {
    "policies": {
        "cost_guard": {
            "type": "function",
            "function": {
                "path": "omnigent.policies.builtins.cost.cost_budget",
                "arguments": {"max_cost_usd": 1.0, "ask_thresholds_usd": [0.05]},
            },
        },
    },
}


async def _evaluate_tool(client: httpx.AsyncClient, sid: str) -> dict[str, Any]:
    """Run a relay tool-call policy query (the non-native gate) and return the verdict.

    Mirrors how the relay asks the server "may I run this tool?": a
    ``function_call`` event carrying ``evaluate_policy`` (a query, not an
    item to persist). ``agent`` / ``call_id`` are required ``FunctionCallData``
    fields; the policy eval keys off ``conv.agent_id``, not these.

    :param client: Test HTTP client.
    :param sid: Session id.
    :returns: The verdict dict, e.g. ``{"verdict": "pending", ...}`` or
        ``{"verdict": "allow"}``.
    """
    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "function_call",
            "data": {
                "evaluate_policy": True,
                "name": "sys_os_shell",
                "arguments": "{}",
                "agent": "test-agent",
                "call_id": f"call_{uuid.uuid4().hex}",
            },
        },
    )
    assert resp.status_code < 300, resp.text
    return resp.json()


async def test_relay_tool_call_ask_approval_persists_checkpoint(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Approving a relay tool-call ASK records the checkpoint so it stops re-asking.

    The non-native (relay) tool-call gate parks a soft-threshold ASK as a
    runner-owned elicitation (verdict ``pending``); the verdict arrives later
    via the ``approval`` event. The deciding policy's ``state_updates`` (the
    crossed cost checkpoint) must be persisted ON ACCEPT — otherwise the
    checkpoint is never recorded and every later tool call re-asks (the bug
    this guards; the native path persisted it but the relay path dropped it).
    """
    from omnigent.policies.builtins.cost import _ASK_APPROVED_KEY

    agent = await create_test_agent(client, guardrails=_COST_GUARD_SOFT_GUARDRAILS)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    store = SqlAlchemyConversationStore(db_uri)

    # Push spend past the $0.05 soft checkpoint (still under the $1.00 limit).
    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_session_usage", "data": {"cumulative_cost_usd": 0.1}},
    )
    assert resp.status_code == 202, resp.text

    # First tool-call query → ASK parked as a pending elicitation.
    verdict = await _evaluate_tool(client, sid)
    assert verdict["verdict"] == "pending", verdict
    eid = verdict["elicitation_id"]

    # Checkpoint not recorded yet — it must land only on approve.
    assert (store.get_conversation(sid).session_state or {}).get(_ASK_APPROVED_KEY) is None

    # Approve → the deciding policy's checkpoint must persist server-side.
    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "approval", "data": {"elicitation_id": eid, "action": "accept"}},
    )
    assert resp.status_code == 202, resp.text
    # 0.05 = the crossed checkpoint recorded on approve. None/0.0 here is the
    # bug: the relay approval dropped the state_updates, so it re-asks forever.
    recorded = (store.get_conversation(sid).session_state or {}).get(_ASK_APPROVED_KEY)
    assert recorded == 0.05, f"checkpoint not recorded on approve: {recorded!r}"

    # Re-evaluating no longer re-asks (pre-fix this returned ``pending`` again).
    assert (await _evaluate_tool(client, sid)).get("verdict") == "allow"


async def test_relay_tool_call_ask_decline_does_not_record_checkpoint(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A declined relay tool-call ASK leaves the checkpoint unrecorded.

    POLICIES.md §7.2: a denied ASK leaves no trace. So a decline must NOT
    persist the checkpoint — the next tool call re-asks (the user did not
    consent to continue past the threshold).
    """
    from omnigent.policies.builtins.cost import _ASK_APPROVED_KEY

    agent = await create_test_agent(client, guardrails=_COST_GUARD_SOFT_GUARDRAILS)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    store = SqlAlchemyConversationStore(db_uri)

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_session_usage", "data": {"cumulative_cost_usd": 0.1}},
    )
    assert resp.status_code == 202, resp.text

    verdict = await _evaluate_tool(client, sid)
    assert verdict["verdict"] == "pending", verdict
    eid = verdict["elicitation_id"]

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "approval", "data": {"elicitation_id": eid, "action": "decline"}},
    )
    assert resp.status_code == 202, resp.text
    # Declined → checkpoint NOT recorded; the next tool call re-asks.
    assert (store.get_conversation(sid).session_state or {}).get(_ASK_APPROVED_KEY) is None
    assert (await _evaluate_tool(client, sid)).get("verdict") == "pending"


async def test_mcp_relay_tool_call_ask_approval_persists_checkpoint(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Approving an MCP relay ``tools/call`` ASK records the checkpoint (no re-prompt).

    The MCP proxy gate parks a soft-threshold ASK as an ``input_required``
    result; the client re-sends (retry) carrying the approval. The deciding
    policy's ``state_updates`` (the cost checkpoint) must be applied on that
    approved retry — otherwise it is dropped and re-prompts every call. This
    is the path openai-agents / MCP-relay sessions actually use (distinct from
    the native ``/policies/evaluate`` and the ``function_call`` query paths).
    The retry's tool execution fails here (no runner bound), but the policy
    write is applied before execution, which is what we assert.
    """
    from omnigent.policies.builtins.cost import _ASK_APPROVED_KEY

    agent = await create_test_agent(client, guardrails=_COST_GUARD_SOFT_GUARDRAILS)
    session = await _create_session(client, agent["id"])
    sid = session["id"]
    store = SqlAlchemyConversationStore(db_uri)

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={"type": "external_session_usage", "data": {"cumulative_cost_usd": 0.1}},
    )
    assert resp.status_code == 202, resp.text

    # First tools/call → over the $0.05 checkpoint → ASK (input_required).
    resp = await client.post(
        f"/v1/sessions/{sid}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mcp__test__echo", "arguments": {}},
        },
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["resultType"] == "input_required", result
    request_state = result["requestState"]
    eid = next(iter(result["inputRequests"]))

    # Not recorded until approve.
    assert (store.get_conversation(sid).session_state or {}).get(_ASK_APPROVED_KEY) is None

    # Retry carrying the approval → the deferred checkpoint must persist
    # (the subsequent tool exec errors for lack of a runner, after the write).
    await client.post(
        f"/v1/sessions/{sid}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "mcp__test__echo",
                "arguments": {},
                "requestState": request_state,
                "inputResponses": {eid: {"action": "accept"}},
            },
        },
    )
    recorded = (store.get_conversation(sid).session_state or {}).get(_ASK_APPROVED_KEY)
    assert recorded == 0.05, f"checkpoint not recorded on MCP approve: {recorded!r}"


async def test_post_external_model_change_publishes_session_model(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_model_change`` persists ``model_override`` and posts a
    typed SessionModelEvent.

    The claude-native forwarder posts this when the user switches model
    inside the Claude Code terminal (``/model`` command or in-TUI
    picker), so the web picker reflects it live (the SSE event) and on
    reload (the persisted override). Asserts the exact published event
    type + payload and the persisted snapshot value.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_model_change", "data": {"model": "opus"}},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}

    assert [event["type"] for _, event in published] == ["session.model"]
    assert published[0][1]["conversation_id"] == session["id"]
    assert published[0][1]["model"] == "opus"

    # Persisted so a reload restores the picker selection.
    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["model_override"] == "opus"


async def test_post_external_model_change_dedupes_when_unchanged(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A repeat ``external_model_change`` for the already-persisted model
    is a no-op: no second ``session.model`` event, no redundant write.

    This is the web→TUI round-trip — the web PATCH set ``model_override``
    to ``"opus"`` and injected ``/model opus``, then the forwarder
    echoes the resulting transcript model back. Without server-side
    dedupe the picker would re-render on its own write.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    # Seed the override the way a web picker PATCH would (silent: no
    # runner forward needed for the test session).
    patch = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"model_override": "opus", "silent": True},
    )
    assert patch.status_code == 200, patch.text
    published.clear()

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_model_change", "data": {"model": "opus"}},
    )
    assert resp.status_code == 202, resp.text
    # Already on "opus" — nothing re-published.
    assert [event["type"] for _, event in published] == []


async def test_post_external_model_change_rejects_empty_model(
    client: httpx.AsyncClient,
) -> None:
    """
    A whitespace-only / missing ``data.model`` 400s.

    Fail loud rather than persist a blank override that would blank the
    picker selection.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_model_change", "data": {"model": "  "}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_model_change" in resp.text


async def test_post_external_model_change_does_not_forward_to_runner(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_model_change`` must NOT re-inject ``/model`` into the runner.

    The terminal is already on the model, so forwarding a ``model_change``
    back would loop (runner injects ``/model`` → transcript records the
    model → forwarder posts ``external_model_change`` → ...). This guards
    that loop boundary — note the contrast with the PATCH path
    (``update_session``), which DOES forward ``model_change`` to the
    runner. The ``session.model`` SSE still fires; the runner sees nothing.
    """
    from omnigent.runtime import set_runner_client

    runner_paths: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record the path of anything routed to the runner; accept all."""
        runner_paths.append(request.url.path)
        return httpx.Response(202, json={})

    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(
            client,
            agent["id"],
            labels={
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "claude-code-native-ui",
            },
        )
        runner_paths.clear()  # ignore any bind-time runner traffic

        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={"type": "external_model_change", "data": {"model": "sonnet"}},
        )
        assert resp.status_code == 202, resp.text
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    # The web picker is nudged...
    assert "session.model" in [event["type"] for _, event in published]
    # ...but nothing was forwarded to the runner (no /model re-injection loop).
    assert runner_paths == [], (
        f"external_model_change must not call the runner; got {runner_paths}"
    )


async def test_post_external_reasoning_effort_change_publishes_session_effort(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_reasoning_effort_change`` persists effort and posts SSE.

    This is the Codex TUI-side thinking-level path. The route must update
    ``conversation.reasoning_effort`` for reload/cost resolution and publish a
    typed live event so the web picker follows the terminal immediately.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_reasoning_effort_change",
            "data": {"reasoning_effort": "medium"},
        },
    )

    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}
    assert [event["type"] for _, event in published] == ["session.reasoning_effort"]
    assert published[0][1]["conversation_id"] == session["id"]
    assert published[0][1]["reasoning_effort"] == "medium"
    # Persisted snapshot proves this was not just a transient SSE update.
    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["reasoning_effort"] == "medium"


async def test_post_external_reasoning_effort_change_clears_effort(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_reasoning_effort_change`` with null clears stale effort.

    Codex reports ``effort: null`` when the session is back on its default
    thinking level. If the route treated null as "omitted", a previous explicit
    ``reasoning_effort`` would survive incorrectly.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    seed = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"reasoning_effort": "high", "silent": True},
    )
    assert seed.status_code == 200, seed.text
    published.clear()

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_reasoning_effort_change",
            "data": {"reasoning_effort": None},
        },
    )

    assert resp.status_code == 202, resp.text
    assert [event["type"] for _, event in published] == ["session.reasoning_effort"]
    assert published[0][1]["reasoning_effort"] is None
    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    # Null from Codex must clear the stored override, not leave "high" behind.
    assert snapshot["reasoning_effort"] is None


async def test_post_external_reasoning_effort_change_rejects_invalid_effort(
    client: httpx.AsyncClient,
) -> None:
    """
    Unsupported terminal-observed effort values fail loud.

    This prevents a malformed Codex event from persisting a value the session
    PATCH path and frontend picker do not understand.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_reasoning_effort_change",
            "data": {"reasoning_effort": "turbo"},
        },
    )

    assert resp.status_code == 400, resp.text
    assert "invalid reasoning_effort" in resp.text


async def test_post_external_codex_collaboration_mode_change_persists_label(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Codex collaboration mode mirrors into the session labels.

    The app-server mode is the only "Plan vs Default" state Codex exposes.
    Persisting it as a label lets session snapshots report the current Codex
    mode without adding a Codex-specific conversation column.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_codex_collaboration_mode_change",
            "data": {"mode": "plan"},
        },
    )

    assert resp.status_code == 202, resp.text
    assert [event["type"] for _, event in published] == ["session.collaboration_mode"]
    assert published[0][1]["conversation_id"] == session["id"]
    assert published[0][1]["mode"] == "plan"
    snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    assert snapshot["labels"]["omnigent.codex_native.collaboration_mode"] == "plan"


async def test_post_external_codex_collaboration_mode_change_rejects_unknown_mode(
    client: httpx.AsyncClient,
) -> None:
    """
    Unknown Codex collaboration mode kinds fail instead of becoming labels."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_codex_collaboration_mode_change",
            "data": {"mode": "review"},
        },
    )

    assert resp.status_code == 400, resp.text
    assert "external_codex_collaboration_mode_change" in resp.text


def _model_change_notes(published: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """
    Extract ``[System: ...]`` model-change note texts from published events.

    The note is appended as a user-role message and broadcast via a
    ``session.input.consumed`` event (see ``_publish_input_consumed``), so
    its text lives at ``event["data"]["data"]["content"][0]["text"]``.

    :param published: ``(session_id, event_dict)`` tuples captured from a
        monkeypatched ``session_stream.publish``.
    :returns: The text of every consumed user message that reads as a
        ``[System: ...]`` marker (model-change notes are the only ones the
        PATCH path emits).
    """
    notes: list[str] = []
    for _, event in published:
        if event.get("type") != "session.input.consumed":
            continue
        data = event.get("data", {}).get("data", {})
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = content[0].get("text", "")
            if text.startswith("[System:"):
                notes.append(text)
    return notes


async def test_patch_model_override_records_system_note_for_inprocess_session(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A web/REPL ``/model`` PATCH on a non-native session appends a durable
    ``[System: model changed to X]`` transcript note.

    This is the visible record the composer no longer shows inline: it
    persists, survives reload, and renders centered+muted in the web UI.
    Asserts the exact note text reached the session stream.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    async def _noop_forward(*_args: Any, **_kwargs: Any) -> None:
        """Isolate the note logic from the live runner forward."""
        return

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner",
        _noop_forward,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    patch = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"model_override": "databricks-gpt-5-4"},
    )
    assert patch.status_code == 200, patch.text

    # Exactly one note, naming the new model. A missing note would mean the
    # gate dropped an in-process session; a wrong string means the text
    # template drifted from what SystemMessageView keys on.
    assert _model_change_notes(published) == ["[System: model changed to databricks-gpt-5-4]"]


async def test_patch_model_override_clear_records_reset_note(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the override (``default``) records a reset note, not a model name."""
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    async def _noop_forward(*_args: Any, **_kwargs: Any) -> None:
        """Isolate the note logic from the live runner forward."""

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner",
        _noop_forward,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    patch = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"model_override": "default"},  # REPL/web clear sentinel
    )
    assert patch.status_code == 200, patch.text
    assert _model_change_notes(published) == ["[System: model reset to the agent default]"]


async def test_patch_model_override_skips_note_for_native_session(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A native-wrapper session (``omnigent.wrapper`` set, here alongside
    ``omnigent.ui == "terminal"``) must NOT get an injected note —
    claude-native uses the picker and codex-native pins its model at launch,
    so an AP-side ``[System: ...]`` item would be a stray/misleading record
    (and pollute the mirrored transcript). The gate keys on the wrapper
    label, NOT ``omnigent.ui`` alone — see
    ``test_patch_model_override_records_note_for_terminal_view_sdk_session``
    for the chat-first SDK session that has ``omnigent.ui`` but no wrapper
    and DOES get the note.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    async def _noop_forward(*_args: Any, **_kwargs: Any) -> None:
        """Isolate the note logic from the live runner forward."""

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner",
        _noop_forward,
    )
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        labels={
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "claude-code-native-ui",
        },
    )

    patch = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"model_override": "opus"},
    )
    assert patch.status_code == 200, patch.text
    # Gate excludes native-wrapper sessions — no transcript note.
    assert _model_change_notes(published) == []


async def test_patch_model_override_records_note_for_terminal_view_sdk_session(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A chat-first SDK session that merely exposes a REPL terminal view
    (``omnigent.ui == "terminal"`` but NO ``omnigent.wrapper``) DOES get the
    note.

    This is the polly / debby case: when such an agent is launched via
    ``omnigent run``, the runner stamps ``omnigent.ui: terminal`` to enable
    the web Chat/Terminal toggle (runner ``app.py``), but the brain is an
    in-process claude-sdk agent whose history Omnigent writes — so a web
    ``/model`` switch should land a durable ``[System: ...]`` note. Gating on
    ``omnigent.ui`` (the pre-fix behavior) wrongly suppressed it; the gate
    must key on the ``omnigent.wrapper`` native label instead.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    async def _noop_forward(*_args: Any, **_kwargs: Any) -> None:
        """Isolate the note logic from the live runner forward."""

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._forward_session_change_to_runner",
        _noop_forward,
    )
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        # Terminal VIEW only — no native wrapper. Mirrors a polly/debby
        # session launched via `omnigent run`.
        labels={"omnigent.ui": "terminal"},
    )

    patch = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"model_override": "databricks-claude-sonnet-4-6"},
    )
    assert patch.status_code == 200, patch.text
    # Note IS recorded: ``omnigent.ui == "terminal"`` alone must not suppress
    # it. An empty list here means the gate regressed to keying on
    # ``omnigent.ui``, re-breaking the polly/debby web ``/model`` feedback.
    assert _model_change_notes(published) == [
        "[System: model changed to databricks-claude-sonnet-4-6]"
    ]


async def test_patch_model_override_silent_skips_note(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``silent`` PATCH (bind-time auto-apply) must NOT record a note — only
    an explicit ``/model`` command should leave a transcript marker.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    patch = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"model_override": "databricks-gpt-5-4", "silent": True},
    )
    assert patch.status_code == 200, patch.text
    assert _model_change_notes(published) == []


async def test_post_external_session_usage_rejects_negative_context_tokens(
    client: httpx.AsyncClient,
) -> None:
    """
    Negative or non-int ``context_tokens`` is rejected with a 400.

    Defends ap-web's ring math (``pct = tokensUsed / contextWindow``)
    from inheriting a bogus negative numerator that would clamp the
    arc to zero and silently mislead users about their context budget.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_usage", "data": {"context_tokens": -1}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_session_usage" in resp.text


async def test_post_external_session_todos_publishes_session_todos(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_session_todos`` publishes a ``session.todos`` SSE event.

    The claude-native forwarder posts this on every PostToolUse / TodoWrite
    hook so the ap-web todo panel updates in real time. A regression here
    would break the panel for ``omnigent claude`` sessions: the UI would
    never receive a ``session.todos`` broadcast and the panel would stay
    blank even when Claude has active tasks.
    """
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    todos = [
        {"content": "Write tests", "status": "completed", "activeForm": "Doing it"},
        {"content": "Fix the bug", "status": "in_progress", "activeForm": "Doing it"},
        {"content": "Review PR", "status": "pending", "activeForm": "Doing it"},
    ]

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_todos", "data": {"todos": todos}},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}

    # Exactly one session.todos event must be published; no extra events.
    assert [ev["type"] for _, ev in published] == ["session.todos"]
    assert published[0][0] == session["id"]
    # The full todos list is forwarded verbatim to subscribers.
    assert published[0][1]["todos"] == todos
    assert published[0][1]["conversation_id"] == session["id"]


async def test_post_external_session_todos_updates_snapshot(
    client: httpx.AsyncClient,
) -> None:
    """
    ``external_session_todos`` persists the list in the in-memory cache so
    the snapshot returned by GET /v1/sessions/{id} reflects it.

    The root bug this tests: ``_EXTERNAL_SESSION_TODOS_TYPE`` was missing
    from ``_ALLOWED_EVENT_TYPES``, so every POST was rejected with a 400
    before ``_handle_external_session_todos`` could populate the cache.
    As a result the snapshot always returned ``todos: []`` even when
    Claude had active tasks.
    """
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sessions_module._session_todos_cache.pop(session["id"], None)

    todos = [
        {"content": "Do something", "status": "in_progress", "activeForm": "Doing it"},
    ]
    try:
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={"type": "external_session_todos", "data": {"todos": todos}},
        )
        assert resp.status_code in (200, 202), resp.text

        snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
        # The snapshot todos field must match exactly what was posted.
        # A failure here means _session_todos_cache was not populated (the
        # original bug), or the snapshot builder ignores the cache.
        assert snapshot["todos"] == todos
    finally:
        sessions_module._session_todos_cache.pop(session["id"], None)


async def test_post_external_session_todos_empty_list_clears_snapshot(
    client: httpx.AsyncClient,
) -> None:
    """
    An empty ``todos`` list is valid and overwrites the previous cache entry.

    Claude posts an empty list when all tasks are done. The panel should
    disappear (renders nothing on empty); the snapshot must reflect the
    cleared list so a page refresh also shows the empty state.
    """
    from omnigent.server.routes import sessions as sessions_module

    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sessions_module._session_todos_cache[session["id"]] = [
        {"content": "Old task", "status": "pending", "activeForm": "Doing it"}
    ]

    try:
        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={"type": "external_session_todos", "data": {"todos": []}},
        )
        assert resp.status_code in (200, 202), resp.text

        snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
        # Empty list must replace the previous cache entry, not be ignored.
        assert snapshot["todos"] == []
    finally:
        sessions_module._session_todos_cache.pop(session["id"], None)


async def test_post_external_session_todos_rejects_missing_todos(
    client: httpx.AsyncClient,
) -> None:
    """
    Payloads missing ``data.todos`` are rejected with a 400.

    Without this guard a malformed forwarder post would silently pass
    without updating the panel, making the bug invisible to the sender.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_todos", "data": {}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_session_todos" in resp.text


async def test_post_external_session_todos_rejects_non_list_todos(
    client: httpx.AsyncClient,
) -> None:
    """
    A non-list ``data.todos`` value is rejected with a 400.

    The handler asserts ``isinstance(todos, list)`` before caching;
    a dict or string would corrupt the cache and produce a malformed
    ``session.todos`` SSE payload downstream.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={"type": "external_session_todos", "data": {"todos": "not a list"}},
    )
    assert resp.status_code == 400, resp.text
    assert "external_session_todos" in resp.text


async def test_post_external_conversation_item_auto_assigns_response_id(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Mirrored items get a server-generated response id when none is sent.

    The claude-native forwarder normally carries Claude's transcript
    response id forward, but the route contract documents that
    ``response_id`` is optional: an external POST that omits it must
    still produce a groupable item with a stable server-assigned id.
    Failure here means the route silently rejects valid forwarder
    output or accepts items that can't be grouped under a response.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture session-stream events emitted by the route.

        :param session_id: Session id passed to ``session_stream``.
        :param event: Event payload published to the stream.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                # response_id omitted to exercise the auto-assign branch.
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "auto-id me"}],
                },
            },
        },
    )
    assert resp.status_code == 202, resp.text

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    items = snap.json()["items"]
    assert len(items) == 1
    persisted_response_id = items[0]["response_id"]
    assert isinstance(persisted_response_id, str) and persisted_response_id, (
        "auto-assigned response_id must be a non-empty string"
    )
    assert published[0][1]["type"] == "session.input.consumed"


async def test_external_user_message_seeds_title_on_claude_native_session(
    client: httpx.AsyncClient,
) -> None:
    """
    First forwarded user message seeds the title on a claude-native session.

    With the placeholder carve-out removed, ``omnigent claude``
    creates sessions without a title — same shape as every other
    untitled session. The transcript forwarder's first
    ``external_conversation_item`` user-message POST must trigger
    the generic ``_seed_missing_title_from_user_message`` path and
    populate the title with the standard first-60-char synthesis.
    This pins the integration of that helper with the external-
    conversation-item route for the claude-native label combination,
    so a future refactor of either side can't silently break the
    sidebar's first-message title for these sessions.
    """
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        # No title — claude-native wrapper no longer stamps one.
        labels={
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "claude-code-native-ui",
        },
    )
    # Precondition: the session was created with no title. If this fails,
    # the create-time placeholder regressed in the wrapper.
    assert session["title"] is None, (
        f"precondition: claude-native session should be created without a "
        f"title now that the placeholder was removed; got {session['title']!r}"
    )

    first_message = "investigate this stack trace from yesterday"
    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "source_id": "src_first_user_msg",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": first_message}],
                },
            },
        },
    )
    assert resp.status_code == 202, resp.text

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    # The synthesized title equals the first message when it already fits
    # the first-60-char budget. If this is empty or unchanged from None,
    # the generic seed path didn't fire on the external_conversation_item
    # route for claude-native labels.
    assert snap.json()["title"] == first_message, (
        f"first user message did not seed the title; title={snap.json()['title']!r}"
    )


async def test_interrupt_on_claude_native_session_skips_idle_publish_on_runner_failure(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    If the runner couldn't deliver the Escape (e.g. tmux pane gone),
    Omnigent must NOT lie to the UI by publishing idle. The spinner spins
    is the right signal — it tells the user the cancel didn't land.

    After the interrupt-unification refactor the Omnigent side no longer
    publishes ``session.status: idle`` itself at all. Idle on a
    claude-native interrupt now comes from the runner's PTY activity
    watcher once the pane quiesces after the Escape (a failed Escape
    naturally surfaces as "no idle" — the pane keeps changing). This
    test acts as a regression guard against re-adding an AP-side idle
    publish — if someone reintroduces the pre-refactor "publish idle on
    2xx" logic, the 503 path here would start leaking idle.
    """
    from omnigent.runtime import session_stream, set_runner_client

    def _handler(request: httpx.Request) -> httpx.Response:
        """Return 503 — the bridge-not-ready shape from the runner."""
        del request
        return httpx.Response(503, json={"error": "claude_native_interrupt_failed"})

    published: list[dict[str, Any]] = []
    real_publish = session_stream.publish

    def _capture_publish(conversation_id: str, event: dict[str, Any]) -> None:
        """Intercept every event the route publishes."""
        published.append({"conversation_id": conversation_id, **event})
        real_publish(conversation_id, event)

    monkeypatch.setattr(session_stream, "publish", _capture_publish)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(
            client,
            agent["id"],
            labels={
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "claude-code-native-ui",
            },
        )

        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={"type": "interrupt", "data": {}},
        )
        assert resp.status_code == 202, resp.text
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    # interrupted still fires (the UI marks the bubble cancelled);
    # idle does not (the Escape didn't land).
    interrupted = [e for e in published if e.get("type") == "session.interrupted"]
    assert interrupted, (
        f"session.interrupted should still publish on runner failure; got {published!r}"
    )
    idle_status = [
        e for e in published if e.get("type") == "session.status" and e.get("status") == "idle"
    ]
    assert not idle_status, (
        f"AP must NOT publish session.status: idle when the runner "
        f"rejected the interrupt — the spinner staying is the correct "
        f"signal that the cancel didn't land. Got: {idle_status!r}"
    )


async def test_stop_session_forwards_stop_session_event_to_runner(
    client: httpx.AsyncClient,
) -> None:
    """
    POST ``/events`` ``stop_session`` forwards the event verbatim to
    the bound runner's ``/events`` endpoint.

    The Omnigent server stays harness-agnostic: it doesn't kill anything
    itself, it relays a ``{"type": "stop_session"}`` event to the
    runner, whose dispatch decides what to do (hard-kill tmux for
    claude-native, 204 for in-process). This pins that the Omnigent forward
    fires and addresses the runner's per-session ``/events`` path with
    the right body — a regression that dropped the forward, mangled
    the body, or hit the wrong URL would silently make the web UI's
    "Stop session" button a no-op.
    """
    from omnigent.runtime import set_runner_client

    forwarded: list[_ForwardedEffort] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record POSTs to the runner; let snapshot reads pass through."""
        if request.method != "POST":
            return httpx.Response(204)
        body = json.loads(request.content) if request.content else None
        forwarded.append(_ForwardedEffort(url=str(request.url), body=body))
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(
            client,
            agent["id"],
            labels={
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "claude-code-native-ui",
            },
        )

        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={"type": "stop_session", "data": {}},
        )
        assert resp.status_code == 202, resp.text
        # Control event, not a persisted item.
        assert resp.json() == {"queued": False}, (
            f"stop_session is a control event and must return "
            f"{{'queued': False}}; got {resp.json()!r}"
        )
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    # Exactly one POST to the session's /events path, carrying the
    # stop_session type. 0 = the Omnigent branch didn't forward (no-op stop
    # button); 2+ = a duplicate relay. Snapshot GETs are filtered out
    # by the handler, so this isolates the control-event forward.
    events_forwards = [
        f for f in forwarded if f.url == f"http://runner/v1/sessions/{session['id']}/events"
    ]
    assert len(events_forwards) == 1, (
        f"Expected exactly one POST of stop_session to the session's "
        f"/events path, got {len(events_forwards)} within {forwarded!r}"
    )
    assert events_forwards[0].body == {"type": "stop_session"}, (
        f"AP must forward a bare stop_session control event to the "
        f"runner; got {events_forwards[0].body!r}"
    )


@pytest.mark.parametrize(
    "failure_mode",
    [
        # Runner reachable but it couldn't kill the session (503 leg).
        "runner_503",
        # WS tunnel closed mid-POST: a BARE ConnectionError, not an httpx.HTTPError.
        "bare_connection_error",
    ],
)
async def test_stop_session_surfaces_runner_failure_as_error(
    client: httpx.AsyncClient,
    failure_mode: str,
) -> None:
    """
    A runner that can't kill the session propagates to the client as
    an error, not a fake success.

    Unlike effort/model_change (where a dropped forward is benign), a
    failed ``stop_session`` means the session is still alive. If the
    Omnigent server swallowed the runner's 503 and returned 202
    ``{queued: false}``, the web UI would close its confirmation
    dialog as if the session stopped — the exact silent-failure the
    review flagged. This pins that the Omnigent route raises (non-2xx)
    instead, so the frontend mutation lands in its error state and
    can tell the user the stop didn't land. The bare-ConnectionError
    leg pins the WS-tunnel transport error mapping to the same clean
    RUNNER_UNAVAILABLE 503 rather than leaking a raw 500.
    """
    from omnigent.runtime import set_runner_client

    def _handler(request: httpx.Request) -> httpx.Response:
        """Snapshot GETs pass; the stop POST gets the runner failure."""
        if request.method != "POST":
            return httpx.Response(204)
        if failure_mode == "bare_connection_error":
            # What WSTunnelTransport raises when the tunnel closes mid-request.
            raise ConnectionError("tunnel closed mid-request")
        # Shape the runner's _handle_claude_native_stop 503 emits when
        # kill_session can't reach the tmux pane.
        return httpx.Response(503, json={"error": "claude_native_stop_failed"})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(
            client,
            agent["id"],
            labels={
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "claude-code-native-ui",
            },
        )

        resp = await client.post(
            f"/v1/sessions/{session['id']}/events",
            json={"type": "stop_session", "data": {}},
        )
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    # RUNNER_UNAVAILABLE → 503. A 202 here would mean the failure was
    # swallowed and the UI would falsely report success.
    assert resp.status_code == 503, (
        f"A runner 503 on stop_session must surface as a non-2xx to the "
        f"client (RUNNER_UNAVAILABLE → 503), got {resp.status_code}: {resp.text}"
    )
    # The failed stop also lifts the just-installed turn fence: the turn
    # keeps running and nothing else would ever lift it, so leaving it set
    # would silently drop the rest of the turn (live + durable).
    from omnigent.server.routes.sessions import _interrupt_fenced_sessions

    assert session["id"] not in _interrupt_fenced_sessions, (
        "a failed stop_session must remove the interrupt fence it installed"
    )


async def test_stop_session_no_runner_lifts_stop_fence(
    client: httpx.AsyncClient,
) -> None:
    """
    A stop with no runner bound anywhere still removes the turn fence.

    When neither the session router nor the global fallback resolves a
    runner client, ``_stop_session_via_runner`` treats the stop as a
    no-op success (the session is not running on any runner) and does
    not raise. The fence installed just before the forward must not
    outlive that no-op: nothing else would ever lift it, and the
    interrupt branch already unfences in the same no-client situation.
    """
    from omnigent.runtime import set_runner_client
    from omnigent.server.routes.sessions import _interrupt_fenced_sessions

    # Pin the no-runner precondition: no global fallback client either.
    set_runner_client(None)
    session_id: str | None = None
    try:
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]

        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "stop_session", "data": {}},
        )
        # No runner resolved = no-op success, not a RUNNER_UNAVAILABLE 503.
        assert resp.status_code == 202, resp.text
        assert session_id not in _interrupt_fenced_sessions, (
            "a no-runner stop_session must remove the fence it installed — "
            "nothing else would ever lift it"
        )
    finally:
        if session_id is not None:
            _interrupt_fenced_sessions.discard(session_id)


@pytest.mark.parametrize(
    "failure_mode",
    [
        # Runner unreachable: the 5s POST raises (the swallowed-HTTPError leg).
        "transport_error",
        # WS tunnel closed mid-POST: a BARE ConnectionError, not an httpx.HTTPError.
        "bare_connection_error",
        # Runner reachable but the cancel didn't land (claude-native 503 leg).
        "runner_503",
        # No runner client resolves at all: nothing was forwarded anywhere.
        "no_runner_client",
    ],
)
async def test_interrupt_forward_failure_lifts_stop_fence(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    """
    A failed interrupt forward removes the fence it just installed.

    The fence is installed BEFORE forwarding the interrupt to the runner.
    If the forward fails the turn keeps running — but the fence used to
    stay set, so the relay dropped the WHOLE remainder of the turn (text,
    items, completed) both live and durably, with nothing left to lift it
    until the next turn. The route must lift the fence when the interrupt
    demonstrably did not land.
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import _interrupt_fenced_sessions

    def _handler(request: httpx.Request) -> httpx.Response:
        """Fail the interrupt POST; let everything else pass through."""
        if request.method == "POST" and request.url.path.endswith("/events"):
            if failure_mode == "transport_error":
                raise httpx.ConnectError("runner unreachable")
            if failure_mode == "bare_connection_error":
                # What WSTunnelTransport raises when the tunnel closes mid-request.
                raise ConnectionError("tunnel closed mid-request")
            return httpx.Response(503, json={"error": "claude_native_interrupt_failed"})
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> httpx.AsyncClient | None:
        """Resolve every session to the failing fake runner (or to none)."""
        del session_id, runner_router
        return None if failure_mode == "no_runner_client" else fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    session_id: str | None = None
    try:
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]

        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "interrupt", "data": {}},
        )
        # Interrupt is best-effort and still ACKs (the UI already marked the
        # bubble interrupted); the fence removal below is the fix under test.
        assert resp.status_code == 202, resp.text
        assert session_id not in _interrupt_fenced_sessions, (
            "a failed interrupt forward must remove the fence it installed — "
            "leaving it set drops the rest of the still-running turn"
        )
    finally:
        if session_id is not None:
            _interrupt_fenced_sessions.discard(session_id)
        await fake_runner.aclose()


async def test_interrupt_forward_success_keeps_stop_fence(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A delivered interrupt keeps the fence so trailing output stays dropped.

    Counterpart of the forward-failure test: it proves the failure-path
    unfence is failure-scoped. If this fence were missing, the cancelled
    turn's trailing deltas would leak into the transcript and live stream
    (the original stop-mid-stream bug).
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes.sessions import _interrupt_fenced_sessions

    def _handler(request: httpx.Request) -> httpx.Response:
        """Accept the interrupt POST (2xx) and all other requests."""
        del request
        return httpx.Response(202)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )

    async def _fake_get_runner_client(
        session_id: str,
        runner_router: object,
    ) -> httpx.AsyncClient:
        """Resolve every session to the accepting fake runner."""
        del session_id, runner_router
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    session_id: str | None = None
    try:
        agent = await create_test_agent(client)
        session = await _create_session(client, agent["id"])
        session_id = session["id"]

        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "interrupt", "data": {}},
        )
        assert resp.status_code == 202, resp.text
        # 2xx from the runner = the cancel landed; the fence must stay so
        # the dying turn's trailing response.* events are suppressed.
        assert session_id in _interrupt_fenced_sessions, (
            "a delivered interrupt must keep the fence installed"
        )
    finally:
        if session_id is not None:
            _interrupt_fenced_sessions.discard(session_id)
        await fake_runner.aclose()


@dataclass
class _ForwardedEffort:
    """
    One forward of an effort change to the runner.

    :param url: Fully-qualified runner URL the Omnigent server POSTed to.
    :param body: Parsed JSON body the Omnigent server sent, or ``None``
        when the request had no body.
    """

    url: str
    body: dict[str, Any] | None


async def test_patch_collaboration_mode_persists_label_and_forwards_event(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    PATCH ``collaboration_mode`` persists the Codex mode and forwards it live.

    The web UI toggle writes through the sessions PATCH route. The server must
    persist the collaboration-mode label for reload, publish a live
    ``session.collaboration_mode`` event for connected clients, and forward a
    harness-agnostic ``plan_mode_change`` control event to the runner so the
    loaded Codex app-server switches modes immediately.
    """
    from omnigent.runtime import set_runner_client

    captured: list[_ForwardedEffort] = []
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record POSTs to /events; let snapshot/status reads pass through."""
        if request.method != "POST":
            return httpx.Response(204)
        body: dict[str, Any] | None = None
        if request.content:
            body = json.loads(request.content)
        captured.append(_ForwardedEffort(url=str(request.url), body=body))
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(
            client,
            agent["id"],
            labels={
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "codex-native-ui",
            },
        )
        captured.clear()

        resp = await client.patch(
            f"/v1/sessions/{session['id']}",
            json={"collaboration_mode": "plan"},
        )
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    assert resp.status_code == 200, resp.text
    assert resp.json()["labels"]["omnigent.codex_native.collaboration_mode"] == "plan"
    plan_forwards = [f for f in captured if f.url.endswith(f"/v1/sessions/{session['id']}/events")]
    assert len(plan_forwards) == 1, f"Expected one runner forward, got {captured!r}"
    assert plan_forwards[0].body == {"type": "plan_mode_change", "enabled": True}
    assert [event["type"] for _, event in published] == ["session.collaboration_mode"]
    assert published[0][1]["mode"] == "plan"


@pytest.mark.parametrize("runner_status", [None, 503], ids=["no_runner", "runner_rejects"])
async def test_patch_collaboration_mode_requires_live_runner_before_persisting(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    runner_status: int | None,
) -> None:
    """
    PATCH ``collaboration_mode`` must not persist UI state before live success.

    A Plan-mode toggle is only correct if Codex app-server accepts the
    corresponding ``thread/settings/update`` through the runner. If no runner
    is reachable, or the runner reports that the loaded Codex bridge cannot
    apply the update, the route must fail and leave the collaboration label
    absent so the web UI rolls back instead of showing a false Plan indicator.
    """
    from omnigent.runtime import set_runner_client

    captured: list[_ForwardedEffort] = []
    published: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    fake_runner: httpx.AsyncClient | None = None
    if runner_status is not None:

        def _handler(request: httpx.Request) -> httpx.Response:
            """
            Record the plan-mode forward and reject it like a missing bridge.

            :param request: Runner request received by the mock transport.
            :returns: Runner response with ``runner_status``.
            """
            body: dict[str, Any] | None = None
            if request.content:
                body = json.loads(request.content)
            captured.append(_ForwardedEffort(url=str(request.url), body=body))
            return httpx.Response(
                runner_status,
                json={
                    "error": "codex_native_settings_update_failed",
                    "detail": "Codex-native plan-mode update requires a loaded Codex bridge.",
                },
            )

        fake_runner = httpx.AsyncClient(
            transport=httpx.MockTransport(_handler),
            base_url="http://runner",
        )

    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(
            client,
            agent["id"],
            labels={
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "codex-native-ui",
            },
        )

        resp = await client.patch(
            f"/v1/sessions/{session['id']}",
            json={"collaboration_mode": "plan"},
        )
        snapshot = (await client.get(f"/v1/sessions/{session['id']}")).json()
    finally:
        if fake_runner is not None:
            await fake_runner.aclose()
        set_runner_client(None)

    assert resp.status_code == 503, resp.text
    assert "Could not enter Plan mode" in resp.text
    assert "omnigent.codex_native.collaboration_mode" not in snapshot["labels"]
    assert published == []
    if runner_status is None:
        assert captured == []
    else:
        plan_forwards = [
            f for f in captured if f.url.endswith(f"/v1/sessions/{session['id']}/events")
        ]
        assert len(plan_forwards) == 1, f"Expected one rejected forward, got {captured!r}"
        assert plan_forwards[0].body == {"type": "plan_mode_change", "enabled": True}


async def test_patch_collaboration_mode_rejects_non_codex_session(
    client: httpx.AsyncClient,
) -> None:
    """
    ``collaboration_mode`` is rejected for sessions that are not Codex-native.

    This keeps a Codex-specific UI control from becoming a generic label write
    that could imply Plan mode on sessions whose runner cannot honor it.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"collaboration_mode": "plan"},
    )

    assert resp.status_code == 400, resp.text
    assert "collaboration_mode is only supported" in resp.text


@pytest.mark.parametrize(
    "native_session,patch_effort,expected_persisted,expected_event_effort",
    [
        # (1) Native + claude-accepted level → POSTs effort_change.
        # The motivating case: dropdown click on a running pane.
        (True, "high", "high", "high"),
        # (2) Non-native + same level → Omnigent server is harness-agnostic
        # so it ALSO POSTs effort_change. The runner's /events
        # dispatch will 204 no-op (covered by a runner-side test);
        # AP's job is just to forward.
        (False, "high", "high", "high"),
        # (3) Clear on native session → persisted None gets forwarded
        # as effort=None. The runner-side native handler decides to
        # skip injection (Claude has no slash for "use spawn default").
        # Before refactor, Omnigent would short-circuit and not POST at all.
        (True, "default", None, None),
        # (4) Level in EFFORT_VALUES but not CLAUDE_EFFORTS (``none``,
        # ``minimal``). After refactor Omnigent no longer filters — it
        # forwards as-is, and the runner-side handler skips injection.
        (True, "none", "none", "none"),
    ],
)
async def test_patch_reasoning_effort_forwards_effort_change_event(
    client: httpx.AsyncClient,
    native_session: bool,
    patch_effort: str,
    expected_persisted: str | None,
    expected_event_effort: str | None,
) -> None:
    """
    PATCH effort always forwards an ``effort_change`` event to
    runner ``/events`` — harness-agnostic on the Omnigent side.

    Before the refactor, Omnigent server made a native-only POST to
    ``/claude-native-effort`` and filtered out clear / unsupported
    values. After the refactor:

    * The POST target is the same generic ``/events`` route every
      other harness event uses.
    * The body is the new ``effort_change`` discriminator with the
      persisted level (or ``None`` for clear) — runner-side dispatch
      decides what to do with it.
    * Omnigent server does not check ``_is_native_terminal_session``
      and does not filter on level — every PATCH that changes effort
      sends the event.

    This parametrize subsumes four pre-refactor tests (the native
    forward, non-native skip, clear skip, unsupported skip): each
    case here was a separate test before, and each had its
    "doesn't POST" / "POSTs to claude-native-effort" assertion
    flipped to "POSTs effort_change to /events".
    """
    from omnigent.runtime import set_runner_client

    captured: list[_ForwardedEffort] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record POSTs to /events; let snapshot/status reads pass through."""
        if request.method != "POST":
            return httpx.Response(204)
        body: dict[str, Any] | None = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        captured.append(_ForwardedEffort(url=str(request.url), body=body))
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session_kwargs = (
            {
                "labels": {
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "claude-code-native-ui",
                },
            }
            if native_session
            else {}
        )
        session = await _create_session(client, agent["id"], **session_kwargs)
        # If we're testing clear, seed an explicit level first so the
        # PATCH actually transitions something (not a no-op).
        if patch_effort == "default":
            seed = await client.patch(
                f"/v1/sessions/{session['id']}",
                json={"reasoning_effort": "high"},
            )
            assert seed.status_code == 200
        captured.clear()

        resp = await client.patch(
            f"/v1/sessions/{session['id']}",
            json={"reasoning_effort": patch_effort},
        )
        assert resp.status_code == 200, resp.text
        # PATCH response reflects the persisted level regardless of
        # forward status (the forward is best-effort).
        assert resp.json()["reasoning_effort"] == expected_persisted, (
            f"PATCH should persist {expected_persisted!r}, got {resp.json()!r}"
        )
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    # Exactly one POST to the unified /events route. 0 = Omnigent server
    # silently dropped the forward (regression in the harness-agnostic
    # always-forward path); 2+ = a legacy branch (e.g. the deleted
    # _forward_claude_native_effort helper) snuck back in alongside
    # the unified path.
    events_forwards = [
        f for f in captured if f.url.endswith(f"/v1/sessions/{session['id']}/events")
    ]
    assert len(events_forwards) == 1, (
        f"Expected exactly one POST to /events, got {len(events_forwards)} "
        f"within all runner requests: {captured!r}"
    )
    forward = events_forwards[0]
    # Body contract: runner reads ``type`` and ``effort`` from this
    # to drive the harness-specific dispatch. A regression in shape
    # (legacy ``{effort: ...}`` without ``type``) would either 400
    # at the runner's discriminator or land as a no-op event.
    assert forward.body == {
        "type": "effort_change",
        "effort": expected_event_effort,
    }, (
        f"Expected body {{'type': 'effort_change', 'effort': "
        f"{expected_event_effort!r}}}, got {forward.body!r}."
    )

    # No POST to the legacy ``/claude-native-effort`` route should
    # happen anymore — its callsite is gone from Omnigent server. A non-
    # empty list here means the deleted ``_forward_claude_native_effort``
    # helper (or an equivalent native-only branch) was re-introduced.
    legacy_forwards = [f for f in captured if "/claude-native-effort" in f.url]
    assert legacy_forwards == [], (
        f"Legacy /claude-native-effort POSTs must not happen anymore; "
        f"AP server forwards effort changes through /events. Got: "
        f"{legacy_forwards!r}"
    )


async def test_silent_patch_skips_effort_change_forward(
    client: httpx.AsyncClient,
) -> None:
    """
    ``silent: true`` persists effort but skips the ``/events`` forward.

    Mirrors the silent-skip semantic the model_override path uses (see
    ``test_silent_patch_skips_claude_native_forward``): bind-time
    sticky-pref handoff on a fresh session must not inject a visible
    ``/effort X`` slash-command item into the pane before the user
    has sent anything. The persisted value is still authoritative —
    the next spawn picks it up via ``--effort``.
    """
    from omnigent.runtime import set_runner_client

    captured: list[_ForwardedEffort] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record POSTs to /events; let snapshot/status reads pass through."""
        if request.method != "POST":
            return httpx.Response(204)
        body: dict[str, Any] | None = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        captured.append(_ForwardedEffort(url=str(request.url), body=body))
        return httpx.Response(204)

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(
            client,
            agent["id"],
            labels={
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "claude-code-native-ui",
            },
        )
        captured.clear()

        resp = await client.patch(
            f"/v1/sessions/{session['id']}",
            json={"reasoning_effort": "high", "silent": True},
        )
        # 200 with persisted value — silent only suppresses the live
        # forward, not the store update.
        assert resp.status_code == 200, resp.text
        assert resp.json()["reasoning_effort"] == "high"
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    # No effort_change forward must reach the runner. A non-empty
    # list here means the silent flag was ignored and bind-time
    # sticky-pref handoff would inject a visible ``/effort high``
    # item into a fresh pane — the bug this skip exists to prevent.
    effort_forwards = [
        f
        for f in captured
        if f.url.endswith(f"/v1/sessions/{session['id']}/events")
        and isinstance(f.body, dict)
        and f.body.get("type") == "effort_change"
    ]
    assert effort_forwards == [], (
        f"silent=True must skip the effort_change forward; got {effort_forwards!r}. "
        f"All runner POSTs: {captured!r}."
    )


async def test_patch_reasoning_effort_swallows_runner_failure(
    client: httpx.AsyncClient,
) -> None:
    """
    Runner 5xx on the effort_change forward does not break PATCH.

    The forward is best-effort: a missing/unresponsive tmux pane
    leaves the persisted value as the authoritative record, and
    the next spawn picks it up via ``--effort``. PATCH must still
    return 200 with the new persisted value.

    Updated for the unified-events refactor: the URL the runner
    rejects is now ``/events`` (not the deleted
    ``/claude-native-effort``), but the swallow-and-return-200
    contract on the Omnigent side is unchanged.
    """
    from omnigent.runtime import set_runner_client

    captured: list[_ForwardedEffort] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """Record POST + 503 — the runner-route's pane-not-ready shape."""
        if request.method != "POST":
            return httpx.Response(204)
        body: dict[str, Any] | None = None
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = None
        captured.append(_ForwardedEffort(url=str(request.url), body=body))
        return httpx.Response(
            503,
            json={
                "error": "claude_native_effort_failed",
                "detail": "tmux target is not advertised",
            },
        )

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="http://runner",
    )
    set_runner_client(fake_runner)
    try:
        agent = await create_test_agent(client)
        session = await _create_session(
            client,
            agent["id"],
            labels={
                "omnigent.ui": "terminal",
                "omnigent.wrapper": "claude-code-native-ui",
            },
        )

        resp = await client.patch(
            f"/v1/sessions/{session['id']}",
            json={"reasoning_effort": "high"},
        )
        # 200 even though the runner 503'd — clicking the dropdown
        # should never appear to fail because the pane is detached.
        assert resp.status_code == 200, resp.text
        assert resp.json()["reasoning_effort"] == "high"
    finally:
        await fake_runner.aclose()
        set_runner_client(None)

    # One effort_change forward was attempted (proves we got far
    # enough to talk to the runner — i.e. the failure was swallowed,
    # not skipped). 0 = Omnigent server bailed before forwarding (regression
    # in the always-forward contract).
    events_forwards = [
        f
        for f in captured
        if f.url.endswith(f"/v1/sessions/{session['id']}/events")
        and isinstance(f.body, dict)
        and f.body.get("type") == "effort_change"
    ]
    assert len(events_forwards) == 1, (
        f"Expected one effort_change POST attempt before swallow, got "
        f"{len(events_forwards)} within all runner requests: {captured!r}"
    )


# ── POST /v1/sessions/{id}/events with client-side tools ────


async def test_session_event_tools_malformed_returns_400(
    client: httpx.AsyncClient,
) -> None:
    """A malformed ``tools`` entry fails fast at the route boundary."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    # Missing function.name is the canonical malformed case.
    resp = await client.post(
        f"/v1/sessions/{session['id']}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
            "tools": [{"type": "function", "function": {"description": "no name"}}],
        },
    )
    assert resp.status_code == 400, (
        f"Expected 400 for malformed tools; got {resp.status_code}: {resp.text[:200]}"
    )


# ── POST /v1/sessions/{id}/events external_codex_subagent_start ──────────────


async def test_external_codex_subagent_start_mints_child_session(
    client: httpx.AsyncClient,
) -> None:
    """
    ``external_codex_subagent_start`` creates a child session with the
    expected labels and returns the child session id.

    The Codex-native forwarder posts this event when it discovers a
    ``collabAgentToolCall`` child thread. The child must surface in the
    ``child_sessions`` listing with the Codex nickname as the ``tool``
    label.

    :param client: The test HTTP client.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "codex-native-ui"},
    )

    resp = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_codex_subagent_start",
            "data": {
                "thread_id": "thread_child_alpha",
                "parent_thread_id": "thread_parent",
                "tool_call_id": "collab_123",
                "prompt": "Audit the auth flow",
                "agent_nickname": "auth-auditor",
                "agent_role": "reviewer",
            },
        },
    )

    assert resp.status_code == 202, f"unexpected status {resp.status_code}: {resp.text}"
    child_id = resp.json()["child_session_id"]
    assert child_id.startswith("conv_"), (
        f"child_session_id must start with conv_; got {child_id!r}"
    )

    children_resp = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    assert children_resp.status_code == 200
    children = children_resp.json()["data"]
    child = next((c for c in children if c["id"] == child_id), None)
    assert child is not None, "Child session must appear in child_sessions listing"

    # tool is derived from agent_nickname → agent_role → "Codex".
    assert child["tool"] == "auth-auditor", (
        f"Expected tool='auth-auditor' (from agent_nickname); got {child['tool']!r}"
    )
    assert child["session_name"] == "thread_child_alpha", (
        f"Expected session_name=thread_id; got {child['session_name']!r}"
    )
    labels = child["labels"]
    assert labels["omnigent.codex_native.subagent_thread_id"] == "thread_child_alpha"
    assert labels["omnigent.codex_native.parent_thread_id"] == "thread_parent"
    assert labels["omnigent.codex_native.collab_tool_call_id"] == "collab_123"
    assert labels["omnigent.codex_native.agent_nickname"] == "auth-auditor"
    assert labels["omnigent.codex_native.agent_role"] == "reviewer"
    # Prompt must surface as the preview before real transcript arrives.
    assert child["last_message_preview"] == "Audit the auth flow", (
        f"Expected prompt preview before transcript exists; got {child['last_message_preview']!r}"
    )


async def test_external_codex_subagent_start_is_idempotent_and_upserts_labels(
    client: httpx.AsyncClient,
) -> None:
    """
    Re-registering the same Codex child thread returns the existing child
    and upserts richer metadata without minting a duplicate row.

    Codex may surface a child first via ``thread/started`` (sparse) and
    later via the parent ``collabAgentToolCall`` (richer). The second POST
    must update the labels without creating a second child row.

    :param client: The test HTTP client.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "codex-native-ui"},
    )

    # First registration — sparse (no nickname yet).
    first = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_codex_subagent_start",
            "data": {
                "thread_id": "thread_child_beta",
                "parent_thread_id": "thread_parent",
            },
        },
    )
    assert first.status_code == 202, first.text
    child_id_first = first.json()["child_session_id"]

    # Second registration — richer (nickname/role added from resume).
    second = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_codex_subagent_start",
            "data": {
                "thread_id": "thread_child_beta",
                "parent_thread_id": "thread_parent",
                "agent_nickname": "Euclid",
                "agent_role": "explorer",
            },
        },
    )
    assert second.status_code == 202, second.text
    child_id_second = second.json()["child_session_id"]

    # Must return the same child id — no duplicate row minted.
    assert child_id_first == child_id_second, (
        f"Idempotent re-registration must return the same child id; "
        f"got {child_id_first!r} then {child_id_second!r}"
    )

    children = (await client.get(f"/v1/sessions/{parent['id']}/child_sessions")).json()["data"]
    matching = [c for c in children if c["session_name"] == "thread_child_beta"]
    # Exactly one row: no duplicate was minted.
    assert len(matching) == 1, (
        f"Expected exactly 1 child row for thread_child_beta; got {len(matching)}"
    )
    # Upserted nickname must be present.
    assert matching[0]["tool"] == "Euclid", (
        f"Expected tool='Euclid' after nickname upsert; got {matching[0]['tool']!r}"
    )


async def test_external_codex_subagent_start_adopts_unlabeled_title_collision(
    client: httpx.AsyncClient,
) -> None:
    """
    Codex re-registration adopts an existing child row that carries the
    colliding title but no thread-id label.

    Same wedge as the claude-native variant: a registration that died
    between ``create_conversation`` and ``set_labels`` leaves a row the
    label-based recovery lookup can't see, so without the title
    fallback every redelivery re-trips the ``(parent, title)`` unique
    index and 500s forever.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "codex-native-ui"},
    )

    # Seed the wedge: the exact collision title, no codex labels.
    seeded = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent["id"],
            "title": "codex-native-ui-subagent:thread_child_gamma",
        },
    )
    assert seeded.status_code == 201, seeded.text
    seeded_id = seeded.json()["id"]

    resp = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_codex_subagent_start",
            "data": {
                "thread_id": "thread_child_gamma",
                "parent_thread_id": "thread_parent",
            },
        },
    )
    # 202, NOT 500 — the collision must be adopted, not escape as an
    # unhandled NameAlreadyExistsError.
    assert resp.status_code == 202, f"unexpected status {resp.status_code}: {resp.text}"
    assert resp.json()["child_session_id"] == seeded_id

    children = (await client.get(f"/v1/sessions/{parent['id']}/child_sessions")).json()["data"]
    # Exactly one child: adoption must not mint a sibling row.
    assert len(children) == 1
    assert children[0]["id"] == seeded_id
    # Thread-id label is healed so future deliveries resolve via the
    # normal label lookup.
    assert (
        children[0]["labels"]["omnigent.codex_native.subagent_thread_id"] == "thread_child_gamma"
    )

    # Redelivery now takes the label-lookup path to the same id.
    again = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_codex_subagent_start",
            "data": {
                "thread_id": "thread_child_gamma",
                "parent_thread_id": "thread_parent",
            },
        },
    )
    assert again.json()["child_session_id"] == seeded_id


async def test_external_codex_subagent_start_rejects_missing_thread_id(
    client: httpx.AsyncClient,
) -> None:
    """
    ``external_codex_subagent_start`` requires a non-empty ``thread_id``.

    The forwarder always supplies the Codex child thread id. A missing or
    empty value is a malformed request that must be rejected with 400.

    :param client: The test HTTP client.
    """
    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "codex-native-ui"},
    )

    resp = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={"type": "external_codex_subagent_start", "data": {}},
    )

    assert resp.status_code == 400, f"Expected 400; got {resp.status_code}: {resp.text}"
    assert "thread_id" in resp.text, (
        f"Expected error message to mention 'thread_id'; got: {resp.text[:300]}"
    )


async def test_external_codex_subagent_terminal_status_accepted_without_runner(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``external_session_status`` on a Codex internal child does not require
    runner delivery.

    Omnigent-spawned native sub-agents must forward terminal status to the
    parent runner inbox. Codex AgentControl children are tracked inside the
    same app-server thread tree and have no runner inbox entry, so the
    ``_require_external_status_forward`` guard must be bypassed for them.

    :param client: The test HTTP client.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.server.routes import sessions as sessions_mod

    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        """
        Capture live stream events from the route.

        :param session_id: Session being published to.
        :param event: Stream event payload.
        :returns: None.
        """
        published.append((session_id, event))

    monkeypatch.setattr(sessions_mod.session_stream, "publish", capture_publish)

    agent = await create_test_agent(client)
    parent = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "codex-native-ui"},
    )
    start_resp = await client.post(
        f"/v1/sessions/{parent['id']}/events",
        json={
            "type": "external_codex_subagent_start",
            "data": {"thread_id": "thread_child_status"},
        },
    )
    assert start_resp.status_code == 202, start_resp.text
    child_id = start_resp.json()["child_session_id"]

    status_resp = await client.post(
        f"/v1/sessions/{child_id}/events",
        json={"type": "external_session_status", "data": {"status": "idle"}},
    )

    # Must succeed — no runner delivery is required for Codex-internal children.
    assert status_resp.status_code == 202, (
        f"Expected 202 for Codex child terminal status; "
        f"got {status_resp.status_code}: {status_resp.text}"
    )
    # The status update must be published to the child session stream.
    assert any(
        sid == child_id and event.get("type") == "session.status" and event.get("status") == "idle"
        for sid, event in published
    ), (
        f"Expected a session.status=idle event for {child_id} in the stream; "
        f"got {[(s, e.get('type')) for s, e in published]}"
    )


async def test_native_message_persisted_when_runner_offline(
    client: httpx.AsyncClient,
) -> None:
    """
    A native message is persisted (not dropped) when no runner is reachable.

    Repro of the desktop bug: a native terminal session whose runner
    crashed before connecting. Posting a message used to raise
    RUNNER_UNAVAILABLE and silently drop the message — the user's input
    vanished on reload while the session stalled. The fix persists the
    user message together with a runner-failure error so both survive,
    and the turn settles ``failed`` instead of 5xx-ing.
    """
    agent = await create_test_agent(client)
    # Native terminal-first session, no host/runner bound → runner offline.
    session = await _create_session(
        client,
        agent["id"],
        labels={"omnigent.wrapper": "claude-code-native-ui"},
    )
    sid = session["id"]

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        },
    )
    # Queued (not RUNNER_UNAVAILABLE) and the persisted user item is returned.
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["queued"] is True
    assert body.get("item_id"), f"expected a persisted item_id, got {body}"

    items = (await client.get(f"/v1/sessions/{sid}/items")).json()["data"]
    # The user's message is durable history — the whole point of the fix.
    user_msgs = [
        i
        for i in items
        if i["type"] == "message" and i.get("content", [{}])[0].get("text") == "hello"
    ]
    assert len(user_msgs) == 1, f"user message not persisted: {items}"
    # A sibling error item explains the failure on reload.
    error_items = [i for i in items if i["type"] == "error"]
    assert len(error_items) == 1, f"expected one error item, got {items}"
    assert error_items[0]["code"] == "runner_failed_to_start"

    # The turn settles failed (not 5xx); the durable error item above is
    # what the web re-renders on reload via itemsToBlocks. (The separate
    # last_task_error snapshot field is the exit-report durability path,
    # which needs a bound runner_id — covered by the snapshot unit test.)
    snap = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snap["status"] == "failed"


async def test_non_native_message_still_raises_when_runner_offline(
    client: httpx.AsyncClient,
) -> None:
    """
    A NON-native message with no runner still fails loud (not persisted).

    The persist-on-offline path is scoped to native terminal sessions
    where the AP server is the legitimate writer. A regular session's
    message would replay to a relaunched runner, so persisting it now
    would desync the store from harness state — it must keep raising.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])  # no native wrapper label
    sid = session["id"]

    resp = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
        },
    )
    # RUNNER_UNAVAILABLE surfaces as an error status, and nothing is persisted.
    assert resp.status_code >= 400, resp.text
    items = (await client.get(f"/v1/sessions/{sid}/items")).json()["data"]
    assert [i for i in items if i["type"] == "error"] == []
