"""Integration tests for ``GET /v1/sessions/{id}/child_sessions``.

The endpoint exposes sub-agent (child) sessions spawned from a parent
session so debug surfaces can enumerate sub-agent calls without parsing
parent ``function_call_output`` JSON handles. Tests here seed sub-agent
conversations directly via the SqlAlchemy stores (rather than going
through the spawn workflow) — the route depends only on
``list_conversations(kind="sub_agent", parent_conversation_id=...)``
and the relay-fed ``_session_status_cache``, so direct seeding gives
fast, deterministic coverage of every response field.

The tasks table has been removed. ``current_task_id`` and ``agent_name``
(previously derived from task rows) are now always ``None``.
``current_task_status`` is derived from session lifecycle state when
available, and is otherwise ``None``. ``agent_id`` is populated from the
conversation row's ``agent_id`` column.
"""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx
import pytest
import yaml

from omnigent.entities import Conversation
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.routes import sessions as sessions_module
from omnigent.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.unit.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_pending_elicitations_index() -> Any:
    """
    Reset the process-global pending-elicitations index around each test.

    The ``pending_elicitations_count`` field on each child summary reads
    this index. Without a reset, an entry recorded by one test would
    inflate another's count (the ``== 0`` assertions would break).
    """
    from omnigent.runtime import pending_elicitations

    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


# ── Helpers ──────────────────────────────────────────────


async def _create_parent_session(
    client: httpx.AsyncClient,
    agent_name: str = "test-agent",
) -> dict[str, Any]:
    """
    Create a parent session bound to a fresh test agent.

    :param client: The test HTTP client.
    :param agent_name: Name for the underlying agent. Tests that
        spin up multiple parents in the same DB must pass distinct
        names — the agent_store enforces unique-by-name and the
        ``test-agent`` default collides on the second call.
    :returns: The ``POST /v1/sessions`` response body.
    """
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
    )
    assert resp.status_code == 201, f"session create failed: {resp.text}"
    return resp.json()


def _seed_child(
    *,
    conv_store: SqlAlchemyConversationStore,
    parent_id: str,
    title: str,
    agent_id: str | None = None,
) -> Conversation:
    """
    Create a child sub-agent conversation.

    Mirrors what :func:`omnigent.tools.builtins.spawn._spawn_one` does,
    minus the workflow start and SSE publish. The tasks table has been
    removed — ``current_task_id``, ``current_task_status``, and
    ``agent_name`` fields in the summary are always ``None``.

    :param conv_store: Store for the child conversation.
    :param parent_id: Parent conversation id, e.g. ``"conv_parent1"``.
    :param title: Sub-agent title in the canonical
        ``"{agent_type}:{session_name}"`` format,
        e.g. ``"researcher:auth"``.
    :param agent_id: Agent id to bind to this conversation (populates
        the ``agent_id`` field in the summary).
    :returns: The created child :class:`Conversation`.
    """
    return conv_store.create_conversation(
        kind="sub_agent",
        title=title,
        parent_conversation_id=parent_id,
        agent_id=agent_id,
    )


# ── 404 ──────────────────────────────────────────────────


async def test_child_sessions_404_for_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    """Route returns 404 when the parent session does not exist."""
    resp = await client.get("/v1/sessions/conv_nonexistent/child_sessions")
    assert resp.status_code == 404


# ── Empty ────────────────────────────────────────────────


