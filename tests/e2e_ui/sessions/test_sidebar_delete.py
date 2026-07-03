"""Browser e2e for deleting a session from the sidebar.

The row kebab's "Delete" item opens a confirmation dialog; confirming it
fires the stop→``DELETE /v1/sessions/{id}`` chain
(``useStopAndDeleteConversation``). Delete is fire-and-forget: the dialog
closes immediately, the row shows a transient "Deleting…" status, then
drops out of the list, and if the deleted session is the active one the
SPA bounces back to ``/``.

This asserts both halves of a real delete:

  - The row is removed from the sidebar (client list converges).
  - The session is gone from the conversation store —
    ``GET /v1/sessions/{id}`` returns 404 — so the removal is durable,
    not just a cache splice the next refetch would resurrect.

The runner-kill half of "delete stops its runner" isn't asserted here:
the e2e harness binds a tunneled, non-host runner, and the stop forwarded
ahead of the delete is a no-op for the openai-agents harness (no external
process to kill, and only host-spawned sessions stop their runner). The
store-removal contract above is the part observable in this harness.
"""

from __future__ import annotations

import time
import uuid

import httpx
from playwright.sync_api import Locator, Page, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


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

    # Open the kebab → Delete → confirm in the dialog. Hover first so the
    # desktop hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("delete-conversation").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Delete", exact=True).click()

    # The row drops out of the sidebar: it first swaps to a transient
    # "Deleting…" status (no href) while the stop→DELETE chain is in
    # flight, then unmounts entirely on success.
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_have_count(0)

    # And the deletion is durable: the conversation is gone from the
    # store, not just spliced from the client cache. The href vanishes
    # the moment the mutation starts (the "Deleting…" state), and the
    # mutation runs ``stopSession`` ahead of the DELETE, so the
    # server-side row removal lands slightly after the UI does — poll
    # until ``GET /v1/sessions/{id}`` reports 404.
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
