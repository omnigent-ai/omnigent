"""Integration tests for the manual "Add Agent" / "Add Codex reviewer" flow.

A separately-registered agent is attached as a child of an existing
session via ``POST /v1/sessions`` (``parent_session_id`` set, arbitrary
``agent_id``, no ``sub_agent_name``). Tests run against the in-process
ASGI ``client`` with no runner bound, so they cover the persistence-layer
primitive (child row, parent link, seeded history, scoping) without an
LLM. Runner execution of a heterogeneous Codex child is an e2e concern
and out of scope here.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.unit.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

# Unique marker proving the seeded text reached the child's history only.
_REVIEW_MARKER = "review impl against designs/feature-x.md [marker-7f3a]"

# A Codex reviewer is just another agent with a codex executor block.
_CODEX_EXECUTOR: dict[str, Any] = {"type": "omnigent", "config": {"harness": "codex"}}


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


async def _add_child_agent(
    client: httpx.AsyncClient,
    *,
    parent_session_id: str,
    child_agent_id: str,
    title: str,
    initial_message: str | None = None,
    terminal_launch_args: list[str] | None = None,
) -> httpx.Response:
    """Attach ``child_agent_id`` as a child of ``parent_session_id``.

    Mirrors a Web UI "Add Agent" request: a standalone agent under an
    existing parent, no ``sub_agent_name``.

    :param client: The test HTTP client.
    :param parent_session_id: Existing parent session id.
    :param child_agent_id: Durable id of the agent to attach.
    :param title: Child title in ``"{tool}:{name}"`` form, e.g. ``"codex:reviewer"``.
    :param initial_message: Optional user message to seed the child with.
    :param terminal_launch_args: Optional native-terminal launch args to
        persist on the child, e.g.
        ``["--permission-mode", "bypassPermissions"]``.
    :returns: The raw ``POST /v1/sessions`` response.
    """
    payload: dict[str, Any] = {
        "agent_id": child_agent_id,
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
    if terminal_launch_args is not None:
        payload["terminal_launch_args"] = terminal_launch_args
    return await client.post("/v1/sessions", json=payload)


# ── Manual Add Agent API ─────────────────────────────────


async def test_add_agent_attaches_arbitrary_agent_as_child(
    client: httpx.AsyncClient,
) -> None:
    """A separately registered agent attaches as a child, bound to itself."""
    parent = await _create_parent_session(client, agent_name="parent-coder")
    reviewer = await create_test_agent(client, name="codex-reviewer", executor=_CODEX_EXECUTOR)
    assert reviewer["id"] != parent["agent_id"]
    # GET /v1/sessions/{id}/agent (the create_test_agent return) reports
    # the harness, consistent with the GET /v1/agents catalog. A None or
    # missing value here means the session-agent endpoint dropped the
    # field the Add Agent picker relies on to badge Codex vs Claude.
    assert reviewer["harness"] == "codex"

    resp = await _add_child_agent(
        client,
        parent_session_id=parent["id"],
        child_agent_id=reviewer["id"],
        title="codex:reviewer",
    )
    assert resp.status_code == 201, f"add-agent failed: {resp.text}"
    child = resp.json()

    assert child["parent_session_id"] == parent["id"]
    assert child["agent_id"] == reviewer["id"]
    assert child["id"] != parent["id"]
    # None means the child resolves its own bundle, not a parent sub-spec.
    assert child["sub_agent_name"] is None


async def test_added_child_appears_in_parent_child_sessions(
    client: httpx.AsyncClient,
) -> None:
    """A manually-added child surfaces in ``GET /{parent}/child_sessions``."""
    parent = await _create_parent_session(client, agent_name="parent-coder-2")
    reviewer = await create_test_agent(client, name="codex-reviewer-2", executor=_CODEX_EXECUTOR)
    add = await _add_child_agent(
        client,
        parent_session_id=parent["id"],
        child_agent_id=reviewer["id"],
        title="codex:reviewer",
    )
    assert add.status_code == 201, add.text
    child_id = add.json()["id"]

    resp = await client.get(f"/v1/sessions/{parent['id']}/child_sessions")
    assert resp.status_code == 200, resp.text
    rows = {row["id"]: row for row in resp.json()["data"]}
    assert child_id in rows, f"added child {child_id} missing from {list(rows)}"
    row = rows[child_id]
    # Title parses into tool/session_name the UI badges the child with.
    assert row["tool"] == "codex"
    assert row["session_name"] == "reviewer"
    assert row["parent_session_id"] == parent["id"]


# ── claude-native-ui child gets the wrapper label ────────


async def test_add_claude_native_child_applies_wrapper_label(
    client: httpx.AsyncClient,
) -> None:
    """
    Adding a ``claude-native-ui`` agent as a child stamps the
    claude-native wrapper label at create time.

    The runner routes a session down the Claude Code terminal path only
    when its conversation carries ``omnigent.wrapper`` =
    ``claude-code-native-ui``. For a user-added Claude Code child that
    label must be applied by ``_create_session_from_existing_agent``
    (the dialog can't persist labels before the runner connects), so
    even messages sent before the runner binds take the claude-native
    path. The literals are asserted directly — they are the wire
    contract the runner keys on, so a drifted value must fail here.

    Host/workspace are intentionally left unset on the child: a
    co-located child inherits the parent runner's workspace via the
    inherited ``runner_id``, so no host binding is needed on the row.

    :param client: The test HTTP client.
    """
    parent = await _create_parent_session(client, agent_name="parent-coder-cn")
    # The wrapper-label trigger keys on the agent name being exactly
    # "claude-native-ui" (the seeded Claude Code wrapper agent name).
    cc = await create_test_agent(client, name="claude-native-ui")

    add = await _add_child_agent(
        client,
        parent_session_id=parent["id"],
        child_agent_id=cc["id"],
        title="ui:claude-native-ui:1",
    )
    assert add.status_code == 201, add.text
    child_id = add.json()["id"]

    rows = (await client.get(f"/v1/sessions/{parent['id']}/child_sessions")).json()["data"]
    row = next(r for r in rows if r["id"] == child_id)
    # Both claude-native labels applied at create time — exact key/values
    # (the wire contracts the runner + Web UI key on), not just presence:
    #  - omnigent.wrapper → runner routes the session to Claude Code.
    #  - omnigent.ui=terminal → AppShell renders it terminal-first.
    assert row["labels"].get("omnigent.wrapper") == "claude-code-native-ui"
    assert row["labels"].get("omnigent.ui") == "terminal"
    # The 3-segment "ui:" title still parses to the bound agent + label.
    assert row["tool"] == "claude-native-ui"
    assert row["session_name"] == "1"


async def test_add_agent_child_preserves_body_terminal_launch_args(
    client: httpx.AsyncClient,
) -> None:
    """
    User-added child sessions keep validated body ``terminal_launch_args``.

    Add Agent children set ``parent_session_id`` but leave
    ``sub_agent_name`` null, so they resolve their own bound agent rather
    than a parent sub-spec. They must therefore follow the ordinary
    create-session launch-arg path, not the named-sub-agent derived path.
    """
    parent = await _create_parent_session(client, agent_name="parent-coder-cn-args")
    cc = await create_test_agent(client, name="claude-native-ui")
    launch_args = ["--permission-mode", "bypassPermissions"]

    add = await _add_child_agent(
        client,
        parent_session_id=parent["id"],
        child_agent_id=cc["id"],
        title="ui:claude-native-ui:args",
        terminal_launch_args=launch_args,
    )
    assert add.status_code == 201, add.text
    child = add.json()
    assert child["sub_agent_name"] is None
    assert child["terminal_launch_args"] == launch_args

    snap = await client.get(f"/v1/sessions/{child['id']}")
    assert snap.status_code == 200, snap.text
    assert snap.json()["terminal_launch_args"] == launch_args


# ── Targeted message scoping ─────────────────────────────


async def test_initial_task_is_scoped_to_the_added_child(
    client: httpx.AsyncClient,
) -> None:
    """The prompt seeded on the child lands only in the child, not the parent."""
    parent = await _create_parent_session(client, agent_name="parent-coder-3")
    reviewer = await create_test_agent(client, name="codex-reviewer-3", executor=_CODEX_EXECUTOR)
    add = await _add_child_agent(
        client,
        parent_session_id=parent["id"],
        child_agent_id=reviewer["id"],
        title="codex:reviewer",
        initial_message=_REVIEW_MARKER,
    )
    assert add.status_code == 201, add.text
    child_id = add.json()["id"]

    child_items = (await client.get(f"/v1/sessions/{child_id}/items")).json()["data"]
    child_user_msgs = [
        i for i in child_items if i.get("type") == "message" and i.get("role") == "user"
    ]
    assert len(child_user_msgs) == 1, child_user_msgs
    assert _REVIEW_MARKER in json.dumps(child_user_msgs)

    parent_items = (await client.get(f"/v1/sessions/{parent['id']}/items")).json()["data"]
    # No leak onto the parent transcript.
    assert _REVIEW_MARKER not in json.dumps(parent_items)


# ── Child history / resources independently visible ──────


async def test_added_child_history_and_resources_resolve_independently(
    client: httpx.AsyncClient,
) -> None:
    """The child's snapshot and resources resolve for the child id directly."""
    parent = await _create_parent_session(client, agent_name="parent-coder-4")
    reviewer = await create_test_agent(client, name="codex-reviewer-4", executor=_CODEX_EXECUTOR)
    add = await _add_child_agent(
        client,
        parent_session_id=parent["id"],
        child_agent_id=reviewer["id"],
        title="codex:reviewer",
        initial_message=_REVIEW_MARKER,
    )
    assert add.status_code == 201, add.text
    child_id = add.json()["id"]

    snap = await client.get(f"/v1/sessions/{child_id}")
    assert snap.status_code == 200, snap.text
    assert snap.json()["id"] == child_id
    assert snap.json()["parent_session_id"] == parent["id"]

    # With no runner bound, resources falls back to the local registry.
    res = await client.get(f"/v1/sessions/{child_id}/resources")
    assert res.status_code == 200, res.text
    assert res.json()["object"] == "list"
    assert isinstance(res.json()["data"], list)


# ── Available agents catalog (blocked — tripwire) ────────


@pytest.mark.xfail(
    reason=(
        "No mounted GET /api/agents catalog route (documented in "
        "omnigent/server/API.md, not wired in app.py). Flips to XPASS "
        "when the route lands."
    ),
    strict=False,
)
async def test_available_agents_catalog_endpoint_exists(
    client: httpx.AsyncClient,
) -> None:
    """``GET /api/agents`` lists launchable template agents for Add Agent."""
    await create_test_agent(client, name="catalog-codex", executor=_CODEX_EXECUTOR)

    resp = await client.get("/api/agents")
    assert resp.status_code == 200, f"catalog endpoint not available: {resp.status_code}"
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert any(a.get("name") == "catalog-codex" for a in body["data"])
