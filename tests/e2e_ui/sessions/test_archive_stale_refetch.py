"""Browser e2e: an archived session must not pop back into the sidebar.

Archiving is optimistic: ``useArchiveConversation``'s ``onMutate`` overlays
``archived: true`` into the cached lists and the row unmounts before the
``PATCH /v1/sessions/{id}`` resolves. But unlike delete — which shares a
``deletingSessionIds`` tombstone with every list writer — archive shares no
tombstone, so a list refetch whose DB read predates the PATCH commit can
resolve *after* the optimistic removal, replace the cached pages wholesale,
and resurrect the just-archived row until the next push frame or poll.

These tests pin that race deterministically instead of spamming quick
archives and hoping to straddle the debounce window:

- The archive PATCH for the target session is **held** at the network layer
  (a Playwright route stashes it un-forwarded), so the server keeps
  reporting the row as unarchived — exactly the state a slow PATCH leaves
  the DB in while the sidebar reloads.
- A sidebar list reload is then forced the same way real concurrent
  activity does it: another client (``httpx``) renames a *different*,
  watched session; its ``WS /v1/sessions/updates`` frame marks the list for
  refetch and fires the debounced ``invalidateQueries``.
- The refetched page resolves while the archived row's PATCH is still in
  flight. With no tombstone filtering the snapshot, the row pops back in.

Each test releases the held PATCH afterwards so the archive commits, then
asserts the server-side flag: a green run proves a fix suppressed only the
stale snapshot, not the archive itself.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
from playwright.sync_api import Locator, Page, Response, Route, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

# Reserved label key that stores project membership (see
# ``sqlalchemy_store.list_projects`` and ``web/src/lib/sessionListCache.ts``).
_PROJECT_LABEL_KEY = "omni_project"

# How long to keep watching for a resurrected row after the stale list
# response has landed. The buggy repaint happens as soon as React Query
# commits that response, so a couple of seconds past the response event is
# generous.
_RESURRECT_WATCH_S = 2.5

# Deadline for the rename-triggered list refetch to complete: WS frame
# delivery + the client's 250 ms invalidate debounce + one fetch round-trip,
# with slack for a busy CI box.
_REFETCH_DEADLINE_S = 20.0

# Keep a resurrected row on screen briefly before failing, so a recording of
# the run shows the bug instead of ending on the frame it appears.
_FILM_LINGER_MS = 1_500


def _rename(base_url: str, session_id: str, title: str) -> None:
    """Set a session's title via ``PATCH /v1/sessions/{id}`` (another client)."""
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _seed_titled_session(base_url: str, *, title: str, project: str | None = None) -> str:
    """Create a session over the REST API, title it, optionally file it.

    Same multipart ``POST /v1/sessions`` path as the ``seeded_session``
    fixture; no runner binding — the session only needs to list in the
    sidebar and be archived, neither of which dispatches to a runner.

    :param base_url: The live server base URL.
    :param title: Unique title so the row is unambiguous on the shared server.
    :param project: Optional project name to file the session under (legacy
        ``omni_project`` label, dual-read by the projects list).
    :returns: The new session id.
    """
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_hello_world_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    body: dict[str, object] = {"title": title}
    if project is not None:
        body["labels"] = {_PROJECT_LABEL_KEY: project}
    patch_resp = httpx.patch(f"{base_url}/v1/sessions/{session_id}", json=body, timeout=10.0)
    patch_resp.raise_for_status()
    return session_id


def _link(page: Page, title: str) -> Locator:
    """The sidebar row link, keyed by its unique accessible name (the title)."""
    return page.get_by_role("link", name=title, exact=True)


def _row(page: Page, title: str) -> Locator:
    """The sidebar row (``<li>``) containing the titled link."""
    return page.locator("li").filter(has=_link(page, title))


def _section(page: Page, title: str) -> Locator:
    """The sidebar ``<section>`` whose collapse-header button reads *title*."""
    return page.locator("section").filter(has=page.get_by_role("button", name=title, exact=True))


def _archive_via_kebab(page: Page, row: Locator) -> None:
    """Archive *row* through the hover-revealed kebab menu."""
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("archive-conversation").click()


