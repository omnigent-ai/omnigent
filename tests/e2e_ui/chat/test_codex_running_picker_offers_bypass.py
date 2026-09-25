"""E2E: a running codex-native session's permission picker offers bypass.

The create-time composer's Codex permission dropdown offers "Bypass approvals
& sandbox" alongside the presets, but the running-session picker lists only
the runtime presets, so a user cannot see or pick bypass mid-session.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_running_codex_permission_picker_offers_bypass(
    page: Page,
    native_codex_mock_session: tuple[str, str],
) -> None:
    base_url, session_id = native_codex_mock_session
    page.goto(f"{base_url}/c/{session_id}")

    picker = page.get_by_test_id("composer-permission-chip")
    expect(picker).to_be_visible(timeout=30_000)
    expect(picker).to_be_enabled(timeout=30_000)
    picker.click()

    menu = page.get_by_test_id("composer-permission-menu")
    expect(menu).to_be_visible(timeout=5_000)
    # The codex runtime presets prove the right menu is open before probing bypass.
    expect(menu.get_by_test_id("composer-permission-option-full-access")).to_be_visible()
    expect(
        menu.get_by_role("menuitemradio", name="Bypass approvals & sandbox", exact=True)
    ).to_be_visible(timeout=5_000)
