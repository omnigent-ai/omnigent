"""Browser e2e for "Mark as unread" from the sidebar.

The row kebab's "Mark as unread" item re-lights the row's unread dot
(``SessionStateBadge`` with ``data-state="unseen"``) and writes the
caller's read-state to the server via ``PUT /v1/sessions/{id}/read-state``.

Read-state is server-backed and per-user (no ``localStorage``), so this
guards the wiring the mocked unit tests can't:

- The PUT actually fires (and doesn't 4xx on wire drift), so the dot
  survives a full page reload — a fresh page load has no client state and
  re-seeds the dot only from ``GET /v1/sessions``'s ``viewer_unread``.
- The server persists it per-user: the list endpoint returns
  ``viewer_unread = true`` for the session, which is what another device
  would read.

A reload (not just an in-tab assertion) is the key signal: since there's
no ``localStorage`` fallback, a dot that reappears after reload proves the
round-trip through the server (the PUT persisted it and a fresh
``GET /v1/sessions`` re-seeded ``viewer_unread``), not a client-only cache
patch.
"""

from __future__ import annotations

from playwright.sync_api import Locator, Page, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _unread_dot(row: Locator) -> Locator:
    """Locate the row's unread (pink) dot — the unseen session-state badge."""
    return row.locator('[data-testid="session-state-badge"][data-state="unseen"]')


def test_mark_unread_lights_the_dot_and_persists_across_reload(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Marking a session unread lights the dot and survives a reload + server.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound (idle) session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()
    # The row starts seen — no unread dot.
    expect(_unread_dot(row)).to_have_count(0)

    # Open the row kebab and pick "Mark as unread". Hover first so the
    # desktop hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("mark-unread-conversation").click()

    # The dot lights immediately (optimistic mirror write), even though this
    # is the session you're currently viewing.
    expect(_unread_dot(row)).to_be_visible()

    # Reload: a fresh page has no client state, so the dot can only come back
    # from the server's per-user read-state (the PUT persisted it; a fresh
    # GET /v1/sessions re-seeds viewer_unread). A client-only patch would be
    # lost here — this is what another device would read too.
    page.reload()
    expect(_row(page, session_id)).to_be_visible()
    expect(_unread_dot(_row(page, session_id))).to_be_visible()
