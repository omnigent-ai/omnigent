"""Browser e2e coverage for scrolling the active session row in the sidebar.

Selecting a session the list already shows must leave the list where it is;
only an active row the list has off-screen (for example a session opened by
URL) is scrolled into view. jsdom cannot measure the scroll geometry, so this
checks it in a real browser against the live server.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle

_LIST_SELECTOR = 'aside[aria-label="Conversations"] nav.overflow-y-auto'

# Enough 28px rows that the list overflows a 720px-tall desktop window.
_SESSION_COUNT = 30

_ROW_FULLY_VISIBLE_JS = """([listSelector, href]) => {
    const list = document.querySelector(listSelector);
    const link = list?.querySelector(`a[href="${href}"]`);
    if (!link) return false;
    const box = link.getBoundingClientRect();
    const listBox = list.getBoundingClientRect();
    return box.top >= listBox.top && box.bottom <= listBox.bottom;
}"""


@pytest.fixture(scope="module")
def scrolling_sessions(live_server: str) -> Iterator[tuple[str, str, list[str]]]:
    """Seed enough titled sessions that the sidebar list scrolls."""
    title_prefix = f"Scroll check {uuid.uuid4().hex[:6]}"
    bundle = _build_hello_world_bundle()
    session_ids: list[str] = []
    try:
        for index in range(_SESSION_COUNT):
            response = post_session_bundle(
                httpx.post, f"{live_server}/v1/sessions", bundle, timeout=30.0
            )
            response.raise_for_status()
            session_id = response.json()["session_id"]
            session_ids.append(session_id)
            httpx.patch(
                f"{live_server}/v1/sessions/{session_id}",
                json={"title": f"{title_prefix} {index:02d}"},
                timeout=10.0,
            ).raise_for_status()
        yield live_server, title_prefix, session_ids
    finally:
        for session_id in session_ids:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def _open_session(page: Page, base_url: str, session_id: str) -> None:
    """Load *session_id* and wait for its sidebar row and the list's first scroll."""
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(f'{_LIST_SELECTOR} a[href="/c/{session_id}"]')).to_be_visible(
        timeout=30_000
    )
    page.wait_for_timeout(1_000)


def _list_scroll_top(page: Page) -> float:
    return page.locator(_LIST_SELECTOR).evaluate("list => list.scrollTop")


def _session_ids_by_visibility(page: Page, title_prefix: str) -> dict[str, list[str]]:
    """Split seeded rows into those the list shows fully and those it has off-screen."""
    return page.locator(_LIST_SELECTOR).evaluate(
        """(list, titlePrefix) => {
            const listBox = list.getBoundingClientRect();
            const rows = { visible: [], offscreen: [] };
            for (const link of list.querySelectorAll('a[href^="/c/"]')) {
                if (!link.textContent.includes(titlePrefix)) continue;
                const box = link.getBoundingClientRect();
                const id = link.getAttribute("href").slice("/c/".length);
                if (box.top >= listBox.top && box.bottom <= listBox.bottom) rows.visible.push(id);
                else if (box.top >= listBox.bottom) rows.offscreen.push(id);
            }
            return rows;
        }""",
        title_prefix,
    )


def _row_fully_visible(page: Page, session_id: str) -> bool:
    return page.evaluate(_ROW_FULLY_VISIBLE_JS, [_LIST_SELECTOR, f"/c/{session_id}"])


def test_clicking_a_visible_session_keeps_the_list_in_place(
    request: pytest.FixtureRequest,
    scrolling_sessions: tuple[str, str, list[str]],
) -> None:
    """Selecting a fully visible row near the list's bottom edge does not recenter it."""
    base_url, title_prefix, session_ids = scrolling_sessions
    page: Page = request.getfixturevalue("page")
    _open_session(page, base_url, session_ids[-1])

    list_box = page.locator(_LIST_SELECTOR).bounding_box()
    assert list_box is not None
    rows = _session_ids_by_visibility(page, title_prefix)
    assert rows["offscreen"], "the seeded list must overflow for this check"
    target_id = rows["visible"][-1]
    target = page.locator(f'{_LIST_SELECTOR} a[href="/c/{target_id}"]')
    target_box = target.bounding_box()
    assert target_box is not None
    assert target_box["y"] > list_box["y"] + list_box["height"] / 2

    scroll_top_before = _list_scroll_top(page)
    target.click()
    expect(page).to_have_url(re.compile(rf"/c/{target_id}$"))
    expect(target).to_have_class(re.compile(r"bg-\[var\(--sidebar-active\)\]"))

    # Sample past a smooth scroll's duration so a late recenter is still caught.
    for _ in range(6):
        page.wait_for_timeout(250)
        assert _list_scroll_top(page) == scroll_top_before
    assert _row_fully_visible(page, target_id)


def test_opening_an_offscreen_session_scrolls_its_row_into_view(
    request: pytest.FixtureRequest,
    scrolling_sessions: tuple[str, str, list[str]],
) -> None:
    """A session opened by URL whose row starts below the fold is brought into view."""
    base_url, title_prefix, session_ids = scrolling_sessions
    page: Page = request.getfixturevalue("page")
    _open_session(page, base_url, session_ids[-1])
    rows = _session_ids_by_visibility(page, title_prefix)
    assert rows["offscreen"], "the seeded list must overflow for this check"
    target_id = rows["offscreen"][-1]

    page.goto(f"{base_url}/c/{target_id}")
    page.wait_for_function(
        _ROW_FULLY_VISIBLE_JS, arg=[_LIST_SELECTOR, f"/c/{target_id}"], timeout=10_000
    )
    assert _list_scroll_top(page) > 0
