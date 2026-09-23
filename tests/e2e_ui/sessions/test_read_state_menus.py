"""Browser e2e for read-state actions in session menus.

Read/unread must be actionable both ways and in bulk:

- A session row's kebab offers "Mark as unread" on a read row and
  "Mark as read" on an unread row, so no row state leaves the menu
  without a read-state action.
- The bulk-selection bar can mark the selected sessions read/unread.
"""

from __future__ import annotations

import re
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _row(page: Page, title: str) -> Locator:
    """Locate the sidebar row (``<li>``) by its titled link.

    Substring name matching (not ``exact``): an unread row's link carries an
    sr-only " (unread)" suffix in its accessible name, and in selection mode
    every row's href collapses to "#", so the unique per-test title is the
    only stable handle across both states.
    """
    return page.locator("li").filter(has=page.get_by_role("link", name=title))


def _unread_dot(row: Locator) -> Locator:
    """Locate the row's unread (pink) dot — the unseen session-state badge."""
    return row.locator('[data-testid="session-state-badge"][data-state="unseen"]')


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Give a session a unique title via ``PATCH /v1/sessions/{id}``."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _open_row_kebab(row: Locator) -> None:
    """Hover the row (the kebab is hover-revealed) and open its menu."""
    row.hover()
    row.get_by_test_id("conversation-actions").click()


def _mark_unread_via_kebab(page: Page, row: Locator) -> None:
    _open_row_kebab(row)
    page.get_by_test_id("mark-unread-conversation").click()
    expect(_unread_dot(row)).to_be_visible()


def _select_rows(page: Page, rows: list[Locator]) -> None:
    """Enter sessions-scope selection mode and select *rows* in order."""
    page.get_by_test_id("toggle-selection-mode").click()
    expect(page.get_by_text("0 selected")).to_be_visible()
    for count, row in enumerate(rows, start=1):
        row.locator("a").click()
        expect(page.get_by_text(f"{count} selected")).to_be_visible()


def _exit_selection_mode(page: Page) -> None:
    """Leave selection mode, tolerating a bulk action that already exited it."""
    exit_btn = page.get_by_role("button", name="Exit selection mode")
    if exit_btn.count() > 0:
        exit_btn.click()
    expect(page.get_by_test_id("toggle-selection-mode")).to_have_attribute(
        "aria-label", "Select sessions"
    )


def test_unread_row_kebab_offers_mark_as_read(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An unread row's kebab offers "Mark as read", and it clears the dot.

    The kebab already offers "Mark as unread" on a read row; once the row is
    unread the menu must offer the reverse action rather than no read-state
    action at all.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-read-state-kebab-{uuid.uuid4().hex[:8]}"
    _set_title(base_url, session_id, title)

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, title)
    expect(row).to_be_visible()
    expect(_unread_dot(row)).to_have_count(0)

    _mark_unread_via_kebab(page, row)

    _open_row_kebab(row)
    expect(page.get_by_role("menu")).to_be_visible()

    mark_read = page.get_by_role("menuitem", name="Mark as read")
    expect(mark_read).to_be_visible()
    mark_read.click()
    expect(_unread_dot(row)).to_have_count(0)


def test_bulk_selection_marks_sessions_read_and_unread(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Bulk selection can mark the selected sessions read, and unread.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` for two
        pre-created runner-bound sessions.
    """
    base_url, session_a, session_b = seeded_session_pair
    suffix = uuid.uuid4().hex[:8]
    title_a = f"e2e-read-state-bulk-a-{suffix}"
    title_b = f"e2e-read-state-bulk-b-{suffix}"
    _set_title(base_url, session_a, title_a)
    _set_title(base_url, session_b, title_b)

    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/c/{session_a}")

    row_a = _row(page, title_a)
    row_b = _row(page, title_b)
    expect(row_a).to_be_visible()
    expect(row_b).to_be_visible()

    # Unread both rows first, so bulk mark-as-read has a visible effect.
    _mark_unread_via_kebab(page, row_a)
    _mark_unread_via_kebab(page, row_b)

    _select_rows(page, [row_a, row_b])
    mark_read = page.get_by_role("button", name=re.compile(r"mark .*as read", re.IGNORECASE))
    expect(mark_read).to_be_visible()
    mark_read.click()

    # The dot badge is hidden while selection mode swaps in checkboxes, so
    # leave selection mode before asserting on the rows' read state.
    _exit_selection_mode(page)
    expect(_unread_dot(row_a)).to_have_count(0)
    expect(_unread_dot(row_b)).to_have_count(0)

    _select_rows(page, [row_a, row_b])
    mark_unread = page.get_by_role("button", name=re.compile(r"mark .*as unread", re.IGNORECASE))
    expect(mark_unread).to_be_visible()
    mark_unread.click()

    _exit_selection_mode(page)
    expect(_unread_dot(row_a)).to_be_visible()
    expect(_unread_dot(row_b)).to_be_visible()
