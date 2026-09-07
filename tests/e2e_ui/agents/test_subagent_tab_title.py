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

from tests.e2e_ui.conftest import open_right_rail, set_session_task_summary


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

    # Header breadcrumb: the sub-agent segment names the product.
    expect(page.get_by_text("Claude Code", exact=True).first).to_be_visible(timeout=30_000)

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

    # The header confirms the page renders as a sub-agent (child) view via the
    # back-to-parent affordance on the breadcrumb title.
    expect(page.get_by_role("link", name="Back to parent session")).to_be_visible(timeout=30_000)

    # The tab is named after the sub-agent, not "New session". The leading
    # "● " working-indicator prefix is tolerated so a mid-turn child still
    # passes; the load-bearing part is the agent name, not the fallback.
    expect(page).to_have_title(re.compile(rf"^(?:● )?{re.escape(agent_name)}$"), timeout=30_000)
    assert "New session" not in page.title()


_SUBAGENT_ROW = '[data-testid="subagent-row"]'

# The task-derived label the background generator would produce, and the
# structured spawn key it has to win over. Distinct strings so neither can
# satisfy the other's assertion.
_TASK_SUMMARY = "Investigate auth token refresh"
_STRUCTURED_NAME = "researcher-1"


@pytest.fixture
def labelled_subagent(
    seeded_session: tuple[str, str],
) -> Iterator[tuple[str, str]]:
    """Seed a child carrying BOTH a structured title and a task_summary.

    The child's title is the structured spawn-or-continue key
    (``"researcher:researcher-1"``), from which the UI derives
    ``session_name``; ``task_summary`` is seeded straight into the store
    because the background title coordinator is its only writer. Having
    both present is the point — it is what makes the label chain's
    preference observable.

    :param seeded_session: ``(base_url, parent_id)`` from the shared fixture.
    :returns: ``(base_url, parent_id)`` with one labelled child attached.
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
            "title": f"researcher:{_STRUCTURED_NAME}",
        },
        timeout=30.0,
    )
    child.raise_for_status()
    child_id = child.json()["id"]
    set_session_task_summary(child_id, _TASK_SUMMARY)
    try:
        yield (base_url, parent_id)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


def test_subagent_rail_prefers_task_summary_over_structured_name(
    page: Page,
    labelled_subagent: tuple[str, str],
) -> None:
    """The rail labels a sub-agent by its task, not its spawn key.

    Sub-agents are named ``"{agent}-{ordinal}"`` so the parent has a stable
    key to spawn-or-continue against, but that key says nothing about what
    the sub-agent is doing. ``task_summary`` carries the task-derived label,
    and both the list and graph views resolve it ahead of the structured
    name — otherwise the rail reads as a row of ``researcher-1``,
    ``researcher-2`` with no way to tell them apart.
    """
    base_url, parent_id = labelled_subagent

    page.goto(f"{base_url}/c/{parent_id}")

    # Scope to the desktop "Workspace" rail so lookups don't also match the
    # hidden mobile drawer, which mirrors the same testids.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()

    row = rail.locator(_SUBAGENT_ROW).first
    expect(row).to_be_visible(timeout=30_000)

    # List view: the task label wins, and the structured key it beat is
    # nowhere in the row (a fallback regression would surface it here).
    expect(row).to_contain_text(_TASK_SUMMARY, timeout=30_000)
    assert _STRUCTURED_NAME not in row.inner_text()

    # Graph view resolves the label through its own layout code, so it gets
    # its own assertion rather than riding on the list's.
    rail.locator('[data-testid="view-mode-graph"]').click()
    expect(rail).to_contain_text(_TASK_SUMMARY, timeout=30_000)
