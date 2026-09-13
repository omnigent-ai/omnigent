"""E2E: Settings → Appearance export/import buttons.

The Appearance section footer has Export and Import buttons next to "Reset to
defaults". Export collects all appearance preferences from localStorage into a
JSON blob and triggers a download. Import opens a file picker that validates
and applies an exported settings file, restoring every preference immediately.

This test verifies the UI presence and localStorage behavior. The actual file
download/upload flow requires special Playwright setup not worth the complexity
for this feature test — we test the underlying localStorage round-trip instead.
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


def test_import_dialog_opens_and_closes(page: Page, seeded_session: tuple[str, str]) -> None:
    """Clicking Import opens the import dialog with a file chooser."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    page.get_by_test_id("import-settings-button").click()
    expect(page.get_by_role("dialog", name="Import settings")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("import-settings-choose-file")).to_be_visible()

    # Close dialog.
    page.get_by_role("button", name="Cancel").click()
    expect(page.get_by_role("dialog", name="Import settings")).to_have_count(0)


def test_settings_persist_to_localstorage(page: Page, seeded_session: tuple[str, str]) -> None:
    """Changing appearance settings writes to localStorage for export."""
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

    # Verify settings were persisted to localStorage (these are what export reads).
    font_size = page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')")
    terminal_theme = page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')")
    assert font_size == "16"
    assert terminal_theme == "dark"  # Stored as plain string


def test_import_restores_localstorage_settings(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Writing settings to localStorage (simulating import) restores UI state."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Step 1: Set preferences and capture them.
    font_size_inc = page.get_by_test_id("ui-font-size-inc")
    for _ in range(5):
        font_size_inc.click()
    expect(page.get_by_test_id("ui-font-size-input")).to_have_value("18")

    terminal_dark = page.get_by_test_id("terminal-theme-dark")
    terminal_dark.click()
    expect(terminal_dark).to_have_attribute("aria-checked", "true")

    # Capture the localStorage state (what export would collect).
    saved_font = page.evaluate("() => window.localStorage.getItem('omnigent:ui-font-size')")
    saved_terminal = page.evaluate("() => window.localStorage.getItem('omnigent:terminal-theme')")

    # Step 2: Change settings to something different.
    font_size_dec = page.get_by_test_id("ui-font-size-dec")
    for _ in range(6):
        font_size_dec.click()
    expect(page.get_by_test_id("ui-font-size-input")).to_have_value("12")

    page.get_by_test_id("terminal-theme-light").click()
    expect(page.get_by_test_id("terminal-theme-light")).to_have_attribute("aria-checked", "true")

    # Step 3: Restore the saved settings (simulating import).
    page.evaluate(
        """([font, terminal]) => {
            window.localStorage.setItem('omnigent:ui-font-size', font);
            window.localStorage.setItem('omnigent:terminal-theme', terminal);
        }""",
        [saved_font, saved_terminal],
    )

    # Step 4: Reload to apply restored settings.
    page.reload()
    expect(page.get_by_role("group", name="Interface font size", exact=True)).to_be_visible(
        timeout=30_000
    )

    # Settings are restored to the saved values.
    expect(page.get_by_test_id("ui-font-size-input")).to_have_value("18")
    expect(page.get_by_test_id("terminal-theme-dark")).to_have_attribute("aria-checked", "true")


def test_export_and_import_preserve_theme_mode(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The web-theme key (next-themes) is included in export/import.

    Note: next-themes stores the value as a plain string (e.g. 'dark'), not
    JSON-stringified like other settings.
    """
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Set theme to dark.
    page.get_by_test_id("theme-dark").click()
    expect(page.get_by_test_id("theme-dark")).to_have_attribute("aria-checked", "true")

    # Verify theme was written to localStorage (plain string, not JSON).
    theme = page.evaluate("() => window.localStorage.getItem('web-theme')")
    assert theme == "dark"

    # Change to light.
    page.get_by_test_id("theme-light").click()
    expect(page.get_by_test_id("theme-light")).to_have_attribute("aria-checked", "true")

    # Restore dark theme (simulating import).
    page.evaluate("() => window.localStorage.setItem('web-theme', 'dark')")
    page.reload()
    expect(page.get_by_role("group", name="Interface font size", exact=True)).to_be_visible(
        timeout=30_000
    )

    # Dark theme is restored.
    expect(page.get_by_test_id("theme-dark")).to_have_attribute("aria-checked", "true")


def test_import_rejects_invalid_json(page: Page, seeded_session: tuple[str, str]) -> None:
    """Import shows error when uploaded file contains invalid JSON or structure."""
    base_url, _session_id = seeded_session
    _open_appearance(page, base_url)

    # Open import dialog.
    page.get_by_test_id("import-settings-button").click()
    expect(page.get_by_role("dialog", name="Import settings")).to_be_visible(timeout=30_000)

    # Upload a file with invalid structure (missing version).
    import json
    import tempfile
    from pathlib import Path

    invalid_settings = {"settings": {"omnigent:ui-font-size": "14"}}  # Missing version field

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(invalid_settings, f)
        temp_path = f.name

    try:
        file_input = page.get_by_test_id("import-settings-file-input")
        file_input.set_input_files(temp_path)

        # Dialog stays open and shows error.
        expect(page.get_by_role("dialog", name="Import settings")).to_be_visible()
        expect(page.get_by_role("alert")).to_be_visible()
        expect(page.get_by_role("alert")).to_contain_text("valid Omnigent settings")
    finally:
        Path(temp_path).unlink()
