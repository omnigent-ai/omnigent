"""E2E: leaving Settings returns to the conversation you came from.

Settings renders into the shared ``AppShell`` outlet under ``/settings`` — a
URL that carries no conversation id. The "Back to Omnigent" link in the
settings sidebar (``SettingsSidebarBody`` in ``shell/settingsNav.tsx``) used to
be hardcoded to ``/``, so leaving settings always dropped the user on the home
landing page instead of the conversation they were viewing.

The Sidebar stays mounted across the transition into settings and now records
the last non-settings location (via ``useTrackSettingsReturn``); the back link
points at it. This test drives the real in-app flow — open a conversation, open
Settings from the sidebar, click Back — and asserts the URL returns to
``/c/<session_id>`` rather than ``/``.

No LLM turn is involved.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_settings_back_returns_to_conversation(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Conversation → Settings → Back lands back on the conversation, not home.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page).to_have_url(f"{base_url}/c/{session_id}")

    # Enter Settings from the sidebar footer control. The conversations sidebar
    # stays mounted and swaps its body to the settings section nav.
    page.get_by_test_id("settings-button").click()
    page.wait_for_url("**/settings**", timeout=30_000)

    # The settings section nav renders in place of the conversation list.
    back = page.get_by_role("link", name="Back to Omnigent")
    expect(back).to_be_visible(timeout=30_000)

    # Back returns to the conversation we came from — the fix. A regression to
    # the hardcoded "/" would land on the home landing page instead.
    back.click()
    expect(page).to_have_url(f"{base_url}/c/{session_id}", timeout=30_000)
