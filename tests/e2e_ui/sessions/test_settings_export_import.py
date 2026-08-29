"""E2E: Settings → Appearance export/import buttons.

The Appearance section footer has Export and Import buttons next to "Reset to
defaults". Export collects all appearance preferences from localStorage into a
JSON blob and triggers a download. Import opens a file picker that validates
and applies an exported settings file, restoring every preference immediately.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def _open_appearance(page: Page, base_url: str) -> None:
    """Open Settings → Appearance and wait for the interface font size group."""
    page.goto(f"{base_url}/settings/appearance")
    expect(page.get_by_role("group", name="Interface font size", exact=True)).to_be_visible(
        timeout=30_000
    )


def test_export_import_buttons_are_visible(page: Page, seeded_session: tuple[str, str]) -> None:
    """The Export and Import buttons render next to Reset to defaults."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    expect(page.get_by_test_id("export-settings-button")).to_be_visible()
    expect(page.get_by_test_id("import-settings-button")).to_be_visible()
    expect(page.get_by_test_id("reset-appearance-button")).to_be_visible()


def test_export_collects_settings_from_localstorage(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Export reads all appearance preferences from localStorage."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Change several appearance preferences away from defaults.
    font_size_inc = page.get_by_test_id("ui-font-size-inc")
    for _ in range(3):
        font_size_inc.click()
    expect(page.get_by_test_id("ui-font-size-input")).to_have_value("16")

    terminal_dark = page.get_by_test_id("terminal-theme-dark")
    terminal_dark.click()
    expect(terminal_dark).to_have_attribute("aria-checked", "true")

    # Verify settings were persisted to localStorage.
    font_size = page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')")
    terminal_theme = page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')")
    assert font_size == "16"
    assert terminal_theme == "dark"

    # Simulate export by calling collectSettings() directly (download behavior
    # requires special Playwright setup that's not worth it for this test).
    exported = page.evaluate("""() => {
        const { collectSettings } = require('@/lib/settingsPortability');
        return collectSettings();
    }""")

    assert exported is not None
    assert exported["version"] == 1
    assert exported["settings"]["omnigent:ui-font-size"] == "16"
    assert exported["settings"]["omnigent:terminal-theme"] == "dark"


def test_import_restores_settings_to_localstorage(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Import applies an exported settings file and updates the UI immediately."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    font_size_input = page.get_by_test_id("ui-font-size-input")
    font_size_inc = page.get_by_test_id("ui-font-size-inc")
    terminal_dark = page.get_by_test_id("terminal-theme-dark")

    # Step 1: Set initial preferences.
    for _ in range(5):
        font_size_inc.click()
    expect(font_size_input).to_have_value("18")
    terminal_dark.click()
    expect(terminal_dark).to_have_attribute("aria-checked", "true")

    # Step 2: Capture exported state.
    exported = page.evaluate("""() => {
        const { collectSettings } = require('@/lib/settingsPortability');
        return collectSettings();
    }""")
    assert exported["settings"]["omnigent:ui-font-size"] == "18"
    assert exported["settings"]["omnigent:terminal-theme"] == "dark"

    # Step 3: Change settings to something different.
    font_size_dec = page.get_by_test_id("ui-font-size-dec")
    for _ in range(6):
        font_size_dec.click()
    expect(font_size_input).to_have_value("12")
    page.get_by_test_id("terminal-theme-light").click()
    expect(page.get_by_test_id("terminal-theme-light")).to_have_attribute("aria-checked", "true")

    # Step 4: Import the saved settings via applyImportedSettings().
    # This simulates what happens when a valid file is uploaded.
    page.evaluate(
        """(exported) => {
        const { applyImportedSettings } = require('@/lib/settingsPortability');
        applyImportedSettings(exported);
    }""",
        exported,
    )

    # Step 5: Reload to verify settings were written to localStorage.
    page.reload()
    expect(page.get_by_role("group", name="Interface font size", exact=True)).to_be_visible(
        timeout=30_000
    )

    # Settings are restored to the exported values.
    expect(page.get_by_test_id("ui-font-size-input")).to_have_value("18")
    expect(page.get_by_test_id("terminal-theme-dark")).to_have_attribute("aria-checked", "true")


def test_import_dialog_opens_on_button_click(page: Page, seeded_session: tuple[str, str]) -> None:
    """Clicking Import opens the import dialog with a file chooser."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    page.get_by_test_id("import-settings-button").click()
    expect(page.get_by_role("dialog", name="Import settings")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("import-settings-choose-file")).to_be_visible()

    # Close dialog.
    page.get_by_role("button", name="Cancel").click()
    expect(page.get_by_role("dialog", name="Import settings")).to_have_count(0)


def test_import_validates_settings_structure(page: Page, seeded_session: tuple[str, str]) -> None:
    """Import rejects invalid settings files with a user-friendly error."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Invalid structure (missing version).
    invalid_settings = {"settings": {"omnigent:ui-font-size": "14"}}

    # Simulate file read and validation.
    result = page.evaluate(
        """(invalid) => {
        const blob = new Blob([JSON.stringify(invalid)], { type: 'application/json' });
        const file = new File([blob], 'test.json', { type: 'application/json' });
        const { readSettingsFile } = require('@/lib/settingsPortability');
        return readSettingsFile(file).catch(err => ({ error: err.message }));
    }""",
        invalid_settings,
    )

    assert "error" in result
    assert "valid Omnigent settings" in result["error"]