async def test_child_sessions_empty_when_no_children(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A parent session with no sub-agents returns an empty page.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    # Empty page: no rows, no cursors, no more pages. Vacuous
    # `len() == 0` would still pass if the route returned ``None``
    # under has_more or omitted the cursors; the exact-match form
    # catches that drift.
    assert body["data"] == []
    assert body["first_id"] is None
    assert body["last_id"] is None
    assert body["has_more"] is False


# ── Full response shape ──────────────────────────────────


async def test_child_sessions_returns_seeded_child_with_full_shape(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A single seeded child surfaces every documented summary field.

    The tasks table has been removed — ``current_task_id``,
    ``current_task_status``, and ``agent_name`` are always ``None``.
    ``agent_id`` is populated from the conversation row's ``agent_id``
    column. ``busy`` is derived from the relay-fed cache (defaults to
    ``False`` with no cache entry).

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    body = resp.json()

    assert len(body["data"]) == 1
    row = body["data"][0]

    # Identity + parent linkage.
    assert row["id"] == child.id
    assert row["object"] == "child_session"
    assert row["parent_session_id"] == session["id"]
    assert row["kind"] == "sub_agent"

    # Title parsing — proves the `:` partition path executed and
    # the prefix/suffix were both surfaced.
    assert row["title"] == "researcher:auth"
    assert row["tool"] == "researcher"
    assert row["session_name"] == "auth"

    # agent_id comes from the conversation row (tasks table removed).
    assert row["agent_id"] == session["agent_id"]
    # agent_name and task fields are None (no tasks table).
    assert row["agent_name"] is None
    assert row["current_task_id"] is None
    assert row["current_task_status"] is None
    assert row["last_task_error"] is None
    # No cache entry → busy=False.
    assert row["busy"] is False

    # No message items yet → no preview.
    assert row["last_message_preview"] is None

    # No outstanding elicitations → 0 (the index is empty for a freshly
    # seeded child that never published an elicitation_request).
    assert row["pending_elicitations_count"] == 0


async def test_child_sessions_surfaces_durable_failure_error(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child with runner-owned failure labels is visibly failed.

    Terminal/native harnesses can fail before a transcript item exists. The
    session-status relay persists that failure as labels; the child summary
    must project them as typed ``last_task_error`` so clients do not parse
    internal labels or render the row as idle.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    conv_store.set_labels(
        child.id,
        {
            sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY: "required_terminal_exited",
            sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY: (
                "Required terminal exited unexpectedly"
            ),
        },
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")

    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["busy"] is False
    assert row["current_task_status"] == "failed"
    assert row["last_task_error"] == {
        "code": "required_terminal_exited",
        "message": "Required terminal exited unexpectedly",
    }


# ── Pending elicitation count ─────────────────────────────


async def test_child_sessions_surfaces_pending_elicitation_count(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child parked on an elicitation reports ``pending_elicitations_count``.

    The Agents rail reads this field to render an "awaiting input"
    badge for a sub-agent that needs attention. The count
    comes from the same in-memory index that feeds the sidebar badge;
    seed it via ``record_publish`` (the SSE publish chokepoint's hook)
    and confirm the endpoint surfaces it.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    from omnigent.runtime import pending_elicitations

    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    pending_elicitations.record_publish(
        child.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_q1",
            "params": {"mode": "form", "message": "Pick one"},
        },
    )
    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    # 1 = the child is parked on one prompt; the rail badges it.
    # 0 here means the count isn't surfaced from the index, so the
    # Agents tab stays blind to a sub-agent needing input.
    assert row["pending_elicitations_count"] == 1


async def test_parent_session_snapshot_replays_child_pending_elicitation(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A parent snapshot includes outstanding child approval payloads.

    A child can publish an elicitation before the user opens the
    parent chat. The live SSE stream has no replay, so the parent
    ``GET /sessions/{id}`` snapshot must synthesize a targeted
    pending event from the child index; otherwise Nessie renders no
    actionable approval card on reload.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    from omnigent.runtime import pending_elicitations

    session = await _create_parent_session(client, agent_name="snapshot-child-pending")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:needs-approval",
        agent_id=session["agent_id"],
    )

    pending_elicitations.record_publish(
        child.id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_child_q1",
            "params": {
                "mode": "form",
                "message": "Approve child command",
                "phase": "codex_command_approval",
            },
        },
    )

    resp = await client.get(f"/v1/sessions/{session['id']}")
    assert resp.status_code == 200, resp.text
    prompts = resp.json()["pending_elicitations"]
    assert len(prompts) == 1
    assert prompts[0]["elicitation_id"] == "elicit_child_q1"
    assert prompts[0]["params"]["message"] == "Approve child command"
    assert prompts[0]["params"]["target_session_id"] == child.id


async def test_child_sessions_zero_pending_when_index_empty(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child with nothing parked reports ``pending_elicitations_count == 0``.

    Inverse of the surfacing test — the field must default to 0, not
    omit or invent a count, so the rail shows no badge for an idle
    sub-agent.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    # No index entry → 0. A non-zero value here means the count is
    # leaking from another session or defaulting wrong.
    assert row["pending_elicitations_count"] == 0


# ── No agent_id (defensive shape) ─────────────────────────


async def test_child_sessions_handles_child_without_agent_id(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child conversation without an agent binding is surfaced with
    ``agent_id=None`` and other task fields nulled out.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="coder:fix-bug",
        agent_id=None,
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == child.id
    # Tool/session_name still parsed from title even without an agent.
    assert row["tool"] == "coder"
    assert row["session_name"] == "fix-bug"
    # Task-derived fields are absent.
    assert row["current_task_id"] is None
    assert row["current_task_status"] is None
    assert row["agent_id"] is None
    assert row["agent_name"] is None
    assert row["busy"] is False


# ── Last message preview ─────────────────────────────────


async def test_child_sessions_returns_latest_message_preview(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child with committed message items surfaces the latest message
    text as ``last_message_preview``.

    Seeds three messages and asserts the route returns the most recent
    one (not the first or a concatenation). Proves both that
    ``list_items(..., order='desc', limit=1)`` is used and that
    ``input_text`` / ``output_text`` blocks are extracted.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    # Use a synthetic response_id since there is no task row.
    response_id = "seed"
    # Append in chronological order so the desc lookup picks the last.
    conv_store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "find the auth bug"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="researcher",
                    content=[{"type": "output_text", "text": "investigating now"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=MessageData(
                    role="assistant",
                    agent="researcher",
                    content=[
                        {
                            "type": "output_text",
                            "text": "Found a stale token check in auth/middleware.py",
                        },
                    ],
                ),
            ),
        ],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["last_message_preview"] == "Found a stale token check in auth/middleware.py"


async def test_child_sessions_preview_skips_meta_messages(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Child-session previews hide durable meta messages.

    A skill invocation can append a hidden ``message.is_meta`` row
    after the last visible message. The Subagents rail must keep
    showing the latest non-meta text instead of leaking raw
    ``<skill>`` content.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    conv_store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="assistant",
                    agent="researcher",
                    content=[{"type": "output_text", "text": "Visible child progress"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "<skill>hidden</skill>"}],
                    is_meta=True,
                ),
            ),
        ],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["last_message_preview"] == "Visible child progress"


@pytest.mark.parametrize(
    "cached_status,expected_busy",
    [
        ("running", True),
        ("waiting", True),
        ("idle", False),
        ("failed", False),
    ],
)
async def test_child_sessions_busy_reflects_relay_status_cache(
    client: httpx.AsyncClient,
    db_uri: str,
    cached_status: str,
    expected_busy: bool,
) -> None:
    """
    ``busy`` mirrors ``_session_status_cache`` when it has data —
    matching the same precedence the single GET uses for ``status``.

    The tasks table is gone, so the cache is the exclusive source of
    busy state. Asserts each of the four cached values the live relay
    can produce maps to the expected ``busy``.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param cached_status: Status value to inject into the cache.
    :param expected_busy: Expected ``busy`` field value in the summary.
    """
    from omnigent.server.routes import sessions as sessions_module

    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )

    # Seed the cache for the child, not the parent.
    sessions_module._session_status_cache[child.id] = cached_status
    try:
        resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
        assert resp.status_code == 200
        row = resp.json()["data"][0]
        assert row["busy"] is expected_busy
    finally:
        sessions_module._session_status_cache.pop(child.id, None)


async def test_child_sessions_truncates_long_message_preview(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Messages longer than the 150-char preview limit are truncated with
    a trailing ellipsis, and the total preview length stays bounded.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    # 200 chars — exceeds the 150 limit.
    long_text = "x" * 200
    conv_store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="assistant",
                    agent="researcher",
                    content=[{"type": "output_text", "text": long_text}],
                ),
            ),
        ],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    preview = resp.json()["data"][0]["last_message_preview"]
    assert preview is not None
    assert preview.endswith("…")
    # The preview replaces one char with the ellipsis, so total
    # length stays at the limit. Failure indicates the truncation
    # math drifted (off-by-one, wrong cap, etc.).
    assert len(preview) == 150


# ── Title without colon (legacy / malformed) ─────────────


async def test_child_sessions_handles_title_without_colon(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A child whose title has no ``:`` is still surfaced.

    The canonical spawn path always writes ``"type:name"``, but the
    schema does not enforce it. The route must treat the title as
    opaque-but-displayable (tool = raw title, session_name = None)
    rather than dropping the row or crashing.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="legacy-untyped",
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    rows = resp.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "legacy-untyped"
    # Defensive parse: whole title falls into `tool`, no session_name.
    assert row["tool"] == "legacy-untyped"
    assert row["session_name"] is None


# ── Title with "ui:" prefix (user-added agent from Web UI) ─


@pytest.mark.parametrize(
    "title,expected_tool,expected_session_name",
    [
        # Canonical user-added Claude Code child.
        ("ui:claude-native-ui:1", "claude-native-ui", "1"),
        # A different agent type + a multi-word label.
        ("ui:codex:my-task-2", "codex", "my-task-2"),
        # A label that itself contains colons: only the first two colons
        # are structural, so the whole remainder is the label.
        ("ui:claude-native-ui:a:b:c", "claude-native-ui", "a:b:c"),
    ],
)
async def test_child_sessions_parses_ui_added_agent_title(
    client: httpx.AsyncClient,
    db_uri: str,
    title: str,
    expected_tool: str,
    expected_session_name: str,
) -> None:
    """
    A child added from the Web UI "Add agent" picker carries the
    3-segment ``"ui:<agent_name>:<user_label>"`` title; the route
    surfaces ``tool=<agent_name>`` and ``session_name=<user_label>``
    so the Agents rail renders it like an LLM-spawned sub-agent.

    The leading ``"ui"`` sentinel distinguishes it from the 2-segment
    ``"<sub_agent_name>:<session_name>"`` form. Without the 3-segment
    branch the route would surface ``tool="ui"`` and
    ``session_name="<agent_name>:<user_label>"`` (the regression this
    guards). The colon-bearing-label case proves only the first two
    colons are structural — the remainder stays in the label.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param title: Seeded ``ui:``-prefixed conversation title.
    :param expected_tool: Agent name the route should surface as ``tool``.
    :param expected_session_name: Label the route should surface as
        ``session_name``.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title=title,
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["title"] == title
    assert row["tool"] == expected_tool
    assert row["session_name"] == expected_session_name


# ── Multiple children, ordering, pagination ───────────────


async def test_child_sessions_multiple_children_default_desc(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Multiple children come back newest-first by default.

    Seeds three children in a known order; the response's first row
    must be the LAST-seeded one. If the route ever changes the
    default sort to ascending, this assert flips and the test fails
    — protecting clients that rely on "most recent first" semantics.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    first = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:a",
        agent_id=session["agent_id"],
    )
    second = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:b",
        agent_id=session["agent_id"],
    )
    third = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:c",
        agent_id=session["agent_id"],
    )

    resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    rows = resp.json()["data"]
    assert [r["id"] for r in rows] == [third.id, second.id, first.id]


async def test_child_sessions_limit_pagination(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``limit`` caps page size and ``has_more`` flags the overflow.

    Three children + ``limit=2`` should return exactly 2 rows with
    ``has_more=True``. If the route forgets to forward ``limit`` or
    mis-maps ``has_more`` from the store's PagedList, this catches
    both regressions.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    for suffix in ("a", "b", "c"):
        _seed_child(
            conv_store=conv_store,
            parent_id=session["id"],
            title=f"researcher:{suffix}",
            agent_id=session["agent_id"],
        )

    resp = await client.get(
        f"/v1/sessions/{session['id']}/child_sessions",
        params={"limit": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["has_more"] is True


# ── Scoping — parent isolation ────────────────────────────


async def test_child_sessions_scoped_to_requested_parent(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Children of session A do not leak into session B's listing.

    Without the ``parent_conversation_id`` filter on
    ``list_conversations``, the route would return every sub-agent
    conversation in the DB. This test seeds children under two
    distinct parents and asserts the response only contains the
    requested parent's rows.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session_a = await _create_parent_session(client, agent_name="agent-a")
    session_b = await _create_parent_session(client, agent_name="agent-b")
    conv_store = SqlAlchemyConversationStore(db_uri)

    child_a = _seed_child(
        conv_store=conv_store,
        parent_id=session_a["id"],
        title="researcher:only-in-a",
        agent_id=session_a["agent_id"],
    )
    child_b = _seed_child(
        conv_store=conv_store,
        parent_id=session_b["id"],
        title="researcher:only-in-b",
        agent_id=session_b["agent_id"],
    )

    resp_a = await client.get(f"/v1/sessions/{session_a['id']}/child_sessions")
    ids_a = [r["id"] for r in resp_a.json()["data"]]
    assert ids_a == [child_a.id]

    resp_b = await client.get(f"/v1/sessions/{session_b['id']}/child_sessions")
    ids_b = [r["id"] for r in resp_b.json()["data"]]
    assert ids_b == [child_b.id]


async def test_closed_child_session_display_is_sanitized_and_read_only(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Closed child sessions hide the internal tombstone and reject chat.

    Legacy closed rows only have a ``:closed:<id>`` title suffix. The
    API must strip that suffix from display fields, synthesize the
    ``omnigent.closed=true`` label for clients, and reject new user
    messages sent directly to the child session.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    """
    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = _seed_child(
        conv_store=conv_store,
        parent_id=session["id"],
        title="researcher:auth",
        agent_id=session["agent_id"],
    )
    tombstoned_title = f"researcher:auth:closed:{child.id}"
    conv_store.update_conversation(child.id, title=tombstoned_title)

    children_resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
    assert children_resp.status_code == 200
    row = children_resp.json()["data"][0]
    assert row["title"] == "researcher:auth"
    assert row["tool"] == "researcher"
    assert row["session_name"] == "auth"
    assert row["labels"][CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE

    snapshot_resp = await client.get(f"/v1/sessions/{child.id}")
    assert snapshot_resp.status_code == 200
    snapshot = snapshot_resp.json()
    assert snapshot["title"] == "researcher:auth"
    assert snapshot["labels"][CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE

    message_resp = await client.post(
        f"/v1/sessions/{child.id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "please continue"}],
            },
        },
    )
    assert message_resp.status_code == 409
    assert "Session is closed" in message_resp.text


# ── Per-child attribution across a 5-10 fan-out ───────────


async def test_child_sessions_per_child_fields_isolated_across_fanout(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With a realistic 5-10 sub-agent fan-out, every per-child field
    stays attributed to its own row — no cross-child bleed.

    The route batch-loads latest message candidates for all children,
    then builds each summary from that map plus the in-memory
    ``_session_status_cache``. The existing multi-child tests only assert
    id ordering; the single-child preview/busy tests can't catch a lookup
    keyed on the wrong id. This seeds eight children, each with a
    distinct latest message and a distinct cached status, then asserts
    the response maps each field back to the correct child.

    The test also monkeypatches ``list_items`` to raise after seeding.
    If the route regresses to the old per-child N+1 lookup, the request
    returns 500 instead of 200.

    :param client: The test HTTP client.
    :param db_uri: Per-test SQLite database URI.
    :param monkeypatch: Pytest monkeypatch fixture used to reject the
        old per-child item-listing path.
    """
    from omnigent.server.routes import sessions as sessions_module

    session = await _create_parent_session(client)
    conv_store = SqlAlchemyConversationStore(db_uri)

    # Eight children — above the "typical 1-5" fan-out the route's
    # N+1 note calls out, so the per-row loop runs enough iterations
    # for a mis-keyed lookup to surface.
    fanout = 8
    # running/waiting → busy True; idle/failed/absent → busy False.
    # Cycling four cached values (plus one uncached) proves the busy
    # bit tracks each child's own cache entry, not a shared default.
    cache_cycle = ["running", "waiting", "idle", "failed", None]

    @dataclass
    class _Expected:
        child_id: str
        tool: str
        session_name: str
        preview: str
        busy: bool

    expected: dict[str, _Expected] = {}
    seeded_cache_ids: list[str] = []
    for i in range(fanout):
        tool = f"agent{i}"
        session_name = f"task-{i}"
        child = _seed_child(
            conv_store=conv_store,
            parent_id=session["id"],
            title=f"{tool}:{session_name}",
            agent_id=session["agent_id"],
        )
        # Distinct latest message per child so a misaligned preview
        # query maps the wrong text and the assertion catches it.
        preview = f"child {i} latest status line"
        conv_store.append(
            child.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="seed",
                    data=MessageData(
                        role="assistant",
                        agent=tool,
                        content=[{"type": "output_text", "text": preview}],
                    ),
                ),
            ],
        )
        cached_status = cache_cycle[i % len(cache_cycle)]
        if cached_status is not None:
            sessions_module._session_status_cache[child.id] = cached_status
            seeded_cache_ids.append(child.id)
        expected[child.id] = _Expected(
            child_id=child.id,
            tool=tool,
            session_name=session_name,
            preview=preview,
            busy=cached_status in ("running", "waiting"),
        )

    try:

        def _fail_list_items(
            _self: SqlAlchemyConversationStore,
            conversation_id: str,
            limit: int = 100,
            after: str | None = None,
            before: str | None = None,
            order: str = "asc",
            type: str | None = None,
        ) -> NoReturn:
            """Fail if child summary rendering uses the old N+1 path.

            The signature mirrors ``SqlAlchemyConversationStore.list_items``
            so keyword calls reach this assertion instead of failing with a
            shape mismatch.

            :param _self: Conversation store instance passed by method binding.
            :param conversation_id: Child conversation id passed by the old path.
            :param limit: Item-page limit passed by the old path.
            :param after: Optional forward cursor passed by the old path.
            :param before: Optional backward cursor passed by the old path.
            :param order: Sort order passed by the old path.
            :param type: Optional item type passed by the old path.
            :returns: Never returns.
            :raises AssertionError: Always, because this path is forbidden.
            """
            del _self, conversation_id, limit, after, before, order, type
            raise AssertionError("child summaries must use the batched preview query")

        monkeypatch.setattr(SqlAlchemyConversationStore, "list_items", _fail_list_items)
        # Default limit is 20, so all eight come back in one page.
        resp = await client.get(f"/v1/sessions/{session['id']}/child_sessions")
        assert resp.status_code == 200
        rows = resp.json()["data"]
        # All seeded children present — a short page would mean the
        # route dropped rows or the default limit shrank below the
        # fan-out.
        assert len(rows) == fanout

        by_id = {row["id"]: row for row in rows}
        # No id appeared twice and none were lost in the loop.
        assert set(by_id) == set(expected)

        for child_id, exp in expected.items():
            row = by_id[child_id]
            # Each field must come from THIS child's row/cache, not a
            # neighbor's. A mismatch on any one points at a lookup
            # keyed on the wrong id during the per-child build.
            assert row["tool"] == exp.tool
            assert row["session_name"] == exp.session_name
            assert row["last_message_preview"] == exp.preview
            assert row["busy"] is exp.busy
            assert row["agent_id"] == session["agent_id"]
    finally:
        for cid in seeded_cache_ids:
            sessions_module._session_status_cache.pop(cid, None)


# ── Native-harness sub-agent terminal-UI label stamping ──────────────


def _bundle_with_harnessed_subagents(name: str, sub_agents: list[dict[str, Any]]) -> bytes:
    """
    Build a bundle whose sub-agents carry an explicit executor harness.

    ``tests.unit.server.helpers.build_agent_bundle`` writes sub-agent configs
    without an ``executor`` block, so it can't express a native harness.
    This minimal builder writes ``agents/<name>/config.yaml`` with the
    given ``harness`` so the create-session path can resolve a native
    sub-agent's harness from the parent bundle.

    :param name: Parent agent name, e.g. ``"nessie-like"``.
    :param sub_agents: Sub-agent dicts, each with ``name`` and ``harness``
        and an optional ``config`` mapping merged into the sub-agent's
        ``executor.config`` (e.g.
        ``{"name": "impl", "harness": "claude-native",
        "config": {"permission_mode": "bypassPermissions"}}`` to declare
        YOLO bypass).
    :returns: A gzipped tar bundle.
    """
    config: dict[str, Any] = {
        "spec_version": 1,
        "name": name,
        "llm": {"model": name, "connection": {"api_key": "test-key"}},
        "executor": {"config": {"harness": "claude-sdk"}},
        "tools": {"agents": [sa["name"] for sa in sub_agents]},
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        cfg = yaml.dump(config).encode()
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(cfg)
        tf.addfile(info, io.BytesIO(cfg))
        for sa in sub_agents:
            sa_config = {
                "spec_version": 1,
                "name": sa["name"],
                "llm": {"model": sa["name"], "connection": {"api_key": "test-key"}},
                # Merge any extra config (e.g. permission_mode / yolo) over
                # the harness so YOLO-declaring bundles can be expressed.
                "executor": {"config": {"harness": sa["harness"], **sa.get("config", {})}},
            }
            sa_bytes = yaml.dump(sa_config).encode()
            sa_info = tarfile.TarInfo(name=f"agents/{sa['name']}/config.yaml")
            sa_info.size = len(sa_bytes)
            tf.addfile(sa_info, io.BytesIO(sa_bytes))
    return buf.getvalue()


async def _create_parent_with_subagents(
    client: httpx.AsyncClient,
    name: str,
    sub_agents: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Register a bundle with harnessed sub-agents and create a parent session.

    :param client: The test HTTP client.
    :param name: Parent agent name (must be unique within the test DB).
    :param sub_agents: Sub-agent dicts with ``name`` + ``harness`` and an
        optional ``config`` mapping (see
        :func:`_bundle_with_harnessed_subagents`).
    :returns: A dict with ``session_id`` (the parent session) and
        ``agent_id`` (the durable agent id resolved from the session).
    """
    bundle = _bundle_with_harnessed_subagents(name, sub_agents)
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 201, f"parent create failed: {resp.text}"
    session_id = resp.json()["session_id"]
    agent_resp = await client.get(f"/v1/sessions/{session_id}/agent")
    assert agent_resp.status_code == 200, f"parent agent lookup failed: {agent_resp.text}"
    return {"session_id": session_id, "agent_id": agent_resp.json()["id"]}


@pytest.mark.parametrize(
    "harness,expected_wrapper",
    [
        ("claude-native", "claude-code-native-ui"),
        ("codex-native", "codex-native-ui"),
    ],
)
async def test_native_subagent_session_stamps_terminal_ui_labels(
    client: httpx.AsyncClient,
    harness: str,
    expected_wrapper: str,
) -> None:
    """
    A sub-agent whose spec uses a native terminal harness gets the
    terminal-first wrapper labels at create time, so the web UI renders
    the Chat/Terminal pill (gated on ``omnigent.ui == "terminal"``).

    Without the stamping, the child row's labels are empty and the pill
    never shows for nessie-style native implementer sub-agents.
    """
    parent = await _create_parent_with_subagents(
        client,
        name=f"orch-{harness}",
        sub_agents=[{"name": "impl", "harness": harness}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-1",
            "sub_agent_name": "impl",
        },
    )
    assert resp.status_code == 201, resp.text
    labels = resp.json()["labels"]
    assert labels.get("omnigent.wrapper") == expected_wrapper
    assert labels.get("omnigent.ui") == "terminal"


@pytest.mark.parametrize(
    "harness,sub_config,expected_args",
    [
        (
            "claude-native",
            {"permission_mode": "bypassPermissions"},
            ["--permission-mode", "bypassPermissions"],
        ),
        (
            "codex-native",
            {"yolo": True},
            ["--dangerously-bypass-approvals-and-sandbox"],
        ),
    ],
)
async def test_native_subagent_yolo_args_derived_from_trusted_spec(
    client: httpx.AsyncClient,
    harness: str,
    sub_config: dict[str, Any],
    expected_args: list[str],
) -> None:
    """
    A YOLO-declaring native worker bundle gets bypass ``terminal_launch_args``.

    The worker sub-agent's own bundle declares its full-bypass intent
    (``permission_mode: bypassPermissions`` for claude-native,
    ``yolo: true`` for codex-native). On a sub-agent create, the server
    derives the matching flag list from that trusted, server-loaded spec
    and persists it as the child session's ``terminal_launch_args`` —
    which the runner appends to the claude / codex argv so the headless
    worker can edit without stalling on an ApprovalCard.

    A failure here means the translation seam regressed and the worker
    would launch in its default prompting mode (and hang headless).
    """
    parent = await _create_parent_with_subagents(
        client,
        name=f"orch-yolo-{harness}",
        sub_agents=[{"name": "impl", "harness": harness, "config": sub_config}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-yolo",
            "sub_agent_name": "impl",
        },
    )
    assert resp.status_code == 201, resp.text
    # The persisted child session carries exactly the YOLO bypass flags;
    # an empty / None value would mean the worker launches prompting.
    assert resp.json()["terminal_launch_args"] == expected_args


async def test_native_subagent_yolo_args_reject_overlong_spec_value(
    client: httpx.AsyncClient,
) -> None:
    """
    Overlong spec-derived launch args fail as ``invalid_input``.

    ``permission_mode`` is declared in the uploaded bundle, but it is
    still persisted as a native CLI argument. The create path must run
    derived args through the same bounds as request-supplied
    ``terminal_launch_args`` and return a client-correctable 400 instead
    of writing an oversized row or surfacing an internal error.
    """
    # Route validation caps each terminal_launch_args entry at 4096
    # bytes/chars; one more proves the derived path is bounded too.
    parent = await _create_parent_with_subagents(
        client,
        name="orch-yolo-overlong-permission-mode",
        sub_agents=[
            {
                "name": "impl",
                "harness": "claude-native",
                "config": {"permission_mode": "x" * 4097},
            }
        ],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-yolo",
            "sub_agent_name": "impl",
        },
    )
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert "invalid terminal_launch_args in sub-agent spec" in error["message"]


@pytest.mark.parametrize(
    "sub_config,expected_persisted",
    [
        # No bypass declared in the trusted spec -> the server derives
        # nothing, so the smuggled flag must NOT be persisted.
        ({}, None),
        # Bypass IS declared -> the server derives the YOLO flag from the
        # trusted spec; the smuggled flag must still be ignored.
        ({"permission_mode": "bypassPermissions"}, ["--permission-mode", "bypassPermissions"]),
    ],
)
async def test_subagent_create_ignores_caller_supplied_launch_args(
    client: httpx.AsyncClient,
    sub_config: dict[str, Any],
    expected_persisted: list[str] | None,
) -> None:
    """
    Caller-supplied ``terminal_launch_args`` never influence a sub-agent create.

    The security boundary: launch wiring for a sub-agent is derived ONLY
    from the trusted, server-loaded sub-spec. A caller who smuggles
    ``terminal_launch_args`` into the sub-agent create body must not be
    able to inject CLI flags into the worker's launch — the persisted
    value must equal what the trusted spec derives (``None`` when the
    spec declares no bypass; the derived YOLO flags when it does), never
    the caller's injected list.

    A failure here means the spawn body became a launch-arg injection
    vector — a caller could, e.g., pass ``--permission-mode
    bypassPermissions`` to a non-YOLO worker and escalate it.
    """
    parent = await _create_parent_with_subagents(
        client,
        name=f"orch-inject-{'yolo' if sub_config else 'plain'}",
        sub_agents=[{"name": "impl", "harness": "claude-native", "config": sub_config}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-inject",
            "sub_agent_name": "impl",
            # Smuggled flags a caller should not be able to apply.
            "terminal_launch_args": ["--dangerously-skip-permissions", "--evil"],
        },
    )
    assert resp.status_code == 201, resp.text
    # Persisted value is what the trusted spec derives, NOT the body args.
    assert resp.json()["terminal_launch_args"] == expected_persisted


@pytest.mark.parametrize(
    "harness,expected_wrapper,expected_model,expected_terminal",
    [
        ("claude-native", "claude-code-native-ui", "claude-native-ui", "claude"),
        ("codex-native", "codex-native-ui", "codex-native-ui", "codex"),
    ],
)
async def test_native_subagent_message_uses_native_terminal_forward(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    expected_wrapper: str,
    expected_model: str,
    expected_terminal: str,
) -> None:
    """
    Native-harness sub-agent child messages take the terminal bypass.

    A ``sys_session_send`` call creates a child session and then posts a
    user message to that child. If the child sub-agent uses
    ``claude-native`` or ``codex-native``, Omnigent must forward the prompt to
    the runner's native terminal event shape and must not persist its
    own AP-side copy; the native transcript forwarder is the single
    writer for conversation items.

    :param client: Test HTTP client.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param harness: Native harness declared by the sub-agent spec,
        e.g. ``"claude-native"``.
    :param expected_wrapper: Wrapper label expected on the child row,
        e.g. ``"claude-code-native-ui"``.
    :param expected_model: Native wrapper model forwarded to the
        runner, e.g. ``"claude-native-ui"``.
    :param expected_terminal: Native terminal resource name sent to
        the runner ensure endpoint, e.g. ``"claude"``.
    """
    parent = await _create_parent_with_subagents(
        client,
        name=f"orch-forward-{harness}",
        sub_agents=[{"name": "impl", "harness": harness}],
    )
    child_resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "impl:task-2",
            "sub_agent_name": "impl",
        },
    )
    assert child_resp.status_code == 201, child_resp.text
    child = child_resp.json()
    assert child["labels"].get("omnigent.wrapper") == expected_wrapper

    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the event Omnigent forwards to the fake runner.

        :param request: HTTP request sent to the fake runner.
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
        Route the native child message to the fake runner.

        :param session_id: Session being routed, e.g. ``"conv_child"``.
        :param runner_router: Real runner router, unused.
        :returns: The fake runner client.
        """
        del session_id, runner_router
        return fake_runner

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_get_runner_client)
    try:
        message_resp = await client.post(
            f"/v1/sessions/{child['id']}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "build the patch"}],
                },
            },
        )
    finally:
        await fake_runner.aclose()

    assert message_resp.status_code == 202, message_resp.text
    # Native (claude-/codex-native) message bypass returns queued=True plus a
    # pending-input id: the message isn't persisted AP-side (the transcript
    # forwarder is the single writer), so the server records a pending-input
    # entry for the optimistic bubble and returns its id.
    message_body = message_resp.json()
    assert message_body["queued"] is True
    assert message_body["pending_id"].startswith("pending_")
    assert forwarded == [
        {
            "path": f"/v1/sessions/{child['id']}/resources/terminals",
            "body": {
                "terminal": expected_terminal,
                "session_key": "main",
                "ensure_native_terminal": True,
            },
        },
        {
            "path": f"/v1/sessions/{child['id']}/events",
            "body": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "build the patch"}],
                "model": expected_model,
                "harness": harness,
                "agent_id": parent["agent_id"],
            },
        },
    ]

    items_resp = await client.get(f"/v1/sessions/{child['id']}/items")
    assert items_resp.status_code == 200, items_resp.text
    assert items_resp.json()["data"] == [], (
        "Native sub-agent prompts must not be persisted by AP; the native "
        "forwarder mirrors accepted terminal transcript items later."
    )


async def test_non_native_subagent_session_has_no_terminal_ui_labels(
    client: httpx.AsyncClient,
) -> None:
    """
    A sub-agent on a non-native harness (e.g. ``claude-sdk``) must NOT get
    the terminal-first labels — it's a headless chat sub-agent with no
    takeover terminal, so the pill must stay hidden.
    """
    parent = await _create_parent_with_subagents(
        client,
        name="orch-sdk",
        sub_agents=[{"name": "reviewer", "harness": "claude-sdk"}],
    )
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent["agent_id"],
            "parent_session_id": parent["session_id"],
            "title": "reviewer:task-1",
            "sub_agent_name": "reviewer",
        },
    )
    assert resp.status_code == 201, resp.text
    labels = resp.json()["labels"]
    assert "omnigent.wrapper" not in labels
    assert "omnigent.ui" not in labels


# ── Multipart (bundled) child creates ────────────────────


async def test_multipart_create_with_parent_links_child(
    client: httpx.AsyncClient,
) -> None:
    """
    A multipart create with ``metadata.parent_session_id`` produces a
    sub-agent child of that session bound to the freshly uploaded agent.

    This is the bundle-mode ``sys_session_create`` server path. The
    child must land in the parent's tree (parent linkage + child_sessions
    listing) and the response must carry the created agent identifiers —
    the runner builds the orchestrator's handle from them.
    """
    parent = await _create_parent_session(client, agent_name="bundle-parent")
    child_bundle = build_agent_bundle(name="bundle-child")
    resp = await client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps({"parent_session_id": parent["id"], "title": "bundled helper"})
        },
        files={"bundle": ("agent.tar.gz", child_bundle, "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    child_id = body["session_id"]
    # The created agent identifiers prove the response contract the
    # runner's bundle-mode handle depends on; a missing/empty agent_id
    # would make sys_session_create fail loud on the runner side.
    assert body["agent_id"].startswith("ag_")
    assert body["agent_name"] == "bundle-child"

    snap = await client.get(f"/v1/sessions/{child_id}")
    assert snap.status_code == 200, snap.text
    # Parent linkage + agent binding traversed metadata → store → row.
    assert snap.json()["parent_session_id"] == parent["id"]
    assert snap.json()["agent_id"] == body["agent_id"]

    listing = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    assert listing.status_code == 200, listing.text
    listed_ids = [c["id"] for c in listing.json()["data"]]
    # kind="sub_agent" is what the child_sessions listing filters on —
    # absence here means the multipart path created a top-level row.
    assert child_id in listed_ids


async def test_multipart_create_with_unknown_parent_404s(
    client: httpx.AsyncClient,
) -> None:
    """
    A multipart create pointing at a nonexistent parent fails with 404
    and creates nothing.

    Without the parent existence check failing loud, the create would
    either orphan a child row or 500 on the FK — both leak a stored
    bundle with no usable session.
    """
    bundle = build_agent_bundle(name="bundle-orphan")
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": "conv_missing"})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 404, resp.text
