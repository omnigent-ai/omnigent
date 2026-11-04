"""Sidebar active-row scrolling, checked against the built SPA in a real browser.

jsdom cannot measure the list's scroll geometry, so these cases need Chromium."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import open_landing, open_live
from tests.browser_ui.chat.session_contract import ChatSessionContract, list_payload

_LIST = 'aside[aria-label="Conversations"] nav.overflow-y-auto'

_ROW_BOX_JS = """([listSelector, href]) => {
    const list = document.querySelector(listSelector);
    const link = list?.querySelector(`a[href="${href}"]`);
    if (!link) return null;
    const box = link.getBoundingClientRect();
    const listBox = list.getBoundingClientRect();
    return {
        top: box.top - listBox.top,
        bottom: box.bottom - listBox.top,
        listHeight: listBox.height,
        scrollTop: list.scrollTop,
    };
}"""

_ROW_FULLY_VISIBLE_JS = f"""(args) => {{
    const row = ({_ROW_BOX_JS})(args);
    return !!row && row.top >= 0 && row.bottom <= row.listHeight;
}}"""

# Scrolls the list as a user would so the row sits one row-height above its bottom edge.
_SCROLL_ROW_ABOVE_LIST_BOTTOM_JS = """([listSelector, href]) => {
    const list = document.querySelector(listSelector);
    const box = list.querySelector(`a[href="${href}"]`).getBoundingClientRect();
    const listBox = list.getBoundingClientRect();
    const delta = box.bottom + box.height - listBox.bottom;
    list.scrollTo({ top: list.scrollTop + delta, behavior: "instant" });
}"""


def _list_open_session_after(chat: ChatSessionContract, newer: int, older: int) -> None:
    """List *newer* seeded sessions above the open session and *older* below it."""
    chat.seed_sidebar_sessions([f"Newer session {index:02d}" for index in range(newer)])
    oldest_newer = chat.sidebar_sessions[-1]
    chat.update_session(
        created_at=oldest_newer["created_at"] - 1, updated_at=oldest_newer["updated_at"] - 1
    )
    chat.seed_sidebar_sessions([f"Older session {index:02d}" for index in range(older)])
    chat.contract.json(
        "/v1/sessions",
        lambda _request: list_payload(
            sorted(
                [chat.session_snapshot(), *chat.sidebar_sessions],
                key=lambda row: -row["updated_at"],
            )
        ),
    )


def _row_box(page: Page, href: str) -> dict:
    row = page.evaluate(_ROW_BOX_JS, [_LIST, href])
    assert row is not None, f"no sidebar row links to {href}"
    return row


def test_clicking_a_visible_session_keeps_the_list_in_place(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """Selecting a fully visible row near the list's bottom edge does not recenter it."""
    chat = chat_session_contract
    _list_open_session_after(chat, newer=24, older=6)
    open_landing(page, chat)
    href = f"/c/{chat.session_id}"
    expect(page.locator(f'{_LIST} a[href="{href}"]')).to_be_attached(timeout=15_000)

    page.evaluate(_SCROLL_ROW_ABOVE_LIST_BOTTOM_JS, [_LIST, href])
    row = _row_box(page, href)
    assert row["top"] >= row["listHeight"] / 2 and row["bottom"] <= row["listHeight"], row
    scroll_top_before = row["scrollTop"]

    target = page.locator(f'{_LIST} a[href="{href}"]')
    target.click()
    expect(page).to_have_url(chat.url)
    expect(target).to_have_class(re.compile(r"bg-\[var\(--sidebar-active\)\]"))

    # Sample past a smooth scroll's duration so a late recenter is still caught.
    for _ in range(6):
        page.wait_for_timeout(250)
        assert _row_box(page, href)["scrollTop"] == scroll_top_before
    assert page.evaluate(_ROW_FULLY_VISIBLE_JS, [_LIST, href])


def test_opening_an_offscreen_session_scrolls_its_row_into_view(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A session opened by URL whose row starts below the fold is brought into view."""
    chat = chat_session_contract
    _list_open_session_after(chat, newer=30, older=0)
    open_live(page, chat)
    href = f"/c/{chat.session_id}"

    page.wait_for_function(_ROW_FULLY_VISIBLE_JS, arg=[_LIST, href], timeout=10_000)
    assert _row_box(page, href)["scrollTop"] > 0
