"""E2E: Settings -> Appearance controls the initial Agents panel view.

The preference is browser-local and seeds each Agents panel mount. Switching
views inside the panel is intentionally temporary and must not overwrite the
Appearance default. No LLM turn is needed.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

STORAGE_KEY = "omnigent:default-agents-view"


def _open_agents_panel(page: Page, base_url: str, session_id: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    expect(rail.get_by_role("button", name="Graph view")).to_be_visible(timeout=30_000)


def test_agents_view_default_persists_and_seeds_panel_mounts(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Graph persists through reloads while an in-panel List switch does not."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/settings/appearance")

    group = page.get_by_role("radiogroup", name="Default Agents view")
    expect(group).to_be_visible(timeout=30_000)
    graph_default = group.get_by_role("radio", name="Graph")
    graph_default.click()
    expect(graph_default).to_have_attribute("aria-checked", "true")
    assert page.evaluate(f"() => localStorage.getItem('{STORAGE_KEY}')") == "graph"

    _open_agents_panel(page, base_url, session_id)
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("button", name="Zoom in")).to_be_visible(timeout=30_000)

    rail.get_by_role("button", name="List view").click()
    expect(rail.get_by_test_id("subagent-main-row")).to_be_visible()
    assert page.evaluate(f"() => localStorage.getItem('{STORAGE_KEY}')") == "graph"

    page.reload()
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    expect(rail.get_by_role("button", name="Zoom in")).to_be_visible(timeout=30_000)
