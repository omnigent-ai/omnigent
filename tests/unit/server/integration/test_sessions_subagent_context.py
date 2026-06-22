"""Integration tests for sub-agent context inheritance and scoping.

A sub-agent (child) session is created via ``POST /v1/sessions`` with
``parent_session_id`` set. These tests pin down three guarantees the
"child operates with the parent's context without re-pasting it" flow
relies on, all at the persistence layer (in-process ASGI ``client``,
no runner bound, no LLM):

- **Runner co-location** — a child inherits the parent's ``runner_id``
  so it lands on the same runner and therefore shares the same on-disk
  workspace. That shared filesystem (not a transcript copy) is how a
  child reads the files the parent produced without them being re-sent.
- **Transcript isolation** — a child does NOT inherit the parent's
  conversation items (contrast with fork, which copies them). Context
  reaches the child only via the explicit seed message, never by
  implicit history bleed.
- **Per-child scoping** — a message seeded on one child lands only on
  that child, and ``child_sessions`` lists only direct children, so the
  Agents surface targets and enumerates each sub-agent independently.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.unit.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────


async def _create_parent_session(
    client: httpx.AsyncClient,
    *,
    agent_name: str,
) -> dict[str, Any]:
    """Create a top-level parent session bound to a fresh agent.

    :param client: The test HTTP client.
    :param agent_name: Unique agent name (agent_store is unique-by-name).
    :returns: The ``POST /v1/sessions`` response body.
    """
    agent = await create_test_agent(client, name=agent_name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, f"parent session create failed: {resp.text}"
    return resp.json()


async def _create_child_session(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    agent_name: str,
    title: str = "worker:child",
    initial_message: str | None = None,
) -> dict[str, Any]:
    """Create a child session under ``parent_session_id``.

    Uses the current ``POST /v1/sessions`` child-session contract:
    ``parent_session_id`` set, the child bound to its own agent.

    :param client: The test HTTP client.
    :param parent_session_id: Existing parent session id.
    :param agent_name: Unique name for the child's agent.
    :param title: Child title in ``"{tool}:{name}"`` form.
    :param initial_message: Optional user message to seed the child with.
    :returns: The ``POST /v1/sessions`` response body for the child.
    """
    agent = await create_test_agent(client, name=agent_name)
    payload: dict[str, Any] = {
        "agent_id": agent["id"],
        "parent_session_id": parent_session_id,
        "title": title,
    }
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
    resp = await client.post("/v1/sessions", json=payload)
    assert resp.status_code == 201, f"child session create failed: {resp.text}"
    return resp.json()


# ── Runner co-location (shared workspace) ────────────────


@pytest.mark.parametrize(
    "parent_runner_id",
    ["runner_colocated_7c2a", None],
    ids=["parent-has-runner", "parent-has-no-runner"],
)
async def test_child_inherits_parent_runner_affinity(
    client: httpx.AsyncClient,
    db_uri: str,
    parent_runner_id: str | None,
) -> None:
    """A child inherits whatever ``runner_id`` the parent is pinned to.

    Co-locating the child on the parent's runner is what gives the child
    the parent's on-disk workspace, so it can read parent-produced files
    without them being re-pasted. The two cases prove the child copies
    the parent's *actual* value: a pinned runner propagates verbatim, and
    an unpinned parent yields ``None`` (no spurious default).
    """
    parent = await _create_parent_session(client, agent_name=f"ctx-parent-{parent_runner_id}")
    if parent_runner_id is not None:
        conv_store = SqlAlchemyConversationStore(db_uri)
        # Fresh parent starts with runner_id NULL, so the NULL-guarded
        # pin must win. A False here means the parent was already pinned
        # (test setup drift), which would invalidate the assertion below.
        assert conv_store.set_runner_id(parent["id"], parent_runner_id) is True

    child = await _create_child_session(
        client,
        parent_session_id=parent["id"],
        agent_name=f"ctx-child-{parent_runner_id}",
    )

    snap = await client.get(f"/v1/sessions/{child['id']}")
    assert snap.status_code == 200, snap.text
    # The child's runner_id must equal the parent's. If it diverges, the
    # inherit-runner-affinity branch in the create handler regressed and
    # the child would be dispatched to a different runner (a different
    # workspace), breaking file sharing. None must stay None — never a
    # fabricated default.
    assert snap.json()["runner_id"] == parent_runner_id
    assert snap.json()["parent_session_id"] == parent["id"]


# ── Transcript isolation (no implicit history bleed) ─────


async def test_child_does_not_inherit_parent_transcript(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child starts with an empty transcript — parent items don't bleed in.

    Unlike fork (which copies the source transcript), a sub-agent child
    is isolated: the parent's prior conversation never appears in the
    child's history. This is the boundary the "without re-pasting"
    contract sits on — context must be handed to the child explicitly,
    not inherited implicitly.
    """
    parent_marker = "PARENT-DESIGN-DISCUSSION [marker-a91f]"
    parent = await _create_parent_session(client, agent_name="ctx-iso-parent")

    # Seed the parent with a design-discussion item directly in the store
    # (no runner is bound, so a posted message event would 503 before
    # persisting). This is the context a naive "inheritance" would copy.
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.append(
        parent["id"],
        [
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": parent_marker}],
                ),
            ),
        ],
    )

    child = await _create_child_session(
        client,
        parent_session_id=parent["id"],
        agent_name="ctx-iso-child",
    )

    child_items = (await client.get(f"/v1/sessions/{child['id']}/items")).json()["data"]
    # The child has no inherited history. A non-empty list here means the
    # create path leaked the parent transcript into the child (an
    # accidental fork), which is exactly what isolation forbids.
    assert child_items == []

    # Sanity: the parent still owns its item — isolation is one-directional
    # absence on the child, not deletion from the parent.
    parent_items = (await client.get(f"/v1/sessions/{parent['id']}/items")).json()["data"]
    assert parent_marker in json.dumps(parent_items)


