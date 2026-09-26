"""UI journey: the Agents rail renders an agent's declared spec icon.

Task 3 added an optional ``icon`` to the agent spec; Task 4 exposed it on the
agent payload. This test pins the web end of that chain: a session whose bound
agent declares an emoji ``icon`` shows that grapheme on the Agents-rail main
row (``SubagentsPanel`` → ``resolveAgentIcon``), instead of the generic bot
glyph. The same resolver backs the picker's ``AgentCard``, so a custom icon
reads consistently across both surfaces.

No message is sent — the icon is read from the bound agent object, not from any
turn — so this stays a fast, LLM-free check, mirroring
``test_agents_tab.py``'s lone-agent baseline.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_SUBAGENT_MAIN_ROW = '[data-testid="subagent-main-row"]'
_EXPECTED_EMOJI = "\U0001f98a"  # 🦊


def test_agents_tab_shows_declared_emoji_icon(
    page: Page,
    emoji_icon_session: tuple[str, str],
) -> None:
    """An agent that declares an emoji icon shows it on the Agents-rail row."""
    base_url, session_id = emoji_icon_session
    page.goto(f"{base_url}/c/{session_id}")

    # Scope every lookup to the desktop "Workspace" rail so it never matches
    # the hidden mobile drawer that mirrors the same testids.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
    expect(agents_tab).to_be_visible(timeout=30_000)
    agents_tab.click()

    # The main row renders the bound agent's declared emoji grapheme rather
    # than a brand/bot glyph.
    main_row = rail.locator(_SUBAGENT_MAIN_ROW)
    expect(main_row).to_be_visible(timeout=30_000)
    expect(main_row).to_contain_text(_EXPECTED_EMOJI, timeout=30_000)
