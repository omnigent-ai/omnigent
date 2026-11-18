"""Entry points for the session's linked pull requests."""

import re

from playwright.sync_api import Page


def open_session_pull_requests(page: Page, *, mobile: bool) -> None:
    if mobile:
        page.get_by_role("button", name=re.compile(r"^(?:Conversation|Session) actions$")).click()
        page.get_by_role("menuitem", name="Pull Requests", exact=True).click()
    else:
        page.get_by_test_id("composer-pr-link").click()
