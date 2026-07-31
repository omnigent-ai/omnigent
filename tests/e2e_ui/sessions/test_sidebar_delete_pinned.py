"""Browser e2e for deleting a *pinned* session from the sidebar.

The Pinned section renders from a query cache (``["pinned-conversations"]``)
that is a deliberate sibling of the paginated ``["conversations"]`` list, so
the delete mutation's prefix-matched sweep over ``["conversations"]`` does not
touch it (see ``useConversations.ts`` — nesting the pinned key under that
prefix would break the pin-toggle's own cache patch). The delete handlers must
therefore drop the row from the pinned cache *explicitly*.

The regression this guards: they didn't, so deleting a pinned session removed
it from the flat list but left its row stranded in the "Pinned" section until a
full reload.

Two harness details are load-bearing here — without them a green test would
prove nothing:

  - The page sits on ``/`` (no active chat). Deleting the *open* session
    bounces the SPA to ``/`` and refetches, and a live ``WS /v1/sessions/updates``
    ``removed`` frame reconciles an active session's list too — either path
    clears the Pinned row regardless of the cache bug.
  - No ``page.reload()``. A reload refetches ``?pinned=true`` and converges the
    section on its own. Only an in-place assertion isolates the delete handler's
    own pinned-cache patch — the code under test.
"""

from __future__ import annotations

import time
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _section(page: Page, title: str) -> Locator:
    """Locate the sidebar ``<section>`` whose header reads *title*."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def test_delete_pinned_session_clears_it_from_pinned_section(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Deleting a pinned session clears its row from the Pinned section.

    Failure mode this catches that the flat-list delete test can't: the
    delete splices the row out of ``["conversations"]`` but leaves it in the
    sibling ``["pinned-conversations"]`` cache, so the Pinned section keeps
    rendering the deleted row until a reload.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-delete-pinned-{uuid.uuid4().hex[:8]}"
    httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    ).raise_for_status()

    # Sit on home so the session is NOT the active chat — deleting the open
    # session would navigate/refetch and mask the pinned-cache bug.
    page.goto(f"{base_url}/")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    # Pin via the row's quick action, then confirm it landed under "Pinned".
    row.hover()
    row.get_by_test_id("quick-pin-conversation").click()
    pinned_row = (
        _section(page, "Pinned")
        .locator("li")
        .filter(has=page.locator(f'a[href="/c/{session_id}"]'))
    )
    expect(pinned_row.locator(f'a[href="/c/{session_id}"]')).to_be_visible()

    # Delete from within the Pinned section: kebab → Delete → confirm.
    pinned_row.hover()
    pinned_row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("delete-conversation").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Delete", exact=True).click()

    # This was the only pinned session, so once the delete patches the pinned
    # cache the whole "Pinned" section unmounts. Assert on the SECTION, not the
    # row's href: while the delete is in flight the row swaps to a "Deleting…"
    # status row that has no href, so an href-count assertion flickers to 0
    # during that transient and would pass even against the buggy build (the
    # href reappears a beat later when the stale pinned cache re-renders the
    # row). The section only disappears when the pinned cache is truly empty —
    # the DeletingRow keeps it mounted — so it can't be fooled by the transient.
    #
    # In place, no reload: a reload refetches ?pinned=true and converges the
    # section on its own, hiding the bug. A tight 3s timeout (vs the suite's
    # 15s streaming default) keeps a regression failing fast; the delete's
    # stop→DELETE round-trip resolves well inside it.
    fast = 3_000
    expect(_section(page, "Pinned")).to_have_count(0, timeout=fast)
    # And it's gone from the sidebar entirely.
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_have_count(0, timeout=fast)

    # The deletion is durable: gone from the store, not just a cache splice a
    # refetch could resurrect. The DELETE trails a best-effort stop, so poll
    # until ``GET /v1/sessions/{id}`` reports 404 (already landed by the time
    # the UI assertions above pass, so this resolves on the first iteration).
    deadline = time.monotonic() + 5.0
    last_status = None
    while time.monotonic() < deadline:
        last_status = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).status_code
        if last_status == 404:
            break
        time.sleep(0.25)
    assert last_status == 404, (
        f"deleted session should be gone from the store (404), got {last_status}"
    )
