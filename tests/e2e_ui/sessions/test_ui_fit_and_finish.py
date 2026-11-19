"""E2E coverage for the shared Settings and sidebar layout geometry."""

from __future__ import annotations

import sys

from playwright.sync_api import Page, expect


def test_keyboard_shortcuts_use_grouped_settings_cards(page: Page, live_server: str) -> None:
    """Shortcut groups render as labeled, bordered Settings cards."""
    page.goto(f"{live_server}/settings/shortcuts")

    main = page.get_by_role("main")
    expect(main.get_by_role("heading", name="Keyboard shortcuts")).to_be_visible(timeout=30_000)
    general = main.get_by_role("heading", name="General", exact=True)
    expect(general).to_be_visible()
    card = general.locator("xpath=..").locator("ul")
    expect(card).to_be_visible()
    new_session = card.get_by_text("Start a new session", exact=True)
    expect(new_session).to_be_visible()
    expect(new_session.locator("xpath=..").locator("kbd")).to_have_text(
        ["⌘", "⇧", "O"] if sys.platform == "darwin" else ["Ctrl", "Shift", "O"]
    )
    expect(card.get_by_text("Show keyboard shortcuts", exact=True)).to_be_visible()

    geometry = card.evaluate(
        """element => {
          const style = getComputedStyle(element);
          return {
            borderWidth: style.borderTopWidth,
            borderRadius: style.borderRadius,
          };
        }"""
    )
    assert geometry["borderWidth"] == "1px"
    assert float(geometry["borderRadius"].removesuffix("px")) >= 10


def test_settings_list_cards_share_the_settings_card_radius(page: Page, live_server: str) -> None:
    """Custom agent and harness list cards use the same corner radius as Settings group cards."""
    corner_radius = "element => getComputedStyle(element).borderTopLeftRadius"
    page.goto(f"{live_server}/settings/general")
    group_card = page.locator('[data-slot="settings-group-card"]').first
    expect(group_card).to_be_visible(timeout=30_000)
    standard_radius = group_card.evaluate(corner_radius)

    page.get_by_test_id("settings-nav-custom-agents").click()
    agent_card = page.get_by_role("region", name="Built-in agents").get_by_role("link").first
    expect(agent_card).to_be_visible(timeout=30_000)
    agent_radius = agent_card.evaluate(corner_radius)

    page.get_by_test_id("settings-nav-harnesses").click()
    harness_card = page.locator('[data-testid^="harness-card-"]').first
    expect(harness_card).to_be_visible(timeout=30_000)
    harness_radius = harness_card.evaluate(corner_radius)

    assert {"custom agents": agent_radius, "harnesses": harness_radius} == {
        "custom agents": standard_radius,
        "harnesses": standard_radius,
    }


def test_sidebar_rows_and_section_titles_share_compact_height(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Primary navigation, section titles, and session rows stay 28px tall."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    new_session = page.get_by_test_id("new-chat-button")
    sessions_title = page.get_by_role("button", name="Sessions", exact=True)
    session_row = page.locator(f'[data-sidebar-session-id="{session_id}"] a')
    expect(session_row).to_be_visible(timeout=30_000)

    assert round(new_session.bounding_box()["height"]) == 28
    assert round(sessions_title.bounding_box()["height"]) == 28
    assert round(session_row.bounding_box()["height"]) == 28
