"""Browser e2e for the sidebar row right-click context menu.

Right-clicking a session row opens the same actions as the row kebab
(three-dots) — Share / Rename / Add-to-project / Stop / Archive / Delete —
positioned at the cursor. Both menus render from a single shared item body
(``ConversationMenuItems`` in ``Sidebar.tsx``), one under a Radix
``DropdownMenu`` (the kebab) and one under a Radix ``ContextMenu`` (this
right-click path).

This guards the wiring the mocked unit test can't exercise end-to-end:

- The native browser context menu is suppressed (``ContextMenuTrigger``
  preventDefaults the ``contextmenu`` event) and the app menu opens instead.
- Right-clicking does NOT navigate, and the same item handlers run — picking
  Rename here swaps the row for the inline edit field, exactly as the kebab's
  Rename does.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect


def _row_link(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row's clickable link for *session_id* by its href."""
    return page.locator(f'a[href="/c/{session_id}"]')


def test_right_click_opens_session_actions_menu(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Right-clicking a row opens the kebab actions and drives the same handlers.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")

    link = _row_link(page, session_id)
    expect(link).to_be_visible()

    # Right-click the row surface. Radix's ContextMenuTrigger preventDefaults
    # the native contextmenu event and opens the app menu at the cursor.
    link.click(button="right")

    # The context menu carries the full set of kebab actions — same testids,
    # proving it renders from the shared ConversationMenuItems body. (The kebab
    # DropdownMenu is closed, so these are the context menu's own items.)
    expect(page.get_by_test_id("share-conversation")).to_be_visible()
    expect(page.get_by_test_id("rename-conversation")).to_be_visible()
    expect(page.get_by_test_id("move-to-project")).to_be_visible()
    expect(page.get_by_test_id("archive-conversation")).to_be_visible()
    expect(page.get_by_test_id("delete-conversation")).to_be_visible()

    # Selecting Rename runs the same path as the kebab / double-click: the row
    # swaps for the inline edit field. Right-click did not navigate away.
    page.get_by_test_id("rename-conversation").click()
    expect(page.get_by_test_id("rename-conversation-input")).to_be_visible()
    expect(page).to_have_url(f"{base_url}/c/{session_id}")
