"""E2E: Settings default Workspace tab controls a newly opened session."""

from __future__ import annotations

import json
import re

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

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


def test_changed_default_applies_to_visited_session_after_reload(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A changed default replaces an old selection; later manual choices still persist."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    rail = page.get_by_role("complementary", name="Workspace")
    files_tab = rail.get_by_role("tab", name="Files", exact=True)
    expect(files_tab).to_have_attribute("aria-selected", "true", timeout=60_000)

    page.goto(f"{base_url}/settings/appearance")
    group = page.get_by_role("radiogroup", name="Default Workspace tab")
    options = group.get_by_role("radio")
    expect(options).to_have_count(4)
    for index, label in enumerate(["Files", "Changes", "GitHub", "Agents"]):
        expect(options.nth(index)).to_have_accessible_name(label)
    github_default = page.get_by_test_id("workspace-tab-default-github")
    github_default.click()
    page.reload()
    expect(github_default).to_have_attribute("aria-checked", "true", timeout=30_000)

    page.goto(f"{base_url}/c/{session_id}")
    github_tab = rail.get_by_role("tab", name="GitHub", exact=True)
    expect(github_tab).to_have_attribute("aria-selected", "true", timeout=60_000)
    page.reload()
    expect(github_tab).to_have_attribute("aria-selected", "true", timeout=60_000)

    files_tab.click()
    page.reload()
    expect(files_tab).to_have_attribute("aria-selected", "true", timeout=60_000)
    github_tab.click()
    page.reload()
    expect(github_tab).to_have_attribute("aria-selected", "true", timeout=60_000)


def test_agents_tab_survives_return_to_unvisited_root(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """A main-agent click keeps Agents selected when the root has no saved tab."""
    base_url, root_id = seeded_session
    child_response = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"parent_session_id": root_id})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    child_response.raise_for_status()
    child_id = child_response.json()["session_id"]
    try:
        page.goto(f"{base_url}/c/{child_id}")
        rail = page.get_by_role("complementary", name="Workspace")
        expect(rail).to_be_visible(timeout=60_000)
        agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
        agents_tab.click()
        expect(agents_tab).to_have_attribute("aria-selected", "true")
        main_row = rail.get_by_test_id("subagent-main-row")
        expect(main_row).to_have_attribute("href", f"/c/{root_id}")

        main_row.click()

        expect(page).to_have_url(f"{base_url}/c/{root_id}")
        expect(agents_tab).to_have_attribute("aria-selected", "true")
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0).raise_for_status()
