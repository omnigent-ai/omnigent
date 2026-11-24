"""UI e2e: finding a shortcut on Settings' Keyboard shortcuts page.

With ``quick_pick_palette`` on, the page's own field filters the rows by name, group or keys,
and a Settings search pick for a shortcut row reveals it even while the field hides it. With the
flag off the page lists its rows as before, with no field.
"""

from __future__ import annotations

import sys

from playwright.sync_api import Page, expect

from tests.e2e_ui._quick_pick_palette import enable_quick_pick_palette

_HIGHLIGHT = "data-settings-highlight"


def _search_field(page: Page):
    return page.get_by_role("combobox", name="Search settings")


def _expect_revealed(page: Page, row_id: str) -> None:
    """The row is on screen and highlighted, and the highlight then fades."""
    row = page.locator(f'[data-settings-item="{row_id}"]')
    expect(row).to_be_in_viewport()
    expect(row).to_have_attribute(_HIGHLIGHT, "")
    expect(row).not_to_have_attribute(_HIGHLIGHT, "", timeout=5_000)


def test_keyboard_shortcuts_search_filters_the_table(page: Page, live_server: str) -> None:
    """The Shortcuts page's field filters its rows; a Settings search pick shows them all again."""
    page.set_viewport_size({"width": 1280, "height": 800})
    enable_quick_pick_palette(page)
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

    # A chord typed as keys finds the row it presses.
    filter_field.fill("cmd+k" if sys.platform == "darwin" else "ctrl+k")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Open command palette")
    # So does a chord only a browser tab shows.
    filter_field.fill("cmd+shift+o" if sys.platform == "darwin" else "ctrl+shift+o")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Start a new session")

    filter_field.fill("no such shortcut")
    expect(main.get_by_role("status")).to_have_text("No shortcuts match “no such shortcut”")
    filter_field.press("Escape")
    expect(filter_field).to_have_value("")
    expect(general).to_be_visible()

    # A Settings search result for a row the filter hides still reveals it.
    filter_field.fill("model")
    _search_field(page).fill("Open command palette")
    expect(page.get_by_role("option").first).to_contain_text("Open command palette")
    page.keyboard.press("Enter")
    expect(filter_field).to_have_value("")
    _expect_revealed(page, "shortcut.palette.commands")

    # Every row the page lists is a search result, the composer's own keys included.
    _search_field(page).fill("Send message")
    expect(page.get_by_role("option").first).to_contain_text("Send message")
    page.keyboard.press("Enter")
    _expect_revealed(page, "shortcut.composer.send")


def test_flag_off_keeps_the_shortcuts_page_without_a_field(page: Page, live_server: str) -> None:
    """Without ``quick_pick_palette`` the page lists its rows with no search field."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{live_server}/settings/shortcuts")
    main = page.get_by_role("main")
    expect(main.get_by_role("heading", name="General", exact=True)).to_be_visible(timeout=30_000)
    expect(main.get_by_role("searchbox", name="Search shortcuts")).to_have_count(0)
