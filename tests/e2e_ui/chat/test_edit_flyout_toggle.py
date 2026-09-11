"""E2E: the composer picker's Edit flyout toggles closed on a second click.

In an existing session, the composer's model/effort pill opens a picker whose
harness row carries an "Edit >" sub-trigger. Clicking it opens the Models /
Effort flyout; clicking it again must close just the flyout while the picker
itself stays open, instead of leaving the flyout pinned with no way to dismiss
it short of closing the whole picker.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native


def test_second_edit_click_closes_flyout(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A second click on the harness row's Edit sub-trigger closes the flyout.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native so the
        flyout carries the Models and Effort lists.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")

        gear = page.get_by_test_id("composer-config-gear")
        expect(gear).to_be_visible(timeout=15_000)
        gear.click()
        menu = page.get_by_test_id("composer-agent-menu")
        expect(menu).to_be_visible()

        # First click on the harness row's Edit sub-trigger opens the
        # Models / Effort flyout.
        edit = page.get_by_test_id("composer-agent-edit")
        edit.click()
        flyout = page.get_by_test_id("composer-agent-config-menu")
        expect(flyout).to_be_visible()
        expect(flyout.get_by_test_id("composer-agent-models")).to_be_visible()

        # A second click must toggle the flyout closed...
        edit.click()
        expect(flyout).not_to_be_visible()
        # ...while the picker itself stays open.
        expect(menu).to_be_visible()
    finally:
        page.unroute_all(behavior="ignoreErrors")


def test_escape_closes_only_the_flyout(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Escape dismisses just the flyout first; a second Escape closes the picker.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native so the
        flyout carries the Models and Effort lists.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")

        gear = page.get_by_test_id("composer-config-gear")
        expect(gear).to_be_visible(timeout=15_000)
        gear.click()
        menu = page.get_by_test_id("composer-agent-menu")
        expect(menu).to_be_visible()

        edit = page.get_by_test_id("composer-agent-edit")
        edit.click()
        flyout = page.get_by_test_id("composer-agent-config-menu")
        expect(flyout).to_be_visible()

        # Escape closes just the flyout, returning focus to the Edit row.
        page.keyboard.press("Escape")
        expect(flyout).not_to_be_visible()
        expect(menu).to_be_visible()
        expect(edit).to_be_focused()

        # A second Escape closes the whole picker.
        page.keyboard.press("Escape")
        expect(menu).not_to_be_visible()
    finally:
        page.unroute_all(behavior="ignoreErrors")
