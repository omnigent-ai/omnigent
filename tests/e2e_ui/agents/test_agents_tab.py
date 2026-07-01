"""UI journey: the right-rail Agents tab and its agent-count badge.

The Agents tab is an unconditional rail tab (AppShell: "the tab is
ALWAYS shown" — its panel always lists at least the "main" row, so an
empty tree is a one-entry list, not a dead end). Its count badge is the
real signal of how many agents are in the session tree:
``agentCount = childSessions.length + 1`` (WorkspacePanel.tsx), i.e. it
reads ``1`` for a lone agent and grows as sub-agents spawn.

This test pins the lone-agent baseline: the tab is present, its badge
reads ``1``, the panel shows the single "main" row, and there are no
sub-agent rows. The companion ``test_subagent_navigation.py`` covers the
multi-agent case where the badge and rows grow once sub-agents exist.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_SUBAGENT_MAIN_ROW = '[data-testid="subagent-main-row"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'


def _create_child_session(
    page: Page,
    base_url: str,
    agent_id: str,
    parent_id: str,
    sub_agent_name: str,
) -> str:
    """Create a child session without spending an LLM turn."""
    child = page.request.post(
        f"{base_url}/v1/sessions",
        data={
            "agent_id": agent_id,
            "parent_session_id": parent_id,
            "sub_agent_name": sub_agent_name,
        },
        timeout=30_000,
    )
    assert child.ok, f"child session create failed: {child.status} {child.status_text}"
    return str(child.json()["id"])


def test_agents_tab_lists_lone_agent(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A single-agent session shows the Agents tab with a count of 1.

    No message is sent — the tab and its baseline count are rail state,
    not a function of any turn — so this stays a fast, LLM-free check.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # Scope every lookup to the desktop "Workspace" rail so it never
    # matches the hidden mobile drawer that mirrors the same testids.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
    expect(agents_tab).to_be_visible(timeout=30_000)
    # Badge starts at 1 for a lone agent (childSessions.length + 1).
    expect(agents_tab).to_contain_text("1")

    agents_tab.click()
    # The panel always renders the "main" row linking back to the root,
    # and a lone agent has no sub-agent rows beneath it.
    expect(rail.locator(_SUBAGENT_MAIN_ROW)).to_be_visible(timeout=30_000)
    expect(rail.locator(_SUBAGENT_ROW)).to_have_count(0)


def test_agents_badge_counts_nested_agents(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The closed Agents-tab badge counts nested coordinator children up to 7."""
    base_url, root_id = seeded_session
    parent = page.request.get(f"{base_url}/v1/sessions/{root_id}", timeout=10_000)
    assert parent.ok, f"root session read failed: {parent.status} {parent.status_text}"
    agent_id = str(parent.json()["agent_id"])
    created_ids: list[str] = []

    try:
        coordinator_ids = [
            _create_child_session(page, base_url, agent_id, root_id, "coordinator_alpha"),
            _create_child_session(page, base_url, agent_id, root_id, "coordinator_beta"),
        ]
        created_ids.extend(coordinator_ids)
        worker_ids = [
            _create_child_session(
                page, base_url, agent_id, coordinator_ids[0], "platform_preflight"
            ),
            _create_child_session(
                page, base_url, agent_id, coordinator_ids[0], "platform_preflight_retry"
            ),
            _create_child_session(page, base_url, agent_id, coordinator_ids[1], "story_worker"),
            _create_child_session(
                page, base_url, agent_id, coordinator_ids[1], "story_worker_retry"
            ),
        ]
        created_ids.extend(worker_ids)

        page.goto(f"{base_url}/c/{root_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))

        # Main + two coordinators + four workers nested under the coordinators.
        expect(agents_tab).to_contain_text("7", timeout=30_000)
    finally:
        for child_id in reversed(created_ids):
            page.request.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10_000)
