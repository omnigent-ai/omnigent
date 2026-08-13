"""Browser e2e for archiving a session from the sidebar.

The row kebab's "Archive" item fires a single
``PATCH /v1/sessions/{id}`` with ``archived: true``; the row shows a
transient "Archiving…" status and then drops out of the default list
(archived sessions live on the Settings page). The runner stop is the
server's job, spawned in the background once the flag commits.

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

import asyncio
import threading
import time
import uuid
from collections.abc import Coroutine
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.async_api import Route, WebSocketRoute, async_playwright
from playwright.async_api import expect as async_expect
from playwright.sync_api import Locator, Page, Request, expect


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its href."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    This file mixes sync ``page``-fixture tests with one async-Playwright test
    (the spinner-persistence test below holds a route open across UI
    assertions). The e2e_ui suite runs many pytest-playwright **sync** tests in
    the same session; once one has run, pytest-asyncio can't start a loop on the
    main thread ("Runner.run() cannot be called from a running event loop").
    Running the coroutine from a fresh thread via :func:`asyncio.run` sidesteps
    that. Any exception — including assertion failures — is captured and
    re-raised on the calling thread so the test fails normally. Mirrors
    ``test_cross_session_routing._run_in_fresh_loop``.

    :param coro: The coroutine to run to completion.
    :raises BaseException: Whatever the coroutine raised, re-raised here.
    """
    captured: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


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

    # The row drops out of the default sidebar list: it first swaps to a
    # transient "Archiving…" status (no href) while the PATCH is in
    # flight, then unmounts entirely once the list refetches without it.
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


def test_archiving_spinner_stays_until_row_leaves(
    seeded_session: tuple[str, str],
) -> None:
    """The "Archiving…" spinner must persist until the row actually leaves.

    Regression test for a visible flicker: the spinner disappeared the
    instant the archive PATCH resolved, but the row lingered in the
    sidebar for the extra round-trip it takes the ``["conversations"]``
    list refetch to drop the now-archived row. For that window the row
    swapped back to its plain, interactive form — no spinner, still
    clickable — which reads as "archive finished" while the session is
    plainly still listed.

    The fix keeps the spinner mounted for the whole span, tearing it down
    only when the row itself unmounts. This asserts that ordering directly.

    Driven through async Playwright (not the sync ``page`` fixture) because
    it holds the list-refetch response open across UI assertions to freeze
    the exact window where the two events can separate.

    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        session.
    """
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_archiving_spinner_persists(base_url, session_id))