def _hold_archive_patches(page: Page, session_id: str) -> list[Route]:
    """Stash (do not forward) archive PATCHes for *session_id*.

    Simulates a slow archive request: the browser's fetch stays in flight and
    the server keeps reporting the row unarchived until the test releases the
    stashed route via :func:`_release_held`. All other traffic to the
    session's URL (snapshot GETs, the cleanup DELETE) passes through.

    :param page: Playwright page, routed before the archive click.
    :param session_id: Session whose archive PATCH is held.
    :returns: Mutable list the pending routes are appended to.
    """
    held: list[Route] = []

    def _stash(route: Route) -> None:
        request = route.request
        if request.method == "PATCH" and "archived" in (request.post_data or ""):
            held.append(route)  # released later from the test body
            return
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}", _stash)
    return held


def _release_held(held: list[Route]) -> None:
    """Forward every stashed PATCH so the archive commits server-side."""
    for route in held:
        with contextlib.suppress(Exception):
            route.continue_()
    held.clear()


def _track_list_refetches(page: Page, *, project: str | None = None) -> list[str]:
    """Record completed non-archived ``GET /v1/sessions`` list responses.

    :param page: Playwright page, subscribed before the archive clicks.
    :param project: ``None`` tracks the flat sidebar list (no ``project=``
        param); a name tracks that folder's ``?project=`` list.
    :returns: Mutable list of response URLs, appended as responses land.
    """
    seen: list[str] = []

    def _on_response(response: Response) -> None:
        url = urlparse(response.url)
        if url.path != "/v1/sessions" or response.request.method != "GET":
            return
        query = parse_qs(url.query)
        if "include_archived" in query:
            return
        if query.get("project", [None])[0] != project:
            return
        seen.append(response.url)

    page.on("response", _on_response)
    return seen


def _wait_for_new_refetch(page: Page, refetches: list[str], baseline: int) -> None:
    """Block until a list refetch response lands beyond *baseline*.

    Polls via ``page.wait_for_timeout`` (never ``time.sleep``): Playwright's
    sync API dispatches route/response callbacks cooperatively on this
    thread, so a bare sleep would starve the handlers this wait depends on.
    """
    deadline = time.monotonic() + _REFETCH_DEADLINE_S
    while len(refetches) <= baseline and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert len(refetches) > baseline, (
        "the sidebar list never refetched after the rename push frame; "
        "the stale-refetch race this test pins was not exercised"
    )


def _watch_for_resurrection(page: Page, link: Locator) -> bool:
    """True if *link* reappears in the DOM within the watch window."""
    deadline = time.monotonic() + _RESURRECT_WATCH_S
    while time.monotonic() < deadline:
        if link.count() > 0:
            return True
        page.wait_for_timeout(100)
    return False


def _wait_archived_server_side(base_url: str, session_id: str, page: Page) -> None:
    """Assert the released archive PATCH committed (``archived: true``)."""
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if snapshot.status_code == 200 and snapshot.json().get("archived") is True:
            return
        page.wait_for_timeout(250)
    raise AssertionError("released archive PATCH never committed server-side")


