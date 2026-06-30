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