async def _drive_archiving_spinner_persists(base_url: str, session_id: str) -> None:
    """Async body of the spinner-persistence test. See the test docstring.

    Reproduction hinges on freezing the window between "PATCH resolved" and
    "row gone". A row leaves the sidebar by exactly two paths, and BOTH are
    pinned open here so the window can't close on its own:

    - **List refetch.** ``useArchiveConversation`` invalidates
      ``["conversations"]`` on PATCH success, which refetches
      ``GET /v1/sessions?…&include_archived=true``; that response, re-peeled
      through the sidebar's archived filter, is what drops the row. The
      handler lets the PATCH itself through (so the mutation resolves — and
      the buggy code's ``onSettled`` fires) but holds the *list refetch*
      open until the assertions have run.
    - **WS push.** An ``archived: true`` frame over
      ``WS /v1/sessions/updates`` would splice the row out client-side
      (``violatesKnownMembership``). The socket is intercepted and its
      client frames swallowed, so no live push arrives to close the window
      behind the test's back.

    With both frozen, the spinner's fate is decided purely by the row's own
    state: the fixed code keeps ``conversation-archiving`` mounted; the old
    code unmounts it and re-shows the interactive ``/c/{id}`` link while the
    row is still present. Releasing the held refetch then proves the row
    really does leave (and the spinner with it), so the freeze didn't merely
    stall the whole flow.

    :param base_url: Spawned server base URL.
    :param session_id: The runner-bound session archived in this test.
    """
    href = f'a[href="/c/{session_id}"]'
    spinner = '[data-testid="conversation-archiving"]'

    def _is_sidebar_list_get(request: Any) -> bool:
        """A GET for the sidebar's paginated session list.

        Distinguished from the sibling ``pinned=true`` query (same
        ``/v1/sessions`` path) by ``include_archived=true`` + no ``pinned``
        param — the exact signature the sidebar's ``useConversations("", true)``
        page issues (``fetchConversationsPage``). Holding the pinned query
        instead would let the real list refetch through and unfreeze the row.
        """
        if request.method != "GET":
            return False
        parsed = urlparse(request.url)
        if parsed.path != "/v1/sessions":
            return False
        return "include_archived=true" in (parsed.query or "") and "pinned=true" not in (
            parsed.query or ""
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            # Give the row a stable, unique title so it's unambiguous in the
            # sidebar and its snapshot is easy to assert on.
            title = f"e2e-archive-spin-{uuid.uuid4().hex[:8]}"
            rename = await page.request.patch(
                f"{base_url}/v1/sessions/{session_id}",
                data={"title": title},
                timeout=10_000,
            )
            assert rename.ok, f"rename failed: {rename.status}"

            release_refetch = asyncio.Event()
            # Set by the test body right after the Archive item is clicked.
            # Only list GETs that fire AFTER that are held, so the sidebar's
            # initial load renders the row first. Gating on the click (not the
            # PATCH body) keeps the freeze independent of request-body parsing.
            archiving_started = {"done": False}
            refetch_held = {"done": False}
            # Diagnostics: every /v1/sessions request, for a precise failure msg.
            list_gets: list[str] = []

            async def handle_sessions(route: Route) -> None:
                request = route.request
                # The list refetch (or reconcile) that would drop the archived
                # row: hold the FIRST one that fires after the archive click
                # until the test releases it. Earlier list GETs (initial
                # sidebar load) pass through so the row renders first.
                if _is_sidebar_list_get(request):
                    list_gets.append(f"{request.method} {request.url}")
                    if archiving_started["done"] and not refetch_held["done"]:
                        refetch_held["done"] = True
                        await release_refetch.wait()
                    await route.continue_()
                    return
                await route.continue_()

            async def handle_updates_ws(ws: WebSocketRoute) -> None:
                # Swallow client watch-set frames without connecting to the real
                # server, so no live ``archived: true`` push can splice the row
                # out from under the test. Mirrors test_hosts_changed_push.
                ws.on_message(lambda _msg: None)

            # One handler for both the list (``/v1/sessions``) and the
            # per-session PATCH (``/v1/sessions/{id}``); the glob covers both.
            await page.route("**/v1/sessions*", handle_sessions)
            await page.route_web_socket(
                lambda url: "/v1/sessions/updates" in url, handle_updates_ws
            )

            await page.goto(f"{base_url}/c/{session_id}")

            row = page.locator("li").filter(has=page.locator(href))
            await async_expect(row).to_be_visible()

            # Open the kebab → Archive. Arm the refetch-hold on the click, so
            # only list GETs triggered by the archive (its ['conversations']
            # invalidation) are frozen — not the initial sidebar load.
            await row.hover()
            await row.get_by_test_id("conversation-actions").click()
            await page.get_by_test_id("archive-conversation").click()
            archiving_started["done"] = True

            # The spinner appears while the PATCH is in flight.
            await async_expect(page.locator(spinner)).to_be_visible()

            # Wait until the post-archive list refetch is actually held — from
            # here the ONLY thing that can remove the row is releasing it, so
            # the window is genuinely frozen and the next assertion is not a
            # race against a refetch that simply hasn't fired yet.
            deadline = asyncio.get_running_loop().time() + 15.0
            while not refetch_held["done"]:
                if asyncio.get_running_loop().time() > deadline:
                    raise AssertionError(
                        "post-archive sidebar-list refetch never fired within 15s — "
                        "the archive PATCH may not have invalidated ['conversations']. "
                        f"sidebar-list GETs seen: {list_gets}"
                    )
                await page.wait_for_timeout(50)

            # KEY ASSERTION. The PATCH has resolved (the refetch it triggered is
            # what we're holding), so the buggy code has already cleared
            # `isArchiving` and swapped the row back to its interactive link.
            # The fix keeps the spinner mounted. Give the buggy behavior ~1.5s
            # of settle time so a regression fails loudly, not flakily. The
            # visible spinner also proves the row's <li> is still present — the
            # spinner IS the row's content mid-archive.
            await page.wait_for_timeout(1_500)
            await async_expect(page.locator(spinner)).to_be_visible()
            # The row must still be its spinner form (a <div>, no anchor), NOT
            # back to the interactive `/c/{id}` link. A reappearing link here —
            # count 1 instead of the expected 0 — is the exact regression: the
            # spinner was torn down a round-trip before the row actually left.
            await async_expect(page.locator(href)).to_have_count(0)
            assert await page.get_by_role("link", name=title).count() == 0, (
                "the interactive session link reappeared while the row was still "
                "in the sidebar — the spinner was torn down too early"
            )

            # Release the held refetch: the row — and its spinner — must now
            # leave the default list. This proves the freeze stalled removal
            # rather than the whole flow, and that the spinner ends exactly at
            # unmount (not before, per the assertions above; not lingering
            # after, per this one).
            release_refetch.set()
            await async_expect(page.locator(spinner)).to_have_count(0)
            await async_expect(page.locator(href)).to_have_count(0)

            # Durable, not just a cache splice: the store row carries the flag.
            snap = await page.request.get(f"{base_url}/v1/sessions/{session_id}", timeout=10_000)
            assert snap.ok, f"snapshot fetch failed: {snap.status}"
            assert (await snap.json())["archived"] is True, "session should report archived=true"
        finally:
            await browser.close()
