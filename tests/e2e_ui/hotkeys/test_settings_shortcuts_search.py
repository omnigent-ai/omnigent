"""UI e2e: finding a shortcut on Settings' Keyboard shortcuts page.

The page's own field filters the rows by name, group or keys, and a Settings search pick for a
shortcut row reveals it even while the field hides it.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui._settings_search import expect_settings_row_revealed, settings_search_field


def test_keyboard_shortcuts_search_filters_the_table(page: Page, live_server: str) -> None:
    """The Shortcuts page's field filters its rows; a Settings search pick shows them all again."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{live_server}/settings/shortcuts")
    main = page.get_by_role("main")
    filter_field = main.get_by_role("searchbox", name="Search shortcuts")
    expect(filter_field).to_be_visible(timeout=30_000)
    general = main.get_by_role("heading", name="General", exact=True)
    expect(general).to_be_visible()

    filter_field.fill("model")
    rows = main.get_by_role("listitem")
    expect(rows).to_have_count(2)
    expect(rows.first).to_contain_text("Open model picker")
    expect(general).to_have_count(0)

    # Chords, no-match and Escape are covered by shortcutsFilter and SearchableShortcutsList tests.
    # A Settings search result for a row the filter hides still reveals it.
    settings_search_field(page).fill("Open command palette")
    expect(page.get_by_role("option").first).to_contain_text("Open command palette")
    page.keyboard.press("Enter")
    expect(filter_field).to_have_value("")
    expect_settings_row_revealed(page, "shortcut.palette.commands")

    # Every row the page lists is a search result, the composer's own keys included.
    settings_search_field(page).fill("Send message")
    expect(page.get_by_role("option").first).to_contain_text("Send message")
    page.keyboard.press("Enter")
    expect_settings_row_revealed(page, "shortcut.composer.send")
