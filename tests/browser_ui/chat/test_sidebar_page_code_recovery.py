"""Sidebar navigation while a page's code download is being recovered."""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, Route, expect

from tests.browser_ui.chat._geometry_helpers import open_landing
from tests.browser_ui.chat.session_contract import ChatSessionContract

_INBOX_PAGE_CODE = re.compile(r"/assets/InboxPage-[^/?]+\.js(?:\?.*)?$")
_REPORT_ONLINE = "Object.defineProperty(Navigator.prototype, 'onLine', { get: () => true })"


def test_sidebar_stays_visible_while_inbox_page_code_recovers(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A failed then slow Inbox download keeps the sidebar instead of the full-window loader."""
    chat = chat_session_contract
    # Offline sandboxes report navigator.onLine false, which disables reload recovery.
    page.add_init_script(_REPORT_ONLINE)
    open_landing(page, chat)
    attempts: list[Route] = []

    def inbox_page_code(route: Route) -> None:
        attempts.append(route)
        if len(attempts) == 1:
            route.abort("failed")

    page.route(_INBOX_PAGE_CODE, inbox_page_code)
    page.get_by_test_id("inbox-button").click()
    deadline = time.monotonic() + 15
    while len(attempts) < 2:
        assert time.monotonic() < deadline, "The Inbox page code was not requested again"
        page.wait_for_timeout(50)
    try:
        expect(page.get_by_test_id("inbox-button")).to_be_visible(timeout=5_000)
    finally:
        for route in attempts[1:]:
            route.continue_()
    expect(page.get_by_role("heading", name="Inbox", level=1)).to_be_visible()
