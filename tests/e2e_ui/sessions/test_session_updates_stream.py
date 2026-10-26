"""E2E: the ``WS /v1/sessions/updates`` push stream drives the sidebar.

This commit replaced the sidebar's 4 s HTTP poll of ``GET /v1/sessions``
with a client-driven WebSocket: the browser sends the session ids it's
showing and the server pushes ``snapshot`` / ``changed`` / ``removed``
frames as those sessions change. The two tests here cover the two halves
of that claim against a real server + real browser:

- ``test_session_rename_streams_to_open_tabs`` — a *third* client (the
  test, standing in for a CLI or another UI) renames a watched session
  over the API, and both already-open tabs reflect it **fast**, with no
  reload. "Fast" is the whole point: with the 4 s poll gone, the only
  path that delivers a change within a few seconds is the push stream —
  the HTTP fallback poll now runs at 45-60 s, far outside the assertion
  window. So a broken stream (handshake rejected, watch-set never sent,
  delta never pushed, cache merge regressed) makes this time out.

- ``test_idle_sidebar_does_not_poll_sessions_list`` — an idle sidebar
  generates no ``GET /v1/sessions`` list traffic over an observation
  window. This guards the headline win (the poll flood is gone); a
  regression that restored ``refetchInterval: 4000`` would light it up.

Selectors: the sidebar renders each session as ``<a href="/c/{id}">``
whose text is the session title (``web/src/shell/Sidebar.tsx`` — the
``Link to={`/c/${conversation.id}`}`` row). The default pytest-playwright
viewport (1280×720) is desktop, so the sidebar is shown without a toggle.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, Request, WebSocket, expect

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import TwoAgentChatSession, _build_hello_world_bundle


@pytest.mark.compat_smoke
def test_session_rename_streams_to_open_tabs(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """An API rename of a watched session reaches both open tabs fast.

    A failure means one of:

    - The updates WebSocket never connected / was rejected at handshake,
      so neither tab is watching the session (server route regression).
    - The client never derived the watch-set from its cached rows or
      never sent it (``SessionUpdatesProvider`` regression).
    - The server didn't diff the rename into a ``changed`` frame, or the
      client didn't merge it / trigger the title reconcile
      (``_emit_deltas`` / ``mergeItemsIntoPages`` regression).

    If the stream were dead the rename would still surface — but only via
    the 45-60 s HTTP fallback poll, so a sub-20 s assertion would time
    out, which is exactly what makes this a stream test and not a poll
    test.

    :param browser: Playwright session-scoped browser; two independent
        contexts stand in for the same user with two tabs open.
    :param seeded_session: ``(base_url, session_id)`` for a runner-bound
        session, created by the fixture.
    """
    base_url, session_id = seeded_session
    # Unique per run so the row-text assertion can only match the title we
    # just set, never a leftover title from another test on the shared DB.
    marker = f"ws-rename-{uuid.uuid4().hex[:8]}"
    row = f'a[href="/c/{session_id}"]'

    tab_a_ctx = browser.new_context()
    tab_b_ctx = browser.new_context()
    try:
        tab_a = tab_a_ctx.new_page()
        tab_b = tab_b_ctx.new_page()
        tab_a.goto(f"{base_url}/c/{session_id}")
        tab_b.goto(f"{base_url}/c/{session_id}")

        # Both sidebars render the session row ⇒ both tabs fetched the list,
        # cached the row, and (via SessionUpdatesProvider) registered it in
        # the WS watch-set. This must hold *before* the rename, or a later
        # match could be an initial snapshot rather than a live delivery.
        expect(tab_a.locator(row)).to_be_visible()
        expect(tab_b.locator(row)).to_be_visible()

        # A third client renames the session over the API — no browser
        # action triggers it, so any UI change is server-pushed.
        resp = httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"title": marker},
            timeout=10.0,
        )
        resp.raise_for_status()

        # KEY ASSERTION: both tabs show the new title within a window well
        # below the HTTP fallback cadence (45-60 s). Only the push stream
        # delivers this fast; a dead stream times out here.
        expect(tab_a.locator(row)).to_contain_text(marker, timeout=20_000)
        expect(tab_b.locator(row)).to_contain_text(marker, timeout=20_000)
    finally:
        tab_a_ctx.close()
        tab_b_ctx.close()


def _count_session_list_requests(page: Page) -> list[str]:
    """
    Record ``GET /v1/sessions`` *list* requests made by ``page``.

    Filters to the list endpoint only: ``/v1/sessions`` with a query
    string, excluding per-session reads (``/v1/sessions/{id}``...) and the
    SSE stream. Returns the backing list so the caller can snapshot its
    length before/after an observation window.

    :param page: The page to observe.
    :returns: A list that grows as matching requests fire (mutated by the
        event handler for the page's lifetime).
    """
    hits: list[str] = []

    def _on_request(request: Request) -> None:
        # The list endpoint is `/v1/sessions?...`; `/v1/sessions/<id>` and
        # `/v1/sessions/<id>/stream` are per-session and must not count.
        if "/v1/sessions?" in request.url:
            hits.append(request.url)

    page.on("request", _on_request)
    return hits


@pytest.mark.compat_smoke
def test_idle_sidebar_does_not_poll_sessions_list(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """An idle sidebar makes no ``GET /v1/sessions`` list calls.

    The old code refetched the list every 4 s (per connected-filter
    variant); the stream replaced that with push, leaving only a 45-60 s
    fallback reconcile. So after the initial load settles, an idle window
    must see *zero* list requests — a restored 4 s ``refetchInterval``
    would produce several within the window and fail this.

    The fixed waits are intentional: this asserts the *absence* of
    traffic, which can't be event-driven — we let the load settle, then
    watch a quiet window.

    :param page: Playwright page (default desktop viewport).
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session
    row = f'a[href="/c/{session_id}"]'

    hits = _count_session_list_requests(page)
    page.goto(f"{base_url}/c/{session_id}")
    # Row visible ⇒ the initial list fetch(es) completed and the stream had
    # a chance to connect and take over freshness.
    expect(page.locator(row)).to_be_visible()
    # Let any initial-load refetch flurry settle out before measuring.
    page.wait_for_timeout(6_000)

    baseline = len(hits)
    # Observe a quiet window comfortably longer than the old 4 s cadence
    # (which would add ~3 per filter variant here) but shorter than the
    # 45-60 s fallback (which must not fire).
    page.wait_for_timeout(12_000)
    new_requests = hits[baseline:]

    # Zero list polls in a 12 s idle window. Any here means the 4 s poll
    # wasn't actually suspended for the stream-covered sidebar.
    assert new_requests == [], (
        f"expected no GET /v1/sessions list polls while idle and "
        f"stream-connected, but saw {len(new_requests)}: {new_requests}"
    )


@pytest.mark.compat_smoke
def test_session_created_elsewhere_appears_via_push(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A session created elsewhere appears in an already-open sidebar fast,
    via the push-discovery path — no list poll.

    The open tab is watching only the sessions it already had cached, so the
    new one can never be in its watch-set. It shows up only because the create
    publishes a ``session_added`` event that the server pushes down the
    stream, which the client reconciles into the list. The assertion window is
    far below the 60 s HTTP fallback reconcile, so a broken discovery push
    (event not published, stream not subscribed, access check wrong) would make
    the new session appear only via that slow fallback and time out here.

    :param page: Playwright page (default desktop viewport).
    :param seeded_session: ``(base_url, existing_session_id)`` from the fixture
        — gives the tab a session to open so the sidebar + stream are live.
    """
    base_url, existing_id = seeded_session
    page.goto(f"{base_url}/c/{existing_id}")
    # Sidebar loaded and the stream is connected/watching before we create the
    # new session — so its later appearance can't be the initial page load.
    expect(page.locator(f'a[href="/c/{existing_id}"]')).to_be_visible()

    # Create a brand-new session "elsewhere" — over the API, the way a CLI or
    # another tab would — with no action in this browser.
    resp = post_session_bundle(
        httpx.post, f"{base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    resp.raise_for_status()
    new_id = resp.json()["session_id"]
    try:
        # KEY ASSERTION: the new session's row appears in the open sidebar well
        # within the push window — only the discovery push delivers it this
        # fast; the 60 s fallback reconcile would blow this timeout.
        expect(page.locator(f'a[href="/c/{new_id}"]')).to_be_visible(timeout=20_000)
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{new_id}", timeout=10.0)


# ── Pushed updates must not refetch cached sidebar lists ──────────────────

_PROJECT_LABEL_KEY = "omni_project"
# Folders page by 20 (``useProjectSessions``); 22 rows gives a second ``after=`` page.
_FOLDER_ROWS = 22
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
# Dispatch turn + sub-agent turn + inbox auto-wake: three serial model calls.
_RELAY_TIMEOUT_MS = 240_000
# Longer than the server's 4 s rescan tick plus the client's 250 ms debounce.
_SETTLE_MS = 6_000


class SidebarTraffic:
    """Record a page's ``GET /v1/sessions`` list requests and session-updates frames."""

    def __init__(self, page: Page) -> None:
        self.requests: list[tuple[float, str]] = []
        self.frames: list[tuple[float, dict]] = []
        page.on("request", self._on_request)
        page.on("websocket", self._on_websocket)

    def _on_request(self, request: Request) -> None:
        if request.method == "GET" and "/v1/sessions?" in request.url:
            self.requests.append((time.monotonic(), request.url))

    def _on_websocket(self, socket: WebSocket) -> None:
        if "/v1/sessions/updates" in socket.url:
            socket.on("framereceived", self._on_frame)

    def _on_frame(self, payload: str | bytes) -> None:
        text = payload if isinstance(payload, str) else payload.decode()
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if frame.get("type") in ("snapshot", "changed", "removed"):
            self.frames.append((time.monotonic(), frame))

    def list_requests(self, since: int) -> list[str]:
        return [url for _, url in self.requests[since:]]

    def describe(self, since: int) -> str:
        requests = [f"{t:.3f} {url}" for t, url in self.requests[since:]]
        frames = []
        for t, frame in self.frames:
            items = frame.get("items") or []
            summary = ",".join(
                f"{item.get('id', '?')[:8]}(parent={str(item.get('parent_session_id'))[:8]},"
                f"status={item.get('status')},updated_at={item.get('updated_at')})"
                for item in items
            )
            frames.append(f"{t:.3f} {frame['type']} {summary or frame.get('ids')}")
        return "list requests:\n  " + "\n  ".join(requests) + "\nframes:\n  " + "\n  ".join(frames)


def file_sessions_into_project(base_url: str, project: str, count: int) -> list[str]:
    ids: list[str] = []
    for i in range(count):
        resp = post_session_bundle(
            httpx.post, f"{base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
        )
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"title": f"Filed {i + 1:02d}", "labels": {_PROJECT_LABEL_KEY: project}},
            timeout=10.0,
        ).raise_for_status()
        ids.append(session_id)
    return ids


