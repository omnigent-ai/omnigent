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
    """Clicking Reset → confirm resets every registry-owned preference to its default.

    Covers all three preferences on the declarative Appearance registry (UI font
    size, terminal theme, workspace panel default): the controls update live —
    without a reload — and the persisted keys are cleared.
    """
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    font_size_input = page.get_by_test_id("ui-font-size-input")
    font_size_inc = page.get_by_test_id("ui-font-size-inc")
    terminal_dark = page.get_by_test_id("terminal-theme-dark")
    workspace_collapsed = page.get_by_test_id("workspace-panel-default-collapsed")

    # Fresh context: the defaults are applied and nothing is persisted yet.
    expect(font_size_input).to_have_value("16")
    expect(page.get_by_test_id("terminal-theme-auto")).to_have_attribute("aria-checked", "true")
    expect(page.get_by_test_id("workspace-panel-default-open")).to_have_attribute(
        "aria-checked", "true"
    )
    stored_font_size = page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')")
    assert stored_font_size is None, "expected no persisted font size on a fresh load"

    # Change each migrated appearance preference away from its default.
    font_size_inc.click()
    font_size_inc.click()
    expect(font_size_input).to_have_value("18")
    terminal_dark.click()
    expect(page.get_by_test_id("terminal-theme-dark")).to_have_attribute("aria-checked", "true")
    workspace_collapsed.click()
    expect(workspace_collapsed).to_have_attribute("aria-checked", "true")

    # Confirm the changes were persisted.
    assert page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')") == "18"
    assert page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')") == "dark"
    assert (
        page.evaluate("() => window.localStorage.getItem('omnigent:default-workspace-panel')")
        == "collapsed"
    )

    # Reset, confirming through the dialog.
    page.get_by_test_id("reset-appearance-button").click()
    expect(page.get_by_role("dialog", name="Reset appearance?")).to_be_visible(timeout=30_000)
    page.get_by_test_id("reset-appearance-confirm").click()

    # Every choice is back to the product default — live, no reload.
    expect(font_size_input).to_have_value("16")
    expect(page.get_by_test_id("terminal-theme-auto")).to_have_attribute("aria-checked", "true")
    expect(page.get_by_test_id("workspace-panel-default-open")).to_have_attribute(
        "aria-checked", "true"
    )
    assert page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')") is None
    assert page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')") is None
    assert (
        page.evaluate("() => window.localStorage.getItem('omnigent:default-workspace-panel')")
        is None
    )
