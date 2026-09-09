"""E2E: Settings → General background session-title preference.

The toggle persists the user's server-side preference through
``GET/PUT /v1/user-settings``. This covers the real browser and server round
trip without launching an LLM title-generation turn.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def _open_general_settings(page: Page, base_url: str):
    """Navigate to Settings → General and wait for the toggle to mount."""
    page.goto(f"{base_url}/settings/general")
    return page.get_by_test_id("background-session-titles-toggle")


def test_background_session_titles_defaults_on_and_persists(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The preference starts on, survives an opt-out, and can be restored."""
    base_url, _session_id = seeded_session
    toggle = _open_general_settings(page, base_url)
    expect(toggle).to_be_visible(timeout=30_000)
    expect(toggle).to_have_attribute("aria-checked", "true")

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")

    page.reload()
    toggle = _open_general_settings(page, base_url)
    expect(toggle).to_have_attribute("aria-checked", "false", timeout=30_000)

    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")

    page.reload()
    toggle = _open_general_settings(page, base_url)
    expect(toggle).to_have_attribute("aria-checked", "true", timeout=30_000)