@pytest.fixture
def two_page_project_folder(live_server: str) -> Iterator[tuple[str, list[str]]]:
    """A project folder with more sessions than one 20-row folder page."""
    project = f"Load test {uuid.uuid4().hex[:6]}"
    ids = file_sessions_into_project(live_server, project, _FOLDER_ROWS)
    try:
        yield project, ids
    finally:
        for session_id in ids:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def folder_section(page: Page, project: str) -> Locator:
    header = page.get_by_role("button", name=project, exact=True)
    return page.locator("section").filter(has=header)


def expand_folder_to_all_pages(page: Page, project: str, expected_rows: int) -> None:
    """Expand a project folder and load it past its first page."""
    header = page.get_by_role("button", name=project, exact=True)
    expect(header).to_be_visible(timeout=30_000)
    if header.get_attribute("aria-expanded") != "true":
        header.click()
    section = folder_section(page, project)
    rows = section.locator('a[href^="/c/"]')
    load_more = section.get_by_role("button", name="Load more", exact=True)
    deadline = time.monotonic() + 30.0
    while rows.count() < expected_rows:
        # The sentinel auto-loads the next page once it nears the viewport; the
        # button is the fallback when the folder is short enough not to scroll.
        if load_more.is_visible():
            load_more.click()
        if time.monotonic() > deadline:
            raise AssertionError(
                f"folder {project!r} shows {rows.count()} rows, expected {expected_rows}"
            )
        page.wait_for_timeout(250)
    expect(rows).to_have_count(expected_rows)


