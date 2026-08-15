"""E2E: Settings default Workspace tab controls a newly opened session."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

STORAGE_KEY = "omnigent:default-workspace-tab"


def test_default_workspace_tab_setting_opens_session_on_chosen_tab(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Choosing Agents in Appearance makes a fresh session land on Agents."""
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/settings/appearance")
    group = page.get_by_role("radiogroup", name="Default Workspace tab")
    expect(group).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("workspace-tab-default-files")).to_have_attribute(
        "aria-checked", "true"
    )
    assert page.evaluate(f"() => localStorage.getItem('{STORAGE_KEY}')") is None

    agents = page.get_by_test_id("workspace-tab-default-subagents")
    agents.click()
    expect(agents).to_have_attribute("aria-checked", "true")
    assert page.evaluate(f"() => localStorage.getItem('{STORAGE_KEY}')") == "subagents"

    page.goto(f"{base_url}/c/{session_id}")
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail).to_be_visible(timeout=60_000)
    agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
    expect(agents_tab).to_have_attribute("aria-selected", "true")
    expect(rail.get_by_role("list")).to_be_visible()
