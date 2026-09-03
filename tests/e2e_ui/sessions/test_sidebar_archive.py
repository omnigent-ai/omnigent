"""Browser e2e for archiving a session from the sidebar.

The row kebab's "Archive" item fires a single ``PATCH /v1/sessions/{id}``
with ``archived: true``. The row leaves the sidebar optimistically — the
cached ``archived`` flag flips in ``onMutate`` and the sidebar filters
archived rows out client-side — and the server stops the session in the
background once the flag commits. Archived sessions live on the Settings
page, not the sidebar.

This asserts both halves of a real archive:

  - The row leaves the sidebar and the store reports ``archived: true``,
    so the removal is durable rather than a cache splice a refetch would
    resurrect.
  - The client sends **no** ``stop_session`` event. Archiving used to
    send one and wait for it, which put the runner's stop timeouts
    (5s pane kill + 10s host-teardown ack) in front of the flag flip.
    Sending one *concurrently* would be worse: it would race the
    server's own stop against the same runner, and the loser gets a 503
    from the already-killed pane — which on a host-spawned session
    skips the host-runner teardown and orphans the runner.

The teardown half ("archiving stops its runner") isn't asserted here
for the same reason ``test_sidebar_delete`` doesn't: the e2e harness
binds a tunneled, non-host runner, so there is no host-launched runner
to tear down. Server-side coverage for that lives in
``tests/server/integration/test_sessions_archive.py``.
"""

from __future__ import annotations

import time
import uuid

import httpx
from playwright.sync_api import Locator, Page, Request, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def test_archive_session_removes_row_without_stop_event(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Archiving removes the row, archives the store row, and sends no stop.

    Failure modes this catches:

    - The PATCH never fires (or 4xxs) so the row lingers in the sidebar.
    - The row is hidden client-side but the store still reports
      ``archived: false``, so a reload brings it back.
    - The client re-grows a ``stop_session`` leg, putting the runner's
      stop timeouts back on the archive path (when serialized) or
      racing the server's stop against the same runner (when parallel).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    title = f"e2e-archive-{uuid.uuid4().hex[:8]}"
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()

    stop_events: list[str] = []

    def _record_stop(request: Request) -> None:
        """Record any client-sent ``stop_session`` event."""
        if request.method != "POST" or not request.url.endswith("/events"):
            return
        if "stop_session" in (request.post_data or ""):
            stop_events.append(request.url)

    page.on("request", _record_stop)
    page.goto(f"{base_url}/c/{session_id}")

    row = _row(page, session_id)
    expect(row).to_be_visible()

    # Open the kebab → Archive. Hover first so the desktop
    # hover-revealed kebab trigger is interactable.
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("archive-conversation").click()

    # The row drops out of the default sidebar list immediately: the cached
    # ``archived`` flag flips optimistically in onMutate and the sidebar filters
    # archived rows out client-side, so the row unmounts on the next frame.
    expect(page.locator(f'a[href="/c/{session_id}"]')).to_have_count(0)

    # Archiving the *active* session redirects to home so the user isn't
    # stranded on a stale URL with no sidebar row to navigate from.
    page.wait_for_url(f"{base_url}/", timeout=10_000)

    # And the archive is durable: the store row carries the flag, not
    # just the client cache. Poll — the list refetch that unmounts the
    # row can land marginally before the snapshot reflects it.
    deadline = time.monotonic() + 15.0
    archived = None
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if snapshot.status_code == 200:
            archived = snapshot.json()["archived"]
            if archived is True:
                break
        time.sleep(0.25)
    assert archived is True, f"archived session should report archived=true, got {archived}"

    # The whole point of the change: archiving is one PATCH. A client
    # stop here would either serialize the runner's timeouts ahead of
    # the flag flip or race the server's own stop against the runner.
    assert stop_events == [], f"archive must not send a stop_session event, sent {stop_events}"
