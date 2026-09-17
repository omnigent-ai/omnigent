"""Browser e2e for post-archive Undo against a multi-user (owner-scoped) server.

``tests/e2e_ui/sessions/test_sidebar_undo_archive.py`` proves the archive →
Undo round-trip on the shared single-user server, where every row's ``owner``
is null and the ownership checks pass vacuously. On a real multi-user server
(header-auth, like a Databricks Apps / SSO-proxy install) each row carries the
creator's user id in ``owner``, the sidebar's "My sessions" tab renders from an
owner-scoped list cache, and the update stream / list reads can lag writes.
That deployment shape breaks Undo in ways the single-user suite can't see:

1. **"My sessions" doesn't restore optimistically.** Archiving evicts the row
   from the owner-scoped ("mine") cache outright (``violatesKnownMembership``),
   so Undo can only bring it back through ``insertNewRowsIntoPages`` — which,
   unlike the WS ``session_added`` path, is called without a ``viewerId``. The
   ownership check ``owner == null || owner === viewerId`` then fails for the
   viewer's OWN sessions and the "mine" cache is skipped: the restored rows
   only reappear after a server list read completes (seconds on a lagging
   deployment). The "All sessions" tab restores instantly.

2. **A stale update-stream frame un-restores the row.** The session-updates
   stream re-reads watched rows on an interval, so a rapid archive → Undo can
   deliver a ``changed`` frame carrying ``archived: true`` *after* Undo already
   painted the row back. Nothing guards the merge against that stale flag (the
   archive tombstone is cleared by the unarchive, and the recently-created
   keep-alive skips rows whose cached copy reads archived), so the restored row
   vanishes until the next tick/refetch brings it back — the flicker.

3. **Restored rows briefly read as unread.** Unarchiving bumps ``updated_at``
   server-side (and archiving pruned the per-viewer read-state), but Undo only
   calls ``markConversationSeen`` after ALL unarchive PATCHes settle. Any row
   whose refreshed ``updated_at`` reaches the client before the whole batch
   settles lights the unread dot for a session the viewer just restored
   themselves.

The tests run against a dedicated multi-user server (the shared ``live_server``
is single-user). Chromium does not attach ``extra_http_headers`` to WebSocket
handshakes, so the sessions-updates socket cannot authenticate the way plain
fetches do; where a test needs the live stream it bridges the socket through a
Python WebSocket client that attaches the identity header — standing in for
the SSO front door that forwards identity on a real deployment — which also
gives the test frame-level delivery control to reproduce the lag
deterministically.
"""

from __future__ import annotations

import contextlib
import json as _json
import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Locator, Page, Route, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)
from tests.e2e_ui.conftest import _build_hello_world_bundle

_ADMIN_HEADERS = {"X-Forwarded-Email": ADMIN_EMAIL}

# One interval of the server's session-updates rescan loop
# (_SESSION_UPDATES_RESCAN_INTERVAL_S = 4.0) plus slack: how long a test waits
# for the stream to emit a frame reflecting a just-committed write.
_UPDATES_TICK_TIMEOUT_S = 8.0


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A NON-single-user server, so list rows carry ``owner`` and the sidebar
    shows the ownership-scoped filter tabs."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_undo_archive_multi_user")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


