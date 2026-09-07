"""Browser e2e for deleting a session from the sidebar.

The row kebab's "Delete" item opens a confirmation dialog; confirming it
fires the stop→``DELETE /v1/sessions/{id}`` chain
(``useStopAndDeleteConversation``), and if the deleted session is the
active one the SPA bounces back to ``/``.

Delete is *optimistic*: the row is spliced out of every cached list in
``onMutate``, so it leaves the sidebar on the next frame rather than
after the stop + DELETE round trip (which carries runner teardown,
worktree cleanup, and managed-sandbox teardown behind it). There is
deliberately no in-flight placeholder — the row unmounts outright.

This asserts three things:

  - The row is removed from the sidebar, with no "Deleting…" placeholder
    standing in for it (the pre-optimistic behavior, which kept the row
    on screen for the whole round trip).
  - The session is gone from the conversation store —
    ``GET /v1/sessions/{id}`` returns 404 — so the removal is durable,
    not just a cache splice the next refetch would resurrect.
  - A *failed* delete puts the row back and reports via a toast. That
    rollback is only reachable because the row left before the server
    answered, so it is the load-bearing proof that delete is optimistic.

The runner-kill half of "delete stops its runner" isn't asserted here:
the e2e harness binds a tunneled, non-host runner, and the stop forwarded
ahead of the delete is a no-op for the openai-agents harness (no external
process to kill, and only host-spawned sessions stop their runner). The
store-removal contract above is the part observable in this harness.
"""

from __future__ import annotations

import re
import time
import uuid

import httpx
from playwright.sync_api import Locator, Page, Route, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _confirm_delete(page: Page, row: Locator) -> None:
    """Open *row*'s kebab and confirm the delete dialog.

    Hovers first so the desktop hover-revealed kebab trigger is
    interactable.

    :param page: Playwright page.
    :param row: The sidebar ``<li>`` to delete.
    """
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("delete-conversation").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Delete", exact=True).click()


def test_delete_session_removes_row_and_from_store(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Deleting a session removes its row and the store row (404).

    Failure modes this catches:

    - The DELETE never fires (or 4xxs) so the row lingers.
    - The row is spliced from the client cache but the server keeps the
      conversation, so a refetch/reload would bring it back.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-delete-{uuid.uuid4().hex[:8]}"
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    _confirm_delete(page, row)

    # The row unmounts outright — no placeholder stands in for it. Assert
    # the whole <li> is gone, not just its href: the pre-optimistic code
    # kept the <li> and swapped its contents for a "Deleting…" status,
    # which would satisfy an href-only check while the row was still
    # visibly occupying the sidebar.
    expect(row).to_have_count(0)
    expect(page.get_by_test_id("conversation-deleting")).to_have_count(0)

    # And the deletion is durable: the conversation is gone from the
    # store, not just spliced from the client cache. The row leaves the
    # sidebar the moment the mutation starts, and the mutation runs
    # ``stopSession`` ahead of the DELETE, so the server-side row removal
    # lands well after the UI does — poll until ``GET /v1/sessions/{id}``
    # reports 404.
    deadline = time.monotonic() + 15.0
    last_status = None
    while time.monotonic() < deadline:
        last_status = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).status_code
        if last_status == 404:
            break
        time.sleep(0.25)
    assert last_status == 404, (
        f"deleted session should be gone from the store (404), got {last_status}"
    )


def test_failed_delete_restores_row_and_warns(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A failed delete puts the row back and surfaces a toast.

    This is the load-bearing test for delete being optimistic: the row
    can only come *back* if it had already left before the server
    answered. It also covers the failure path the removed inline
    error/retry row used to own — once the row is spliced out it
    unmounts, taking any in-row error state with it, so the toast is the
    only signal the user gets.

    Failure modes this catches:

    - The optimistic splice is never rolled back, so a delete that
      failed still looks like it succeeded and the session silently
      reappears on the next reload.
    - The rollback restores the list but drops the user's only notice
      that anything went wrong.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-delete-fail-{uuid.uuid4().hex[:8]}"
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")
    row = _row(page, session_id)
    expect(row).to_be_visible()

    # Fail the DELETE (and only the DELETE — the stop that precedes it,
    # and every unrelated session request, must still reach the server).
    def _fail_delete(route: Route) -> None:
        if route.request.method == "DELETE":
            route.fulfill(status=500, content_type="application/json", body='{"detail":"boom"}')
        else:
            route.continue_()

    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _fail_delete)

    _confirm_delete(page, row)

    # Rolled back: the row is in the sidebar again...
    expect(row).to_be_visible()
    # ...and the user is told, naming the session so a failure is
    # attributable when several deletes are in flight.
    toast = page.get_by_test_id("toast")
    expect(toast).to_be_visible()
    expect(toast).to_contain_text("back in the sidebar")
    expect(toast).to_contain_text(title)

    # The session really is still there — the rollback reflects reality
    # rather than just repainting a row the server had already dropped.
    assert httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).status_code == 200
