"""UI journey: the right-rail Capabilities tab and its four sections.

The Capabilities tab is an unconditional rail tab (WorkspacePanel: it is
always registered, and its endpoint always resolves for a bound session),
rendering a read-only view of the agent's capabilities. Its panel is
structured as a panel-level "Only show usable" scope toggle followed by
four always-present sections — Skills, MCP servers, Local tools, and
Sub-agents — each of which renders its own empty state rather than
disappearing when the agent has nothing in that group.

This test pins that baseline for the lone hello_world agent: the tab is
present, clicking it renders the capabilities panel, the top-level scope
toggle is present, and all four section headers render. No message is
sent — the panel is rail state driven by the capabilities endpoint, not a
function of any turn — so this stays a fast, LLM-free check.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_CAPABILITIES_PANEL = '[data-testid="capabilities-panel"]'
_SECTION_TITLES = ("Skills", "MCP servers", "Local tools", "Sub-agents")


def test_capabilities_tab_renders_sections_and_scope_toggle(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Capabilities tab renders its scope toggle and four sections."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # Scope every lookup to the desktop "Workspace" rail so it never
    # matches the hidden mobile drawer that mirrors the same testids.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    capabilities_tab = rail.get_by_role("tab", name=re.compile("^Capabilities"))
    expect(capabilities_tab).to_be_visible(timeout=30_000)

    capabilities_tab.click()

    # The panel resolves once the capabilities endpoint warms; wait on the
    # panel container rather than any single section.
    panel = rail.locator(_CAPABILITIES_PANEL)
    expect(panel).to_be_visible(timeout=30_000)

    # The panel-level scope toggle (Radix Switch, role="switch") governs
    # the whole view and is present regardless of the agent's config.
    expect(panel.get_by_role("switch")).to_be_visible()

    # All four sections render their headers unconditionally — each owns an
    # empty state, so an agent with nothing in a group still shows the header.
    for title in _SECTION_TITLES:
        expect(panel.get_by_text(title, exact=True)).to_be_visible()
