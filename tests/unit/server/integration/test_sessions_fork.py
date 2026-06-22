"""Integration tests for ``POST /v1/sessions/{source_id}/fork``.

Exercises the fork endpoint through the real route → store → DBOS
workflow pipeline with a mocked LLM (``ControllableMockClient``).
Unit tests in ``tests/server/routes/test_sessions_fork.py`` stub
the stores; these tests verify that the joints between route, store,
and workflow actually hold together.

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real SQLAlchemy stores + mock LLM) and helpers from
``tests/server/integration/test_sessions_endpoints.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.unit.server.helpers import create_test_agent
from tests.unit.server.integration.test_sessions_endpoints import (
    _create_session,
    _wait_for_idle,
)

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────


async def _fork_session(
    client: httpx.AsyncClient,
    source_id: str,
    *,
    title: str | None = None,
) -> httpx.Response:
    """
    Fork a session and return the raw ``httpx.Response``.

    Returns the raw response (not just ``.json()``) so callers can
    assert on status codes for both success and error cases.

    :param client: The test HTTP client.
    :param source_id: Session ID to fork.
    :param title: Optional title for the fork.
    :returns: The raw HTTP response.
    """
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    return await client.post(
        f"/v1/sessions/{source_id}/fork",
        json=payload,
    )


async def _get_session_items(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict[str, Any]]:
    """
    Fetch all items for a session via the items endpoint.

    :param client: The test HTTP client.
    :param session_id: Session to query.
    :returns: List of item dicts.
    """
    resp = await client.get(f"/v1/sessions/{session_id}/items")
    assert resp.status_code == 200, (
        f"Failed to list items for {session_id}: {resp.status_code} {resp.text}"
    )
    return resp.json()["data"]


async def _add_comment(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    path: str,
    body: str,
) -> dict[str, Any]:
    """
    Add a file comment to a session and return the created comment.

    :param client: The test HTTP client.
    :param session_id: Session to comment on.
    :param path: Workspace-relative file path the comment anchors to,
        e.g. ``"designs/feature.md"``.
    :param body: Comment text.
    :returns: The created comment dict (includes ``id``, ``created_by``).
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/comments",
        json={"path": path, "body": body, "start_index": 0, "end_index": 4},
    )
    assert resp.status_code == 200, (
        f"Failed to add comment to {session_id}: {resp.status_code} {resp.text}"
    )
    return resp.json()


async def _list_comments(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict[str, Any]]:
    """
    List all comments on a session.

    :param client: The test HTTP client.
    :param session_id: Session to query.
    :returns: List of comment dicts.
    """
    resp = await client.get(f"/v1/sessions/{session_id}/comments")
    assert resp.status_code == 200, (
        f"Failed to list comments for {session_id}: {resp.status_code} {resp.text}"
    )
    return resp.json()


# ── Tests ────────────────────────────────────────────────


# NOTE: ``test_fork_copies_items_and_clones_agent`` and
# ``test_fork_then_send_message_to_fork`` were dropped with the DBOS /
# ``/v1/responses`` removal. Both seeded a session by driving an
# in-process workflow turn (so the source had user+assistant items to
# deep-copy), and the second one also posted a follow-up
# ``/v1/sessions/{id}/events`` to the fork — which now requires a
# runner-bound session to succeed. The remaining tests in this file
# cover the fork-specific code paths (route logic, label/title
# inheritance, store-level deep-copy of explicit items, error cases)
# without needing an executed turn. End-to-end fork-after-execution is
# covered at the e2e level against a real runner.