def _delete_sessions(base_url: str, session_ids: list[str]) -> None:
    """Best-effort cleanup so seeded sessions don't leak into other tests."""
    for session_id in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def test_archived_session_stays_out_of_sidebar_during_stale_refetch(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Quick archives + a list reload must not resurrect the archived row.

    Journey (the reported repro): archive two sessions back-to-back from the
    sidebar kebab while the list is reloading. The second session's PATCH is
    still in flight when the reload's GET resolves, so the response carries
    ``archived: false`` for it and — with no archive tombstone — the row
    pops back into the sidebar until the next push frame or poll.

    Failure mode this catches: ``fetchConversationsPage`` writes a list
    snapshot that no archive tombstone filters, so a stale full-page replace
    resurrects a row the user just archived (delete is already covered by
    ``deletingSessionIds``; archive has no equivalent).
    """
    base_url, trigger_id = seeded_session
    run = uuid.uuid4().hex[:8]
    title_a = f"e2e-arch-a-{run}"
    title_b = f"e2e-arch-b-{run}"
    trigger_title = f"e2e-arch-trigger-{run}"

    _rename(base_url, trigger_id, trigger_title)
    a_id = _seed_titled_session(base_url, title=title_a)
    b_id = _seed_titled_session(base_url, title=title_b)

    page.set_viewport_size({"width": 1280, "height": 800})
    # Keep a *different* session active so archiving A/B never triggers the
    # archive-the-active-session home redirect mid-test.
    page.goto(f"{base_url}/c/{trigger_id}")
    expect(_link(page, title_a)).to_be_visible()
    expect(_link(page, title_b)).to_be_visible()

    held = _hold_archive_patches(page, b_id)
    refetches = _track_list_refetches(page)

    try:
        # Archive A (commits normally), then B in quick succession. B's row
        # leaves the sidebar optimistically while its PATCH stays in flight.
        _archive_via_kebab(page, _row(page, title_a))
        expect(_link(page, title_a)).to_have_count(0)
        _archive_via_kebab(page, _row(page, title_b))
        expect(_link(page, title_b)).to_have_count(0)

        # Reload the sidebar list the way concurrent activity does: another
        # client renames a watched session; the push frame schedules the
        # debounced invalidation, and the refetch's DB read still sees B as
        # unarchived because its PATCH is held in flight.
        baseline = len(refetches)
        _rename(base_url, trigger_id, f"{trigger_title}-renamed")
        _wait_for_new_refetch(page, refetches, baseline)

        resurrected = _watch_for_resurrection(page, _link(page, title_b))
        if resurrected:
            page.wait_for_timeout(_FILM_LINGER_MS)
        assert not resurrected, (
            "archived session popped back into the sidebar: the stale list "
            "refetch replaced the cached pages and resurrected the row "
            "because archive shares no tombstone with the list writers"
        )

        # Green path: a fix must suppress only the stale snapshot, not the
        # archive itself — release the PATCH and the flag must still commit.
        _release_held(held)
        _wait_archived_server_side(base_url, b_id, page)
        expect(_link(page, title_b)).to_have_count(0)
    finally:
        _release_held(held)
        _delete_sessions(base_url, [a_id, b_id])


def test_archived_folder_row_stays_out_during_stale_project_refetch(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A project folder must not repaint its just-archived member either.

    Same race on the secondary path: each expanded folder renders its own
    ``["project-sessions", <name>]`` list, which the archive ``onMutate``
    never cancels (rename/delete cancel both query families) and no
    tombstone filters — so a folder refetch resolving while the archive
    PATCH is in flight repaints the archived row inside the folder.
    """
    base_url, trigger_id = seeded_session
    run = uuid.uuid4().hex[:8]
    project = f"e2e-arch-proj-{run}"
    title_filed = f"e2e-arch-filed-{run}"
    title_member = f"e2e-arch-member-{run}"
    trigger_title = f"e2e-arch-ptrigger-{run}"

    _rename(base_url, trigger_id, trigger_title)
    filed_id = _seed_titled_session(base_url, title=title_filed, project=project)
    # A second member keeps the folder alive once the first row archives.
    member_id = _seed_titled_session(base_url, title=title_member, project=project)

    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/c/{trigger_id}")

    # Folders render collapsed; expand so the folder's own list query is
    # live (a collapsed folder fetches nothing) and its rows are visible.
    folder_header = page.get_by_role("button", name=project, exact=True)
    expect(folder_header).to_be_visible()
    folder_header.click()
    expect(folder_header).to_have_attribute("aria-expanded", "true")
    folder = _section(page, project)
    folder_row_link = folder.get_by_role("link", name=title_filed, exact=True)
    expect(folder_row_link).to_be_visible()

    held = _hold_archive_patches(page, filed_id)
    refetches = _track_list_refetches(page, project=project)

    try:
        _archive_via_kebab(page, _row(page, title_filed))
        expect(folder_row_link).to_have_count(0)

        # Force the folder list to reload while the archive PATCH is held:
        # the rename push frame's debounced invalidation covers
        # ["project-sessions"] too, and nothing cancelled or filters it.
        baseline = len(refetches)
        _rename(base_url, trigger_id, f"{trigger_title}-renamed")
        _wait_for_new_refetch(page, refetches, baseline)

        resurrected = _watch_for_resurrection(page, folder_row_link)
        if resurrected:
            page.wait_for_timeout(_FILM_LINGER_MS)
        assert not resurrected, (
            "archived session popped back into its project folder: the "
            "stale project-sessions refetch repainted the row because the "
            "archive neither cancels nor tombstones the folder lists"
        )

        _release_held(held)
        _wait_archived_server_side(base_url, filed_id, page)
        expect(folder_row_link).to_have_count(0)
    finally:
        _release_held(held)
        _delete_sessions(base_url, [filed_id, member_id])