def send_message(page: Page, text: str) -> None:
    composer = page.get_by_role("textbox", name=re.compile("Message the agent|Send a message"))
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def dispatch_prompt(routing_token: str) -> str:
    return (
        "Ask Deep Thought for the Answer to the Ultimate Question of Life, the "
        "Universe, and Everything, then tell me exactly what it said, including "
        f"its verification code. Routing marker: {routing_token}"
    )


def folder_page_reloads(urls: list[str], project: str) -> list[str]:
    """Folder page requests with an ``after=`` cursor, i.e. an already-loaded page re-requested."""
    return [
        url
        for url in urls
        if "after=" in url and parse_qs(urlparse(url).query).get("project") == [project]
    ]


def test_pushed_updates_do_not_reload_loaded_sidebar_pages(
    request: pytest.FixtureRequest,
    browser: Browser,
    two_agent_chat_session: TwoAgentChatSession,
    two_page_project_folder: tuple[str, list[str]],
) -> None:
    """Running an agent that spawns a sub-agent must not re-page expanded folders.

    Tab A opens the orchestrator session; tab B sits on the landing page where
    the orchestrator is a non-active row. Both expand the same folder past its
    first page. The dispatch then produces pushed ``changed`` frames: the
    orchestrator's status/updated_at (already cached in both tabs) and the
    sub-agent row (never in a ``kind=default`` list). Neither may make the
    client re-request the folder's already-loaded ``after=`` page.
    """
    chat = two_agent_chat_session
    project, _ = two_page_project_folder
    page = request.getfixturevalue("page")
    observer_ctx = browser.new_context()
    try:
        observer = observer_ctx.new_page()
        traffic_a = SidebarTraffic(page)
        traffic_b = SidebarTraffic(observer)

        page.goto(f"{chat.base_url}/c/{chat.session_id}")
        # `.first`: the Agents rail renders a second link to the open session.
        expect(page.locator(f'a[href="/c/{chat.session_id}"]').first).to_be_visible(timeout=30_000)
        expand_folder_to_all_pages(page, project, _FOLDER_ROWS)
        observer.goto(f"{chat.base_url}/")
        expect(observer.locator(f'a[href="/c/{chat.session_id}"]').first).to_be_visible(
            timeout=30_000
        )
        expand_folder_to_all_pages(observer, project, _FOLDER_ROWS)
        page.wait_for_timeout(_SETTLE_MS)
        baseline_a = len(traffic_a.requests)
        baseline_b = len(traffic_b.requests)

        send_message(page, dispatch_prompt(chat.routing_token))
        reply = page.locator(_ASSISTANT_BUBBLE, has_text=chat.verification_code).first
        expect(reply).to_be_visible(timeout=_RELAY_TIMEOUT_MS)
        page.wait_for_timeout(_SETTLE_MS)

        reloads_a = folder_page_reloads(traffic_a.list_requests(baseline_a), project)
        reloads_b = folder_page_reloads(traffic_b.list_requests(baseline_b), project)
        assert reloads_a == [] and reloads_b == [], (
            "pushed updates re-requested loaded folder pages\n"
            f"orchestrator tab: {len(reloads_a)} after= reloads\n"
            f"{traffic_a.describe(baseline_a)}\n"
            f"landing tab: {len(reloads_b)} after= reloads\n"
            f"{traffic_b.describe(baseline_b)}"
        )
    finally:
        observer_ctx.close()
