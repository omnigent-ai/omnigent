"""Find Settings search's field and check that a picked row is revealed."""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect

SETTINGS_HIGHLIGHT = "data-settings-highlight"


def settings_search_field(page: Page) -> Locator:
    return page.get_by_role("combobox", name="Search settings")


def expect_settings_row_revealed(page: Page, row_id: str) -> None:
    """The row is on screen and highlighted, and the highlight then fades."""
    row = page.locator(f'[data-settings-item="{row_id}"]')
    expect(row).to_be_in_viewport()
    expect(row).to_have_attribute(SETTINGS_HIGHLIGHT, "")
    expect(row).not_to_have_attribute(SETTINGS_HIGHLIGHT, "", timeout=5_000)
