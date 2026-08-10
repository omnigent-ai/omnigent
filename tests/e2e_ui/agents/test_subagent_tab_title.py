"""UI journey: a sub-agent (child) session names the browser tab after
the sub-agent, not the generic "New session" fallback.

Child sessions are not part of the sidebar conversation list, so the
tab-title effect in ``ChatPage`` has no sidebar row to read a title from
and historically fell back to ``UNTITLED_CONVERSATION_LABEL`` ("New
session"). The fix titles the tab after the bound sub-agent — the same
name the chat header renders — so a backgrounded sub-agent tab stays
identifiable.

The child is seeded directly via the JSON ``POST /v1/sessions`` contract
with ``parent_session_id`` set (reusing the parent's bound ``agent_id``),
mirroring ``mobile_session_with_child_agent``. That makes the server
record a ``kind="sub_agent"`` conversation the UI hydrates as a child
without an LLM run, so this stays a fast, deterministic check of the
title path rather than a real sub-agent spawn.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def subagent_session(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed one sub-agent (child) under the parent; yield ``(base_url, child_id)``.

    Mirrors ``mobile_session_with_child_agent`` but surfaces the child id
    instead of the parent's, since this journey navigates INTO the child
    to assert its tab title. The child reuses the parent's bound
    ``agent_id`` so the server records a ``kind="sub_agent"`` conversation
    without an LLM run. Cleaned up by deleting the child on teardown.
    """
    base_url, parent_id = seeded_session
    parent = httpx.get(f"{base_url}/v1/sessions/{parent_id}", timeout=10.0)
    parent.raise_for_status()
    agent_id = parent.json()["agent_id"]

    child = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent_id,
            "sub_agent_name": "researcher",
            "title": "researcher:auth",
        },
        timeout=30.0,
    )
    child.raise_for_status()
    # JSON POST /v1/sessions returns a SessionResponse ("id"), unlike the
    # multipart bundled create used by seeded_session ("session_id").
    child_id = child.json()["id"]
    try:
        yield (base_url, child_id)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


@pytest.fixture
def claude_native_subagent(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Register a claude-native Task child; yield ``(base_url, child_id)``.

    Goes through the real ``external_subagent_start`` contract the
    claude-native forwarder POSTs when Claude Code's Task tool spawns a
    sub-agent, rather than the plain child-create above. That is what
    stamps the ``claude-code-native-ui-subagent`` wrapper label and
    records Claude's own ``subagent_type`` as ``sub_agent_name`` — the
    two fields the identity labels have to choose between.
    """
    base_url, parent_id = seeded_session
    started = httpx.post(
        f"{base_url}/v1/sessions/{parent_id}/events",
        json={
            "type": "external_subagent_start",
            "data": {
                "subagent_id": "a01442e2a856ce778",
                "agent_type": "general-purpose",
                "description": "Read notes.txt contents",
                "tool_use_id": "toolu_01P8fodgyqp4yQcPWCNNdrGJ",
            },
        },
        timeout=30.0,
    )
    started.raise_for_status()
    child_id = started.json()["child_session_id"]
    try:
        yield (base_url, child_id)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


def test_claude_native_subagent_reads_as_the_product(
    page: Page,
    claude_native_subagent: tuple[str, str],
) -> None:
    """A Claude Code Task child names the product, not Claude's own type.

    The child reuses its parent's ``claude-native-ui`` agent row and
    carries ``sub_agent_name="general-purpose"`` (the Task tool's
    ``subagent_type``). Both are Omnigent/Claude internals, and both used
    to reach the screen — the header rendered the raw agent name and the
    composer's identity label rendered the sub-agent type. The wrapper
    label is the only field naming the product, so both surfaces must
    resolve from it.
    """
    base_url, child_id = claude_native_subagent

    # Guard the premise: this only tests the fix if the child really does
    # carry the internal names the UI must not surface.
    child = httpx.get(f"{base_url}/v1/sessions/{child_id}", timeout=10.0).json()
    assert child["sub_agent_name"] == "general-purpose"
    assert child["labels"]["omnigent.wrapper"] == "claude-code-native-ui-subagent"

    page.goto(f"{base_url}/c/{child_id}")
    expect(page.get_by_role("link", name="Back to parent session")).to_be_visible(timeout=30_000)

    # Header breadcrumb: the product, captioned as a sub-agent.
    expect(page.get_by_text("Claude Code", exact=True).first).to_be_visible(timeout=30_000)
    expect(page.get_by_text("Sub-agent", exact=True)).to_be_visible()

    # Claude Code drives its own children, so the composer is read-only.
    expect(page.get_by_placeholder("Claude Code sub-agents are read-only")).to_be_visible()

    # Neither internal reaches the screen anywhere on the page.
    body = page.locator("body").inner_text()
    assert "General-purpose" not in body
    assert "claude-native-ui" not in body


def test_subagent_tab_title_uses_agent_name(
    page: Page,
    subagent_session: tuple[str, str],
) -> None:
    """Opening a child session titles the tab after the sub-agent.

    The bound agent's name — read from ``GET /sessions/{id}/agent``, the
    same source the chat header renders — becomes ``document.title``,
    replacing the "New session" fallback that child sessions used to show.
    """
    base_url, child_id = subagent_session

    # Resolve the expected title from the bound-agent endpoint so the
    # assertion tracks whatever the seeded agent is named rather than a
    # hard-coded string (the UI titles the tab from this same source).
    agent = httpx.get(f"{base_url}/v1/sessions/{child_id}/agent", timeout=10.0)
    agent.raise_for_status()
    agent_name = agent.json()["name"]
    assert agent_name, "bound agent has no name to title the tab with"
    assert agent_name != "New session", "agent name collides with the fallback label"

    page.goto(f"{base_url}/c/{child_id}")

    # The header confirms the page renders as a sub-agent (child) view:
    # the back-to-parent affordance and the "Sub-agent" identity caption.
    expect(page.get_by_role("link", name="Back to parent session")).to_be_visible(timeout=30_000)
    expect(page.get_by_text("Sub-agent", exact=True)).to_be_visible()

    # The tab is named after the sub-agent, not "New session". The leading
    # "● " working-indicator prefix is tolerated so a mid-turn child still
    # passes; the load-bearing part is the agent name, not the fallback.
    expect(page).to_have_title(re.compile(rf"^(?:● )?{re.escape(agent_name)}$"), timeout=30_000)
    assert "New session" not in page.title()