# ── Per-child message scoping ────────────────────────────


async def test_sibling_children_seed_context_is_isolated(
    client: httpx.AsyncClient,
) -> None:
    """A message seeded on one child reaches only that child, not its sibling.

    Two children under one parent each get a distinct seed. Each child's
    transcript must contain its own marker and neither the sibling's nor
    leak onto the parent — proving a targeted message addresses a single
    sub-agent rather than fanning out across the tree.
    """
    marker_a = "TASK-FOR-CHILD-A [marker-aaa1]"
    marker_b = "TASK-FOR-CHILD-B [marker-bbb2]"
    parent = await _create_parent_session(client, agent_name="ctx-sib-parent")

    child_a = await _create_child_session(
        client,
        parent_session_id=parent["id"],
        agent_name="ctx-sib-child-a",
        title="worker:a",
        initial_message=marker_a,
    )
    child_b = await _create_child_session(
        client,
        parent_session_id=parent["id"],
        agent_name="ctx-sib-child-b",
        title="worker:b",
        initial_message=marker_b,
    )

    items_a = json.dumps((await client.get(f"/v1/sessions/{child_a['id']}/items")).json())
    items_b = json.dumps((await client.get(f"/v1/sessions/{child_b['id']}/items")).json())
    items_parent = json.dumps((await client.get(f"/v1/sessions/{parent['id']}/items")).json())

    # Each child sees only its own seed. Cross-presence would mean the
    # seed routed to the wrong (or both) child sessions.
    assert marker_a in items_a and marker_b not in items_a
    assert marker_b in items_b and marker_a not in items_b
    # Neither seed leaks onto the parent transcript.
    assert marker_a not in items_parent
    assert marker_b not in items_parent


# ── Agents-surface tree scoping ──────────────────────────


async def test_child_sessions_lists_only_direct_children(
    client: httpx.AsyncClient,
) -> None:
    """``child_sessions`` returns direct children only, not grandchildren.

    The Agents surface enumerates one level of the spawn tree per
    session. A root → child → grandchild chain must show the child under
    the root and the grandchild under the child — never the grandchild
    flattened onto the root.
    """
    root = await _create_parent_session(client, agent_name="ctx-tree-root")
    child = await _create_child_session(
        client,
        parent_session_id=root["id"],
        agent_name="ctx-tree-child",
        title="worker:child",
    )
    grandchild = await _create_child_session(
        client,
        parent_session_id=child["id"],
        agent_name="ctx-tree-grandchild",
        title="worker:grandchild",
    )

    root_children = (await client.get(f"/v1/sessions/{root['id']}/child_sessions")).json()
    root_ids = {row["id"] for row in root_children["data"]}
    # The direct child is listed; the grandchild is one level deeper and
    # must not appear under the root. If it does, the listing query
    # widened from parent_conversation_id to a whole-tree scan.
    assert child["id"] in root_ids
    assert grandchild["id"] not in root_ids

    child_children = (await client.get(f"/v1/sessions/{child['id']}/child_sessions")).json()
    child_ids = {row["id"] for row in child_children["data"]}
    # The grandchild surfaces under its direct parent, confirming the
    # chain is intact rather than the grandchild being orphaned.
    assert grandchild["id"] in child_ids
    assert root["id"] not in child_ids
