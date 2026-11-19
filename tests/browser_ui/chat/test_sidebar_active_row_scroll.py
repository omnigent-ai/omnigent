"""Sidebar scrolls the active row only when it is not already fully visible."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.browser_ui.chat._geometry_helpers import open_landing
from tests.browser_ui.chat.session_contract import ChatSessionContract

_LIST = 'aside[aria-label="Conversations"] nav.overflow-y-auto'
_SIDEBAR_SESSIONS = 22
_BASE_TIMESTAMP = 1_704_067_200
# Seeded rows are one second apart; this sorts the active session below all of them.
_OLDEST_TIMESTAMP = 1_600_000_000


def _list_scroll_top(page: Page) -> float:
    return page.evaluate(f"() => document.querySelector('{_LIST}').scrollTop")


def _wait_for_list_scroll_to_settle(page: Page) -> None:
    """Return once the list has fired no scroll events for 300 ms."""
    page.evaluate(
        f"""() => new Promise((resolve) => {{
            const list = document.querySelector('{_LIST}');
            let timer = setTimeout(finish, 300);
            function finish() {{
                list.removeEventListener('scroll', restart);
                resolve();
            }}
            function restart() {{
                clearTimeout(timer);
                timer = setTimeout(finish, 300);
            }}
            list.addEventListener('scroll', restart);
        }})"""
    )


def _row_position(page: Page, session_id: str) -> dict[str, float]:
    return page.evaluate(
        f"""() => {{
            const list = document.querySelector('{_LIST}');
            const row = list.querySelector('li[data-sidebar-session-id="{session_id}"]');
            const listBox = list.getBoundingClientRect();
            const rowBox = row.getBoundingClientRect();
            return {{
                fullyVisible: rowBox.top >= listBox.top && rowBox.bottom <= listBox.bottom,
                gapToListBottom: listBox.bottom - rowBox.bottom,
                listOverflows: list.scrollHeight > list.clientHeight,
            }};
        }}"""
    )


def test_clicking_a_visible_row_leaves_the_list_in_place(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """A click on a row that is already fully visible does not scroll the list."""
    chat = chat_session_contract
    chat.seed_sidebar_sessions(
        [f"Click demo session {index:02d}" for index in range(1, _SIDEBAR_SESSIONS + 1)]
    )
    # Sorts below the 17 seeded rows above it, so it starts fully visible near the bottom edge.
    chat.update_session(updated_at=_BASE_TIMESTAMP - 17.5)
    open_landing(page, chat)
    _wait_for_list_scroll_to_settle(page)
    before = _row_position(page, chat.session_id)
    assert before["listOverflows"]
    assert before["fullyVisible"]
    assert before["gapToListBottom"] < 60
    scroll_top_before = _list_scroll_top(page)

    page.locator(f'li[data-sidebar-session-id="{chat.session_id}"] a').first.click()
    expect(page).to_have_url(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_visible()
    _wait_for_list_scroll_to_settle(page)

    assert _list_scroll_top(page) == scroll_top_before
    assert _row_position(page, chat.session_id)["fullyVisible"]


def test_offscreen_active_row_is_revealed_on_load(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    """An active row below the fold is scrolled into view when the page loads."""
    chat = chat_session_contract
    chat.seed_sidebar_sessions(
        [f"Reveal demo session {index:02d}" for index in range(1, _SIDEBAR_SESSIONS + 1)]
    )
    chat.update_session(updated_at=_OLDEST_TIMESTAMP)
    page.goto(chat.url)
    expect(page.get_by_label("Message the agent")).to_be_visible()
    page.wait_for_function(
        f"""() => {{
            const list = document.querySelector('{_LIST}');
            const row = list.querySelector('li[data-sidebar-session-id="{chat.session_id}"]');
            const listBox = list.getBoundingClientRect();
            const rowBox = row.getBoundingClientRect();
            return rowBox.top >= listBox.top && rowBox.bottom <= listBox.bottom;
        }}"""
    )
    _wait_for_list_scroll_to_settle(page)

    assert _list_scroll_top(page) > 0
    assert _row_position(page, chat.session_id)["fullyVisible"]
