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
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Page, Request, WebSocket, expect

from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle

# The sidebar watches its loaded first page plus the route id, so a session must
# lie beyond that page for its bare id to be absent from the watch-set.
SIDEBAR_PAGE_SIZE = 30
UPDATES_WS_PATH = "/v1/sessions/updates"
PUSH_DELIVERY_TIMEOUT_MS = 20_000


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


@pytest.fixture
def off_list_session(live_server: str, runner_id: str) -> Iterator[tuple[str, str, str]]:
    """Yield ``(base_url, target_id, visible_id)``: ``target_id`` is older than a
    full sidebar page of fillers, so the sidebar's first page omits it;
    ``visible_id`` is the newest filler, shown at the top."""
    bundle = _build_hello_world_bundle()

    def _create(title: str) -> str:
        resp = post_session_bundle(httpx.post, f"{live_server}/v1/sessions", bundle, timeout=30.0)
        resp.raise_for_status()
        session_id = resp.json()["session_id"]
        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0
        ).raise_for_status()
        return session_id

    created: list[str] = []
    try:
        target = _create("off-list target")
        created.append(target)
        bind_session_runner(httpx.patch, live_server, target, runner_id, timeout=10.0)
        # The list sorts at second resolution; the fillers must sort newer.
        time.sleep(1.2)
        for index in range(SIDEBAR_PAGE_SIZE):
            created.append(_create(f"sidebar filler {index + 1:02d}"))
        visible = created[-1]
        bind_session_runner(httpx.patch, live_server, visible, runner_id, timeout=10.0)

        page1 = httpx.get(
            f"{live_server}/v1/sessions?visibility=all&limit={SIDEBAR_PAGE_SIZE}", timeout=10.0
        ).json()
        page1_ids = {row["id"] for row in page1["data"]}
        assert target not in page1_ids and visible in page1_ids, (
            "precondition: target must be off the sidebar's first page and the renamed row on it"
        )
        yield live_server, target, visible
    finally:
        for session_id in created:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def _watch_updates_socket(page: Page) -> dict[str, Any]:
    """Count opens/closes of the sidebar's updates WebSocket and record its frames."""
    events: dict[str, Any] = {"opens": 0, "closes": 0, "watch_frames": [], "received_types": []}

    def _frame(payload: object) -> dict[str, Any]:
        if isinstance(payload, dict):
            payload = payload.get("payload", "")
        try:
            parsed = json.loads(payload)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _on_websocket(ws: WebSocket) -> None:
        if UPDATES_WS_PATH not in ws.url:
            return
        events["opens"] += 1
        ws.on("framesent", lambda payload: events["watch_frames"].append(_frame(payload)))
        ws.on(
            "framereceived",
            lambda payload: events["received_types"].append(_frame(payload).get("type")),
        )
        ws.on("close", lambda _ws: events.__setitem__("closes", events["closes"] + 1))

    page.on("websocket", _on_websocket)
    return events


@pytest.mark.compat_smoke
def test_conv_prefixed_link_keeps_sidebar_updates_live(
    browser: Browser,
    off_list_session: tuple[str, str, str],
) -> None:
    """Opening an off-list session by its legacy ``conv_<hex>`` link keeps the
    sidebar's updates stream alive, so another client's rename still streams in.

    The CLI prints links as ``/c/conv_<hex>`` and the SPA puts that spelling
    into its watch-set verbatim; with the session off the first page, no bare
    twin accompanies it. A server that files watched rows under the bare id
    read back from the database crash-loops the stream, and the rename only
    arrives via the 45-60 s HTTP fallback.
    """
    base_url, target, visible = off_list_session
    link_id = f"conv_{target}"
    marker = f"renamed-{uuid.uuid4().hex[:8]}"
    row = f'a[href="/c/{visible}"]'

    context = browser.new_context()
    try:
        page = context.new_page()
        events = _watch_updates_socket(page)
        page.goto(f"{base_url}/c/{link_id}")
        expect(page.locator(row)).to_be_visible()
        # Let a crash-looping stream show itself before the rename.
        page.wait_for_timeout(5_000)

        httpx.patch(
            f"{base_url}/v1/sessions/{visible}", json={"title": marker}, timeout=10.0
        ).raise_for_status()

        def _summary() -> str:
            watched = [
                set(frame.get("session_ids", []))
                for frame in events["watch_frames"]
                if frame.get("type") == "watch"
            ]
            in_every_watch = bool(watched) and all(link_id in ids for ids in watched)
            return (
                f"opens={events['opens']} closes={events['closes']} "
                f"received={events['received_types']} watch_frames={len(watched)} "
                f"link_id_in_every_watch={in_every_watch}"
            )

        try:
            expect(page.locator(row)).to_contain_text(marker, timeout=PUSH_DELIVERY_TIMEOUT_MS)
        except AssertionError as exc:
            raise AssertionError(
                f"rename never reached the sidebar row over the stream; {_summary()}"
            ) from exc
        assert "snapshot" in events["received_types"], _summary()
        assert events["closes"] == 0, _summary()
        assert any(link_id in frame.get("session_ids", []) for frame in events["watch_frames"]), (
            f"the SPA never watched the conv_-prefixed id; {_summary()}"
        )
        # Hold the delivered title so a recording of this journey shows it.
        page.wait_for_timeout(1_500)
    finally:
        context.close()
