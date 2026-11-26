"""UI e2e: Ctrl-N / Ctrl-P move through the ⌘K command palette like the arrows."""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect


def _palette_input(page: Page) -> Locator:
    return page.get_by_role("combobox", name="Command palette")


def _selected(page: Page) -> Locator:
    """The palette's highlighted row."""
    return page.get_by_role("dialog", name="Command palette").get_by_role("option", selected=True)


def _expect_selected(page: Page, label: str) -> None:
    expect(_selected(page)).to_have_attribute("data-value", label)


def test_ctrl_n_and_ctrl_p_move_through_the_command_palette(page: Page, live_server: str) -> None:
    page.goto(live_server)
    expect(page.get_by_test_id("sidebar-search-button")).to_be_visible(timeout=30_000)

    page.keyboard.press("ControlOrMeta+k")
    query = _palette_input(page)
    expect(query).to_be_focused()
    expect(_selected(page)).to_have_count(1)
    first = _selected(page).get_attribute("data-value")
    assert first

    page.keyboard.press("Control+n")
    expect(_selected(page)).not_to_have_attribute("data-value", first)
    second = _selected(page).get_attribute("data-value")
    assert second
    page.keyboard.press("ArrowUp")
    _expect_selected(page, first)
    page.keyboard.press("ArrowDown")
    _expect_selected(page, second)

    # Ctrl-P steps back, and neither key types into or leaves the palette.
    page.keyboard.press("Control+p")
    _expect_selected(page, first)
    expect(query).to_be_focused()
    expect(query).to_have_value("")
