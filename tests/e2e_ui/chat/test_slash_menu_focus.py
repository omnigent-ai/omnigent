"""Slash suggestions follow the composer's input focus."""

from playwright.sync_api import Page, expect


def test_slash_menu_hides_when_composer_blurs(page: Page, seeded_session: tuple[str, str]) -> None:
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("/")
    menu_item = page.get_by_test_id("slash-menu-item-help")
    expect(menu_item).to_be_visible()

    composer.blur()
    expect(menu_item).not_to_be_visible()
