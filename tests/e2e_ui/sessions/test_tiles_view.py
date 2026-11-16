"""E2E: the Tile view (``tiles_view``) and its Single agent ⇄ Tiles nav toggle.

Covers what component tests cannot: the Tile view shares the sidebar's live
session cache (a separate query on its key broke the sidebar), the nav toggle
carries the active agent across routes, Cards hand off to Focus, and the
Settings preference hides the nav entry. No LLM turn is involved.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def _tile(page: Page, session_id: str):
    return page.locator(f'[data-testid="agent-tile"][data-session-id="{session_id}"]')


def test_nav_toggle_opens_tiles_and_carries_the_active_agent(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Single agent → Tiles keeps the session active; clicking a tile switches it."""
    base_url, session_a, session_b = seeded_session_pair
    page.goto(f"{base_url}/c/{session_a}")
    toggle = page.get_by_test_id("tiles-toggle-nav")
    expect(toggle).to_have_text("Tiles", timeout=30_000)

    toggle.click()
    expect(page).to_have_url(f"{base_url}/tiles?active={session_a}")
    expect(_tile(page, session_a)).to_have_attribute("data-active", "true", timeout=30_000)
    expect(_tile(page, session_b)).to_be_visible()
    # The Tile view reads the sidebar's rows; the sidebar must still load.
    expect(page.get_by_text("Some sessions could not be loaded")).to_have_count(0)

    # Click the tile's body, not a control, to make it the active agent.
    _tile(page, session_b).locator("header strong").click()
    expect(_tile(page, session_b)).to_have_attribute("data-active", "true")
    expect(page).to_have_url(f"{base_url}/tiles?active={session_b}")

    expect(toggle).to_have_text("Single agent")
    toggle.click()
    expect(page).to_have_url(f"{base_url}/c/{session_b}")


def test_cards_open_the_picked_agent_in_focus(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Picking a card switches to Focus with that agent active."""
    base_url, session_a, _session_b = seeded_session_pair
    page.goto(f"{base_url}/tiles?view=cards")
    card = _tile(page, session_a)
    expect(card).to_be_visible(timeout=30_000)

    card.click()
    expect(page).to_have_url(f"{base_url}/tiles?view=focus&active={session_a}")
    expect(page.get_by_role("radio", name="Focus")).to_have_attribute("aria-checked", "true")
    expect(_tile(page, session_a)).to_have_attribute("data-active", "true")


def test_settings_can_hide_tiles_from_the_sidebar(page: Page, live_server: str) -> None:
    """Settings → General → Tiles removes and restores the nav toggle."""
    page.goto(f"{live_server}/settings/general")
    show_in_sidebar = page.get_by_test_id("tiles-show-in-sidebar-toggle")
    expect(show_in_sidebar).to_have_attribute("aria-checked", "true", timeout=30_000)

    show_in_sidebar.click()
    expect(show_in_sidebar).to_have_attribute("aria-checked", "false")
    page.goto(f"{live_server}/")
    expect(page.get_by_test_id("tiles-toggle-nav")).to_have_count(0, timeout=30_000)

    page.goto(f"{live_server}/settings/general")
    page.get_by_test_id("tiles-show-in-sidebar-toggle").click()
    page.goto(f"{live_server}/")
    expect(page.get_by_test_id("tiles-toggle-nav")).to_be_visible(timeout=30_000)
