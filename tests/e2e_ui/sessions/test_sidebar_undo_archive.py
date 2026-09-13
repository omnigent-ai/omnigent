"""Browser e2e for undoing a session archive from the sidebar.

Archiving a session pops a floating Undo pill (a sonner toast, bottom-right
like the old "View in Settings" pointer). Archiving again while it's still up
merges into the SAME pill and resets its countdown, so a burst of archives is
covered by one Undo that restores every session at once. The pill reads
"Archived N session(s)." — singular for one, plural for more — with a bold,
underlined **Undo** and a small "View in Settings" link.

This asserts the headline behaviour: two archives in quick succession collapse
into one pill whose Undo unarchives BOTH — durably, not just in the client
cache. Undo replays ``PATCH /v1/sessions/{id}`` with ``archived: false`` for the
whole batch, so the store rows flip back and the sidebar rows return.

Copy/pluralisation and batch-merge/reset mechanics are unit-tested in
``web/src/shell/archiveUndoToast.test.tsx``; this file proves the wiring across
a real archive → toast → unarchive round-trip in the browser.
"""

from __future__ import annotations

import time

import httpx
from playwright.sync_api import Locator, Page, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _archive_from_row(page: Page, session_id: str) -> None:
    """Open a sidebar row's kebab and click Archive."""
    row = _row(page, session_id)
    expect(row).to_be_visible()
    # Hover first so the desktop hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("archive-conversation").click()


def _archived_flag(base_url: str, session_id: str) -> bool | None:
    """Read the store's ``archived`` flag for *session_id* (None if missing)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    if resp.status_code != 200:
        return None
    return resp.json()["archived"]


def _wait_for_archived(base_url: str, session_id: str, want: bool) -> None:
    """Poll until the store row reports ``archived == want`` (or time out).

    The list refetch that repaints the sidebar can land marginally before the
    single-session snapshot reflects the write, so this polls rather than
    reading once.
    """
    deadline = time.monotonic() + 15.0
    seen: bool | None = None
    while time.monotonic() < deadline:
        seen = _archived_flag(base_url, session_id)
        if seen is want:
            return
        time.sleep(0.25)
    raise AssertionError(f"session {session_id} should report archived={want}, got {seen}")


def test_undo_restores_every_session_archived_in_succession(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """Two quick archives merge into one pill whose Undo restores both.

    Failure modes this catches:

    - Archiving fires no toast, or a separate toast per archive, so a burst
      can't be undone as one action.
    - The pill miscounts (e.g. always "1 session", or the literal
      "session(s)") instead of "Archived 2 sessions.".
    - Undo only restores the last-archived session, or only un-hides rows in
      the client cache while the store still reports ``archived: true`` (a
      reload would re-hide them).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` for two
        pre-created runner-bound sessions.
    """
    base_url, session_a, session_b = seeded_session_pair

    # Start on session A's chat surface, then archive both in succession. The
    # first archive (of the active session) redirects home; B is archived from
    # its sidebar row, which stays listed on "/".
    page.goto(f"{base_url}/c/{session_a}")
    _archive_from_row(page, session_a)
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_have_count(0)
    page.wait_for_url(f"{base_url}/", timeout=10_000)

    _archive_from_row(page, session_b)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_have_count(0)

    # One pill, covering both — merged rather than stacked, and pluralised.
    pill = page.get_by_test_id("archive-undo-toast")
    expect(pill).to_be_visible()
    expect(pill).to_contain_text("Archived 2 sessions.")

    _wait_for_archived(base_url, session_a, True)
    _wait_for_archived(base_url, session_b, True)

    # Undo restores the whole batch.
    pill.get_by_test_id("archive-undo-button").click()

    # Both rows return to the sidebar...
    expect(page.locator(f'a[href="/c/{session_a}"]')).to_have_count(1)
    expect(page.locator(f'a[href="/c/{session_b}"]')).to_have_count(1)

    # ...and the un-archive is durable: the store rows carry archived: false,
    # not just the client cache.
    _wait_for_archived(base_url, session_a, False)
    _wait_for_archived(base_url, session_b, False)
