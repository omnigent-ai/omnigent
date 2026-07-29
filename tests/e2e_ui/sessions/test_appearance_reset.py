"""E2E: the Settings → Appearance reset button and the removed sidebar font size card.

The sidebar-only font size card was removed; users should scale the entire
interface with the "Interface font size" control instead. A "Reset to defaults"
button at the bottom of the Appearance section opens a confirmation dialog and,
on confirm, restores every appearance choice to its product default.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def _open_appearance(page: Page, base_url: str) -> None:
    """Open Settings → Appearance and wait for the interface font size group."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("group", name="Interface font size", exact=True)).to_be_visible(
        timeout=30_000
    )


def test_sidebar_font_size_card_is_removed(page: Page, seeded_session: tuple[str, str]) -> None:
    """The dedicated Sidebar font size card is no longer rendered."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    expect(page.get_by_role("group", name="Sidebar settings", exact=True)).to_have_count(0)
    expect(page.get_by_test_id("sidebar-font-size-input")).to_have_count(0)


def test_appearance_reset_restores_defaults(page: Page, seeded_session: tuple[str, str]) -> None:
    """Clicking Reset → confirm resets UI font size and terminal theme back to defaults."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    font_size_input = page.get_by_test_id("ui-font-size-input")
    font_size_inc = page.get_by_test_id("ui-font-size-inc")
    terminal_dark = page.get_by_test_id("terminal-theme-dark")

    # Fresh context: the defaults are applied and nothing is persisted yet.
    expect(font_size_input).to_have_value("16")
    expect(page.get_by_test_id("terminal-theme-auto")).to_have_attribute("aria-checked", "true")
    stored_font_size = page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')")
    assert stored_font_size is None, "expected no persisted font size on a fresh load"

    # Change two unrelated appearance preferences away from their defaults.
    font_size_inc.click()
    font_size_inc.click()
    expect(font_size_input).to_have_value("18")
    terminal_dark.click()
    expect(page.get_by_test_id("terminal-theme-dark")).to_have_attribute("aria-checked", "true")

    # Confirm both changes were persisted.
    assert page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')") == "18"
    assert page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')") == "dark"

    # Reset, confirming through the dialog.
    page.get_by_test_id("reset-appearance-button").click()
    expect(page.get_by_role("dialog", name="Reset appearance?")).to_be_visible(timeout=30_000)
    page.get_by_test_id("reset-appearance-confirm").click()

    # Both choices are back to the product defaults.
    expect(font_size_input).to_have_value("16")
    expect(page.get_by_test_id("terminal-theme-auto")).to_have_attribute("aria-checked", "true")
    assert page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')") is None
    assert page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')") is None
