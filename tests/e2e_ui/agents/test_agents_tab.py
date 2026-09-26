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

import json
import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_SUBAGENT_MAIN_ROW = '[data-testid="subagent-main-row"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'


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


def test_agents_graph_shows_root_branded_role_and_fallback_icons(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The real graph journey identifies every supported node category."""
    base_url, session_id = seeded_session
    bundle = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent/contents", timeout=10.0)
    bundle.raise_for_status()

    children: list[str] = []
    child_specs = [
        ("reviewer", "branded-child", {"omnigent.wrapper": "codex-native-ui"}),
        ("Explore", "role-child", None),
        ("general-purpose", "fallback-child", None),
    ]
    try:
        for tool, name, labels in child_specs:
            child = httpx.post(
                f"{base_url}/v1/sessions",
                data={
                    "metadata": json.dumps(
                        {
                            "parent_session_id": session_id,
                            "title": f"{tool}:{name}",
                        }
                    )
                },
                files={"bundle": ("agent.tar.gz", bundle.content, "application/gzip")},
                timeout=30.0,
            )
            child.raise_for_status()
            child_id = child.json()["session_id"]
            children.append(child_id)
            if labels is not None:
                patched = httpx.patch(
                    f"{base_url}/v1/sessions/{child_id}",
                    json={"labels": labels},
                    timeout=10.0,
                )
                patched.raise_for_status()

        page.goto(f"{base_url}/c/{session_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Agents")).click()
        rail.get_by_role("button", name="Graph view").click()

        nodes = {}
        expected_labels = ["hello_world", "branded-child", "role-child", "fallback-child"]
        for label in expected_labels:
            node = rail.locator(".react-flow__node").filter(has_text=label)
            expect(node).to_be_visible(timeout=30_000)
            nodes[label] = node
            icon = node.get_by_test_id("agent-node-icon")
            expect(icon).to_have_count(1)
            expect(icon).to_have_attribute("aria-hidden", "true")

        expect(
            nodes["branded-child"].get_by_test_id("agent-node-icon").locator("title")
        ).to_have_text("Codex")
        expect(
            nodes["role-child"].locator('[data-testid="agent-node-icon"].lucide-search')
        ).to_have_count(1)
        expect(
            nodes["hello_world"].locator('[data-testid="agent-node-icon"].lucide-bot')
        ).to_have_count(1)
        expect(nodes["fallback-child"].get_by_test_id("agent-node-icon")).to_have_attribute(
            "viewBox", "0 0 1024 1024"
        )
    finally:
        for child_id in children:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
