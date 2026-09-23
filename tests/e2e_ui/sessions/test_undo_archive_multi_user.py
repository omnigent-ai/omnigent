"""Post-archive Undo journeys on an owner-scoped server.

Covers optimistic restore, stale archive frames, and unread suppression while
an authenticated WebSocket bridge controls update delivery.
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

# One four-second update rescan plus slack.
_UPDATES_TICK_TIMEOUT_S = 8.0


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """Run with owner-scoped session lists and filter tabs."""
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
    """Switch filters, retrying when Radix swallows a click during close animation."""
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
    """Park requests to simulate backend lag."""

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
        """Release parked requests that the page has not aborted."""
        held, self._held = self._held, []
        for route in held:
            with contextlib.suppress(Exception):
                route.continue_()


class _SessionUpdatesBridge:
    """Relay session updates with identity and test-controlled delivery.

    Python adds the header Chromium omits; the sync test thread pumps frames.
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
    """Open an admin page, optionally routing its first update connection."""
    # The shared recording hook instruments only Playwright's async API.
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
    """Undo repaints owner-scoped rows while server list reads are parked."""
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
        pill = page.get_by_test_id("archive-undo-toast-item")
        expect(pill).to_be_visible()
        expect(pill).to_contain_text("Archived 2 sessions")

        # Undo must restore from cache while list reads lag.
        page.route("**/v1/sessions**", list_holds.hold_session_lists)
        pill.get_by_role("button", name="Undo").click()
        expect(pill).to_be_hidden(timeout=5_000)

        # The all-sessions cache retained the rows and repaints immediately.
        _select_filter(page, "all")
        expect(_link(page, session_a)).to_be_visible(timeout=5_000)
        expect(_link(page, session_b)).to_be_visible(timeout=5_000)

        # The owner-scoped cache must also repaint before any list read completes.
        _select_filter(page, "mine")
        expect(_link(page, session_a)).to_be_visible(timeout=4_000)
        expect(_link(page, session_b)).to_be_visible(timeout=4_000)
    finally:
        # Release reads and confirm the persisted result.
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
    """A stale archived frame cannot hide a row after Undo restores it."""
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

        # Hold the stream snapshot that observes the archived state.
        _archive_from_row(page, session_c)
        _wait_archived(server, session_c, True)
        pill = page.get_by_test_id("archive-undo-toast-item")
        expect(pill).to_be_visible()

        # Keep the row archived until the held stream captures that stale state.
        page.route("**/v1/sessions**", patch_holds.hold_patch_for(session_c))
        page.route("**/v1/sessions**", list_holds.hold_session_lists)
        pill.get_by_role("button", name="Undo").click()
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

        # Commit the unarchive, then deliver the older archived frame.
        patch_holds.release()
        _wait_archived(server, session_c, False)
        bridge.hold_from_server = False
        bridge.release_held()

        # The row must remain visible until a fresh stream tick arrives.
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
    """A restored row stays read while another unarchive in its batch is pending."""
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
        pill = page.get_by_test_id("archive-undo-toast-item")
        expect(pill).to_be_visible()
        expect(pill).to_contain_text("Archived 2 sessions")

        # Park E so D's update arrives while the batch remains pending.
        page.route("**/v1/sessions**", patch_holds.hold_patch_for(session_e))
        page.route("**/v1/sessions**", list_holds.hold_session_lists)
        pill.get_by_role("button", name="Undo").click()
        expect(_link(page, session_d)).to_be_visible(timeout=5_000)
        _wait_archived(server, session_d, False)

        # D's self-initiated timestamp bump must not light the unread dot.
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