async def test_fork_empty_session(
    client: httpx.AsyncClient,
) -> None:
    """
    Forking a session that has no items produces an empty idle fork.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    resp = await _fork_session(client, session["id"])
    assert resp.status_code == 201
    fork = resp.json()

    assert fork["status"] == "idle"

    fork_items = await _get_session_items(client, fork["id"])
    # No items to copy — fork should be empty.
    assert fork_items == [], (
        f"Fork of empty session should have no items, got {len(fork_items)}. "
        "If non-empty, fork_conversation is injecting phantom items."
    )


async def test_fork_preserves_labels(
    client: httpx.AsyncClient,
) -> None:
    """
    Forking inherits the source session's labels.
    """
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        title="Original",
        labels={"env": "prod", "team": "ml"},
    )

    resp = await _fork_session(client, session["id"], title="My Fork")
    assert resp.status_code == 201
    fork = resp.json()

    # Title should be the explicit override, not inherited.
    assert fork["title"] == "My Fork", (
        f"Fork title should be 'My Fork' (explicitly set), got {fork['title']!r}."
    )

    # Labels should be inherited from source — fork_conversation
    # copies labels in its transaction.
    assert fork["labels"] == {"env": "prod", "team": "ml"}, (
        f"Fork labels should match source labels, "
        f"got {fork['labels']!r}. If empty, fork_conversation is "
        "not copying labels from the source."
    )


async def test_fork_coding_session_stamps_fork_source_label(
    client: httpx.AsyncClient,
) -> None:
    """
    Forking a session that had a working directory stamps the
    fork-source label on the clone.

    The source binds a ``workspace`` (no host needed — only ``git``
    requires one). The fork deliberately drops the workspace, so the
    clone is unbound; ``fork_conversation`` records provenance via the
    ``omnigent.fork.source_id`` label (value = source id). That label
    is what later makes the online dot report the clone offline and the
    UI open the directory picker. Without it, typing into the clone
    silently drops the message against a runner that can't start.
    """
    agent = await create_test_agent(client)
    # workspace without host_id is valid (the git-requires-host check is
    # the only cross-field constraint); this makes the source a "coding"
    # session for fork purposes.
    create = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "workspace": "/tmp/proj", "title": "Coding"},
    )
    assert create.status_code == 201, f"{create.status_code} {create.text}"
    source = create.json()

    resp = await _fork_session(client, source["id"])
    assert resp.status_code == 201
    fork = resp.json()

    # The label's presence marks "needs a directory before it can run";
    # its value points back at the source so the picker can prefill the
    # original host/dir/branch. A missing key means fork_conversation
    # didn't detect source.workspace; a wrong value means it stamped the
    # wrong source.
    assert fork["labels"].get("omnigent.fork.source_id") == source["id"], (
        f"Expected fork-source label = {source['id']!r}, got "
        f"{fork['labels'].get('omnigent.fork.source_id')!r}."
    )
    # The workspace itself must NOT be carried over — the clone rebinds
    # its own. If this is set, the clone would look bound and skip the
    # picker entirely.
    assert fork.get("workspace") is None, (
        f"Fork must not inherit the source workspace, got {fork.get('workspace')!r}."
    )


async def test_fork_chat_session_has_no_fork_source_label(
    client: httpx.AsyncClient,
) -> None:
    """
    Forking a chat-only session (no working directory) adds no
    fork-source label.

    CUJ 2: a session that never bound a directory has a self-contained
    transcript. Its fork must stay in-process-resumable, so the online
    dot keeps reading it reachable — i.e. the ``omnigent.fork.source_id``
    label must be absent. Stamping it here would wrongly force the clone
    offline and pop a directory picker the session doesn't need.
    """
    agent = await create_test_agent(client)
    source = await _create_session(client, agent["id"], title="Chat only")

    resp = await _fork_session(client, source["id"])
    assert resp.status_code == 201
    fork = resp.json()

    # Absent key — presence would route a chat-only clone into the
    # coding-resume path it doesn't belong in.
    assert "omnigent.fork.source_id" not in fork["labels"], (
        f"Chat-only fork must not carry the fork-source label, got labels {fork['labels']!r}."
    )


async def test_fork_auto_derives_title(
    client: httpx.AsyncClient,
) -> None:
    """
    When no title is provided, the fork's title is derived as
    ``"Fork of <source_title>"``.
    """
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        title="My Chat",
    )

    resp = await _fork_session(client, session["id"])
    assert resp.status_code == 201
    fork = resp.json()

    assert fork["title"] == "Fork of My Chat", (
        f"Auto-derived fork title should be 'Fork of My Chat', "
        f"got {fork['title']!r}. If None or different, the store's "
        "fork_conversation title-derivation logic is broken."
    )


async def test_fork_nonexistent_session_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """
    Forking a session that doesn't exist returns 404 with error
    code ``"not_found"``.
    """
    resp = await _fork_session(client, "conv_does_not_exist")
    assert resp.status_code == 404, (
        f"Fork of nonexistent session should return 404, got {resp.status_code}."
    )
    body = resp.json()
    assert body["error"]["code"] == "not_found", (
        f"Error code should be 'not_found', got {body['error']['code']!r}."
    )


async def test_fork_a_fork(
    client: httpx.AsyncClient,
) -> None:
    """
    A fork can itself be forked (nested fork). All three sessions
    are independent with distinct IDs, agent IDs, and copied items.
    """
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        initial_message="root message",
    )
    await _wait_for_idle(client, session["id"])

    # First fork.
    resp1 = await _fork_session(client, session["id"])
    assert resp1.status_code == 201
    fork1 = resp1.json()

    # Second fork — forking the fork.
    resp2 = await _fork_session(client, fork1["id"])
    assert resp2.status_code == 201
    fork2 = resp2.json()

    # All three must have distinct IDs and agent IDs.
    ids = {session["id"], fork1["id"], fork2["id"]}
    assert len(ids) == 3, f"All three sessions must have unique IDs, got {ids}."
    agent_ids = {session["agent_id"], fork1["agent_id"], fork2["agent_id"]}
    assert len(agent_ids) == 3, f"All three sessions must have unique agent_ids, got {agent_ids}."

    # The second fork should have the same items as the first fork
    # (which has the same items as the source).
    fork1_items = await _get_session_items(client, fork1["id"])
    fork2_items = await _get_session_items(client, fork2["id"])

    assert len(fork2_items) == len(fork1_items), (
        f"Second fork should have {len(fork1_items)} items (same as "
        f"first fork), got {len(fork2_items)}."
    )

    # Content types must match between the two forks.
    fork1_types = [i["type"] for i in fork1_items]
    fork2_types = [i["type"] for i in fork2_items]
    assert fork2_types == fork1_types, (
        f"Fork-of-fork item types {fork2_types} don't match first fork {fork1_types}."
    )

    # IDs must all be distinct across all three sessions.
    all_item_ids = (
        {i["id"] for i in await _get_session_items(client, session["id"])}
        | {i["id"] for i in fork1_items}
        | {i["id"] for i in fork2_items}
    )
    source_items = await _get_session_items(client, session["id"])
    expected_unique = len(source_items) * 3
    assert len(all_item_ids) == expected_unique, (
        f"Expected {expected_unique} unique item IDs across 3 sessions, "
        f"got {len(all_item_ids)}. Some IDs were reused across forks."
    )


# ── Fork copies-vs-not-copied semantics ─────────


async def test_fork_copies_transcript_content_with_fresh_ids(
    client: httpx.AsyncClient,
) -> None:
    """Fork deep-copies item role+text verbatim, with fresh item IDs.

    Stronger than ``test_fork_a_fork`` (types/count only). No runner is
    bound, so the source transcript is just the seeded user message.
    """
    agent = await create_test_agent(client)
    session = await _create_session(
        client,
        agent["id"],
        initial_message="root message",
    )
    await _wait_for_idle(client, session["id"])

    source_items = await _get_session_items(client, session["id"])
    assert source_items[0]["content"][0]["text"] == "root message"

    resp = await _fork_session(client, session["id"])
    assert resp.status_code == 201
    fork_items = await _get_session_items(client, resp.json()["id"])

    assert [i["role"] for i in fork_items] == [i["role"] for i in source_items]
    assert [i["content"][0]["text"] for i in fork_items] == [
        i["content"][0]["text"] for i in source_items
    ]
    # Fresh IDs — copied items must not alias the source rows.
    assert {i["id"] for i in fork_items}.isdisjoint({i["id"] for i in source_items})


async def test_fork_does_not_copy_comments(
    client: httpx.AsyncClient,
) -> None:
    """Fork copies the transcript but NOT file comments, and leaves the
    source's comments untouched."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"], title="Has comments")

    await _add_comment(client, session["id"], path="designs/a.md", body="note one")
    await _add_comment(client, session["id"], path="designs/a.md", body="note two")
    assert len(await _list_comments(client, session["id"])) == 2

    resp = await _fork_session(client, session["id"])
    assert resp.status_code == 201

    assert await _list_comments(client, resp.json()["id"]) == []
    assert len(await _list_comments(client, session["id"])) == 2