def _create_admin_session(server: MultiUserServer, title: str) -> str:
    """Create an admin-owned hello_world session named *title*; return its id."""
    bundle = _build_hello_world_bundle()
    create = httpx.post(
        f"{server.base_url}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=_ADMIN_HEADERS,
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    rename = httpx.patch(
        f"{server.base_url}/v1/sessions/{session_id}",
        json={"title": title},
        headers=_ADMIN_HEADERS,
        timeout=30.0,
    )
    rename.raise_for_status()
    return session_id


def _wait_archived(server: MultiUserServer, session_id: str, want: bool) -> None:
    """Poll the store until *session_id* reports ``archived == want``."""
    deadline = time.monotonic() + 15.0
    seen: bool | None = None
    while time.monotonic() < deadline:
        resp = httpx.get(
            f"{server.base_url}/v1/sessions/{session_id}",
            headers=_ADMIN_HEADERS,
            timeout=10.0,
        )
        seen = resp.json()["archived"] if resp.status_code == 200 else None
        if seen is want:
            return
        time.sleep(0.2)
    raise AssertionError(f"session {session_id} should report archived={want}, got {seen}")


def _link(page: Page, session_id: str) -> Locator:
    """The sidebar link for *session_id* (present iff the row is listed)."""
    return page.locator(f'a[href="/c/{session_id}"]')


def _row(page: Page, session_id: str) -> Locator:
    """The sidebar row (``<li>``) for *session_id*."""
    return page.locator("li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _archive_from_row(page: Page, session_id: str) -> None:
    """Open a sidebar row's kebab and click Archive."""
    row = _row(page, session_id)
    expect(row).to_be_visible()
    row.hover()
    row.get_by_test_id("conversation-actions").click()
    page.get_by_test_id("archive-conversation").click()
    expect(_link(page, session_id)).to_have_count(0)


def _select_filter(page: Page, value: str) -> None:
    """Switch the sidebar's session filter (``all`` / ``mine`` / ...).

    A click that lands while the previous menu instance is still animating
    closed can be swallowed (Radix treats it as the closing toggle), so retry
    the trigger once if the menu didn't open.
    """
    trigger = page.get_by_test_id("session-filter")
    item = page.get_by_test_id(f"session-filter-{value}")
    trigger.click()
    try:
        item.wait_for(state="visible", timeout=3_000)
    except PlaywrightTimeoutError:
        trigger.click()
        item.wait_for(state="visible", timeout=5_000)
    item.click()
    expect(item).to_be_hidden(timeout=5_000)


class _HeldRoutes:
    """Park matching requests unanswered until :meth:`release` — the client-
    observable equivalent of a backend whose reads/writes lag."""

    def __init__(self) -> None:
        self._held: list[Route] = []

    def hold_session_lists(self, route: Route) -> None:
        """Route handler: park every ``GET /v1/sessions`` list read."""
        req = route.request
        if req.method == "GET" and urlsplit(req.url).path.endswith("/v1/sessions"):
            self._held.append(route)
            return
        route.fallback()

    def hold_patch_for(self, session_id: str) -> Callable[[Route], None]:
        """Route handler: park ``PATCH /v1/sessions/{session_id}`` only."""

        def handler(route: Route) -> None:
            req = route.request
            if req.method == "PATCH" and urlsplit(req.url).path.endswith(
                f"/v1/sessions/{session_id}"
            ):
                self._held.append(route)
                return
            route.fallback()

        return handler

    def release(self) -> None:
        """Let every parked request proceed (best-effort; the page may have
        aborted some while they were parked)."""
        held, self._held = self._held, []
        for route in held:
            with contextlib.suppress(Exception):
                route.continue_()


class _SessionUpdatesBridge:
    """Authenticated relay for the ``/v1/sessions/updates`` socket.

    Chromium omits ``extra_http_headers`` from WebSocket handshakes, so on a
    header-auth multi-user server the page's updates socket is rejected (403)
    and the sidebar loses live updates entirely. On a real deployment the SSO
    front door injects the identity header on every request, WebSocket
    handshakes included; this bridge reproduces that: the page's socket is
    served by the test, which relays frames over a Python WebSocket connection
    that carries ``X-Forwarded-Email``.

    Relaying also hands the test the server→client frame schedule. With
    ``hold_from_server`` set, frames queue in :attr:`held` instead of reaching
    the page — the update stream "lagging" — until :meth:`release_held`
    delivers them. Frames only move server→page on :meth:`pump`, which must be
    called from the test thread (Playwright sync objects are not thread-safe).
    """

    def __init__(self, server: MultiUserServer) -> None:
        self._ws_url = f"{server.base_url}/v1/sessions/updates".replace("http", "ws", 1)
        self._to_server: queue.Queue[str | bytes | None] = queue.Queue()
        self._from_server: deque[str | bytes] = deque()
        self._lock = threading.Lock()
        self._page_ws = None
        self._closed = threading.Event()
        self.connect_error: str | None = None
        self.hold_from_server = False
        self.held: list[str | bytes] = []

    def handler(self, ws) -> None:  # ws: playwright WebSocketRoute
        """``page.route_web_socket`` handler: serve the page's socket."""
        self._page_ws = ws
        ws.on_message(self._to_server.put)
        ws.on_close(lambda code=None, reason=None: self.close())
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        from websockets.sync.client import connect as ws_connect

        try:
            conn = ws_connect(
                self._ws_url,
                additional_headers=_ADMIN_HEADERS,
                open_timeout=15,
            )
        except Exception as exc:  # surfaced by the test's pump loop
            self.connect_error = f"{type(exc).__name__}: {exc}"
            return
        try:

            def send_loop() -> None:
                while True:
                    msg = self._to_server.get()
                    if msg is None:
                        return
                    conn.send(msg)

            threading.Thread(target=send_loop, daemon=True).start()
            while not self._closed.is_set():
                try:
                    msg = conn.recv(timeout=0.25)
                except TimeoutError:
                    continue
                except Exception:
                    return
                with self._lock:
                    self._from_server.append(msg)
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def pump(self) -> None:
        """Deliver (or hold) any frames the relay has received so far."""
        assert self.connect_error is None, (
            f"authenticated updates-stream relay failed to connect: {self.connect_error}"
        )
        with self._lock:
            frames = list(self._from_server)
            self._from_server.clear()
        for frame in frames:
            if self.hold_from_server:
                self.held.append(frame)
            else:
                self._page_ws.send(frame)

    def release_held(self) -> None:
        """Deliver every held frame to the page, oldest first."""
        held, self.held = self.held, []
        for frame in held:
            self._page_ws.send(frame)

    def close(self) -> None:
        self._closed.set()
        self._to_server.put(None)


def _new_admin_page(
    browser: Browser,
    server: MultiUserServer,
    *,
    sidebar_filter: str,
    updates_bridge: _SessionUpdatesBridge | None = None,
) -> tuple[BrowserContext, Page]:
    """An admin-identified page on the sidebar's *sidebar_filter* tab.

    When *updates_bridge* is given, the sessions-updates socket is routed
    through it (installed before navigation, so the app's first connection
    attempt is already bridged).
    """
    # Film the journey when the recording harness asks for it. The conftest's
    # OMNIGENT_E2E_RECORD_DIR hook instruments the async API only, and these
    # tests drive the sync `browser` fixture, so honor the env var directly.
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    ctx = browser.new_context(
        extra_http_headers=_ADMIN_HEADERS,
        **({"record_video_dir": record_dir} if record_dir else {}),
    )
    ctx.add_init_script(
        f'window.localStorage.setItem("omnigent:session-filter", {_json.dumps(sidebar_filter)})'
    )
    page = ctx.new_page()
    if updates_bridge is not None:
        page.route_web_socket(lambda url: "/v1/sessions/updates" in url, updates_bridge.handler)
    page.goto(f"{server.public_url}/")
    expect(page.get_by_test_id("session-filter")).to_be_visible(timeout=30_000)
    return ctx, page


def _pump_until(
    page: Page,
    bridge: _SessionUpdatesBridge,
    timeout_s: float,
    *,
    predicate: Callable[[], bool] | None = None,
    probe: Callable[[], None] | None = None,
) -> bool:
    """Pump the bridge (and *probe*) until *predicate* holds or time runs out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        bridge.pump()
        if probe is not None:
            probe()
        if predicate is not None and predicate():
            return True
        page.wait_for_timeout(40)
    return predicate() if predicate is not None else False


def _frame_reports_archived(frame: str | bytes, session_id: str, archived: bool) -> bool:
    """True when a stream frame carries *session_id* with the given flag."""
    try:
        payload = _json.loads(frame)
    except (ValueError, TypeError):
        return False
    return any(
        item.get("id") == session_id and item.get("archived", False) is archived
        for item in payload.get("items") or []
    )


def test_undo_restores_optimistically_on_my_sessions_tab(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """Undo must repaint restored rows on "My sessions" without a server trip.

    Journey (all on the default "My sessions" tab, as the sessions' owner):
    archive sessions A and B from their row menus — both merge into one Undo
    pill — then, with every ``GET /v1/sessions`` list read parked (the lagging
    backend), click Undo.

    Archiving evicted both rows from the owner-scoped "mine" cache, so only
    Undo's optimistic re-insert can repaint them there — and that insert is
    exactly what skips owner-scoped caches when no viewer id is supplied. On
    the broken build the rows come back instantly on "All sessions" (whose
    cache keeps archived rows and needs only the flag flip) but stay missing
    from "My sessions" until a list read completes — which on a real deployment
    means seconds of the user wondering whether Undo worked. The final
    assertions are the regression guard: A and B must be visible on
    "My sessions" while list reads are still parked.
    """
    server = multi_user_server
    session_a = _create_admin_session(server, "Undo repro A (mine-tab restore)")
    session_b = _create_admin_session(server, "Undo repro B (mine-tab restore)")
    list_holds = _HeldRoutes()
    ctx, page = _new_admin_page(browser, server, sidebar_filter="mine")
    try:
        expect(_link(page, session_a)).to_be_visible(timeout=30_000)
        expect(_link(page, session_b)).to_be_visible(timeout=30_000)

        # Archive both; the second merges into the first's pill.
        _archive_from_row(page, session_a)
        _archive_from_row(page, session_b)
        _wait_archived(server, session_a, True)
        _wait_archived(server, session_b, True)
        pill = page.get_by_test_id("archive-undo-toast")
        expect(pill).to_be_visible()
        expect(pill).to_contain_text("Archived 2 sessions.")

        # Park every list read: the client is now on its own, exactly as it is
        # inside a real deployment's index-lag window. Undo's restore must not
        # depend on a fetch completing.
        page.route("**/v1/sessions**", list_holds.hold_session_lists)
        pill.get_by_test_id("archive-undo-button").click()
        expect(pill).to_be_hidden(timeout=5_000)

        # Control: the "All sessions" tab restored both rows instantly (its
        # cache kept the archived rows, so the optimistic flag flip repaints
        # them) — the undo itself worked.
        _select_filter(page, "all")
        expect(_link(page, session_a)).to_be_visible(timeout=5_000)
        expect(_link(page, session_b)).to_be_visible(timeout=5_000)

        # THE BUG: back on "My sessions", the same rows must be there just as
        # instantly — no list read has completed, so only the optimistic
        # restore can have put them there. On the broken build this times out:
        # the re-insert skipped the owner-scoped cache, and the user watches
        # their restored sessions stay missing from "My sessions" until the
        # backend catches up.
        _select_filter(page, "mine")
        expect(_link(page, session_a)).to_be_visible(timeout=4_000)
        expect(_link(page, session_b)).to_be_visible(timeout=4_000)
    finally:
        # Let the parked reads land so the journey's tail is the (slow) restore
        # completing, then confirm the store agrees before tearing down.
        list_holds.release()
        try:
            expect(_link(page, session_a)).to_be_visible(timeout=15_000)
            _wait_archived(server, session_a, False)
            _wait_archived(server, session_b, False)
        except (AssertionError, PlaywrightTimeoutError):
            pass
        ctx.close()


def test_rapid_archive_undo_does_not_flicker_restored_row(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """A stale update-stream frame must not un-restore a just-undone archive.

    Journey (on "All sessions", where Undo's repaint works): archive session C,
    click Undo about a second later — the row pops straight back — while the
    sessions-updates stream runs a beat behind. Its rescan read the row between
    the archive and the unarchive, so the frame it delivers carries
    ``archived: true`` and lands only *after* Undo already restored the row —
    routine on a shared deployment, reproduced here by holding the relay's
    server→client frames (and the unarchive PATCH) until that stale frame is
    captured. List reads are parked for the window so the sidebar shows purely
    what the client believes.

    The restored row must stay put. On the broken build the stale frame's merge
    re-flags the row archived and it vanishes from the sidebar — the appear →
    disappear → reappear flicker — until the stream's next tick reports the
    unarchive and brings it back.
    """
    server = multi_user_server
    session_c = _create_admin_session(server, "Undo repro C (rapid archive undo)")
    bridge = _SessionUpdatesBridge(server)
    patch_holds = _HeldRoutes()
    list_holds = _HeldRoutes()
    ctx, page = _new_admin_page(browser, server, sidebar_filter="all", updates_bridge=bridge)
    try:
        expect(_link(page, session_c)).to_be_visible(timeout=30_000)
        # Let the stream hand-shake and deliver its snapshot, then lag it.
        _pump_until(page, bridge, 1.0)
        bridge.hold_from_server = True

        # Archive C; the write commits, and the stream's next rescan (held by
        # the relay) will report it archived.
        _archive_from_row(page, session_c)
        _wait_archived(server, session_c, True)
        pill = page.get_by_test_id("archive-undo-toast")
        expect(pill).to_be_visible()

        # Park C's unarchive PATCH so the rescan tick reads the row while it is
        # still archived — the stale frame a laggy stream delivers after Undo —
        # and park list reads so a refetch can't repaint ahead of the stream.
        page.route("**/v1/sessions**", patch_holds.hold_patch_for(session_c))
        page.route("**/v1/sessions**", list_holds.hold_session_lists)
        pill.get_by_test_id("archive-undo-button").click()
        # Undo's optimistic restore: C pops straight back.
        expect(_link(page, session_c)).to_be_visible(timeout=5_000)

        # Capture the stale archived:true frame from the (held) stream.
        got_stale_frame = _pump_until(
            page,
            bridge,
            _UPDATES_TICK_TIMEOUT_S,
            predicate=lambda: any(
                _frame_reports_archived(f, session_c, True) for f in bridge.held
            ),
        )
        assert got_stale_frame, (
            "updates stream never reported the archived row; cannot exercise the "
            "stale-frame window"
        )

        # Let the unarchive commit (the user's Undo has long since happened),
        # then deliver the stale frame — the moment the laggy stream catches up
        # with the page.
        patch_holds.release()
        _wait_archived(server, session_c, False)
        bridge.hold_from_server = False
        bridge.release_held()

        # THE BUG: the restored row must not vanish. Sample its presence while
        # the stream's next tick (which reports archived: false) makes its way
        # over; on the broken build the stale frame re-archives the row and it
        # disappears for seconds before that tick restores it.
        vanished: list[float] = []
        recovered: list[float] = []
        start = time.monotonic()

        def probe() -> None:
            if _link(page, session_c).count() == 0:
                vanished.append(time.monotonic() - start)
            elif vanished:
                recovered.append(time.monotonic() - start)

        _pump_until(page, bridge, _UPDATES_TICK_TIMEOUT_S, probe=probe)
        assert not vanished, (
            "restored row flickered away after Undo: a stale updates frame "
            f"re-archived it (hidden from {vanished[0]:.2f}s to {vanished[-1]:.2f}s; "
            + (
                f"reappeared at {recovered[0]:.2f}s"
                if recovered
                else "still missing when sampling ended"
            )
            + ")"
        )
    finally:
        patch_holds.release()
        list_holds.release()
        bridge.close()
        with contextlib.suppress(AssertionError):
            _wait_archived(server, session_c, False)
        ctx.close()


def test_undo_does_not_light_unread_dot_on_restored_rows(
    browser: Browser,
    multi_user_server: MultiUserServer,
) -> None:
    """Rows the viewer just restored must not light the unread dot.

    Journey (on "All sessions", where Undo's repaint works): archive sessions D
    and E, click Undo. Each unarchive PATCH bumps the row's ``updated_at`` (and
    archiving pruned its per-viewer read-state), but Undo marks the rows seen
    only after the WHOLE batch of PATCHes settles. Park E's PATCH (one slow
    write in a batch — routine on a shared server): D's refreshed row then
    reaches the page — via the update stream's next tick — while the batch is
    still pending. On the broken build D, a session the viewer restored
    themselves with no new activity, lights the "New messages" dot.
    """
    server = multi_user_server
    session_d = _create_admin_session(server, "Undo repro D (unread dot)")
    session_e = _create_admin_session(server, "Undo repro E (unread dot)")
    bridge = _SessionUpdatesBridge(server)
    patch_holds = _HeldRoutes()
    list_holds = _HeldRoutes()
    ctx, page = _new_admin_page(browser, server, sidebar_filter="all", updates_bridge=bridge)
    try:
        expect(_link(page, session_d)).to_be_visible(timeout=30_000)
        expect(_link(page, session_e)).to_be_visible(timeout=30_000)
        _pump_until(page, bridge, 1.0)

        # No dot before the journey starts: the rows were seeded read-as-of-load.
        unseen_dot_d = _row(page, session_d).locator(
            '[data-testid="session-state-badge"][data-state="unseen"]'
        )
        expect(unseen_dot_d).to_have_count(0)

        _archive_from_row(page, session_d)
        _archive_from_row(page, session_e)
        _wait_archived(server, session_d, True)
        _wait_archived(server, session_e, True)
        pill = page.get_by_test_id("archive-undo-toast")
        expect(pill).to_be_visible()
        expect(pill).to_contain_text("Archived 2 sessions.")

        # One slow unarchive in the batch: park E's PATCH; D's commits at once.
        # List reads are parked too, so D's refreshed row arrives the way it
        # does on a live deployment — over the update stream.
        page.route("**/v1/sessions**", patch_holds.hold_patch_for(session_e))
        page.route("**/v1/sessions**", list_holds.hold_session_lists)
        pill.get_by_test_id("archive-undo-button").click()
        expect(_link(page, session_d)).to_be_visible(timeout=5_000)
        _wait_archived(server, session_d, False)

        # THE BUG: as soon as D's refreshed row (bumped updated_at) reaches the
        # page over the stream, its row lights the unread dot, because the
        # batch's mark-seen is still parked behind E.
        lit: list[float] = []
        start = time.monotonic()

        def probe() -> None:
            if unseen_dot_d.count() > 0:
                lit.append(time.monotonic() - start)

        _pump_until(page, bridge, _UPDATES_TICK_TIMEOUT_S, probe=probe)
        assert not lit, (
            "Undo lit the unread dot on a row the viewer restored themselves "
            f"(first seen {lit[0]:.2f}s after Undo)"
        )
    finally:
        patch_holds.release()
        list_holds.release()
        bridge.close()
        with contextlib.suppress(AssertionError):
            _wait_archived(server, session_e, False)
        ctx.close()
