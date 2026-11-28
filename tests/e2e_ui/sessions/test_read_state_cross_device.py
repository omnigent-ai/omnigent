"""Browser e2e: reading a session on one client clears its unread state on the
user's other, already-open clients.

Two Chromium contexts stand in for one user's devices: a desktop browser and
the mobile app (Pixel 7 profile plus an Android bridge stub that records the
badge count pushed through ``setBadgeCount``).
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    Response,
    WebSocket,
    expect,
)

from tests.e2e_ui.conftest import (
    _create_runner_bound_session,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
)

_REPLY_DELAY_S = 4.0
_TURN_TIMEOUT_MS = 90_000
# The second client's list refreshes over WS /v1/sessions/updates or the
# connected-stream reconcile poll (60 s); allow one full interval plus slack.
_LIST_REFRESH_TIMEOUT_S = 75.0
_UNSEEN_DOT = '[data-testid="session-state-badge"][data-state="unseen"]'
_UNREAD_ROW = '[data-testid="inbox-unread"]'
_SIDEBAR = 'aside[aria-label="Conversations"]'

_ANDROID_SHELL_INIT_SCRIPT = """
window.__badgeCalls = [];
window.omnigentNative = {
  kind: "android",
  setBadgeCount: function (count, activation) {
    window.__badgeCalls.push({ count: count, activation: activation || null });
  },
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  onNativeInsets: function () { return function () {}; },
};
"""


@pytest.fixture
def three_sessions(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, list[str]]]:
    """Three runner-bound ``hello_world`` sessions, deleted on teardown.

    :param live_server: Spawned (or prepared) server fixture.
    :param tmp_path_factory: Pytest temp path factory (for a runner respawn log).
    :returns: ``(base_url, [session_id, ...])``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    ids: list[str] = []
    try:
        for _ in range(3):
            ids.append(_create_runner_bound_session(live_server, runner_id))
        yield live_server, ids
    finally:
        for sid in ids:
            # Best-effort cleanup must not mask the test result.
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{sid}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def _row(page: Page, session_id: str) -> Locator:
    """Locate the sidebar row (``<li>``) for *session_id* by its link."""
    return page.locator(f"{_SIDEBAR} li").filter(has=page.locator(f'a[href="/c/{session_id}"]'))


def _unread_dot(row: Locator) -> Locator:
    return row.locator(_UNSEEN_DOT)


def _open_session(page: Page, session_id: str) -> None:
    """Click the sidebar row for *session_id* and wait until its page is open.

    Rows re-sort and resize as replies land, so a click can hit a neighbouring
    row after Playwright's hit check; verify the URL and retry. The URL changes
    before the transcript re-binds, so also wait for the main pane to carry this
    session's id, otherwise a following read of ``.last`` can hit the previous
    session's transcript mid-swap.
    """
    link = _row(page, session_id).locator(f'a[href="/c/{session_id}"]')
    for attempt in range(3):
        link.click()
        try:
            expect(page).to_have_url(re.compile(rf"/c/{session_id}$"), timeout=5_000)
            expect(page.locator(f'main[data-session-id="{session_id}"]')).to_be_visible(
                timeout=5_000
            )
            return
        except AssertionError:
            if attempt == 2:
                raise


def _last_badge(page: Page) -> int | None:
    return page.evaluate("() => window.__badgeCalls.at(-1)?.count ?? null")


def _wait_badge(page: Page, count: int, timeout_ms: int) -> None:
    page.wait_for_function(
        "n => window.__badgeCalls.length > 0 && window.__badgeCalls.at(-1).count === n",
        arg=count,
        timeout=timeout_ms,
    )


def _walk_rows(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict carrying a session ``id`` and ``viewer_last_seen``."""
    if isinstance(node, dict):
        if "id" in node and "viewer_last_seen" in node:
            yield node
        for value in node.values():
            yield from _walk_rows(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_rows(value)


@dataclass
class _ListObserver:
    """Highest per-session ``viewer_last_seen`` a client received from the server."""

    seen: dict[str, int] = field(default_factory=dict)

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)
        page.on("websocket", self._on_websocket)

    def _ingest(self, payload: Any) -> None:
        for row in _walk_rows(payload):
            value = row.get("viewer_last_seen")
            if isinstance(value, int):
                self.seen[row["id"]] = max(self.seen.get(row["id"], 0), value)

    def _on_response(self, response: Response) -> None:
        if response.request.method != "GET" or urlparse(response.url).path != "/v1/sessions":
            return
        try:
            payload = response.json()
        except Exception:
            # A body that is gone or not JSON is not a list refresh.
            return
        self._ingest(payload)

    def _on_websocket(self, ws: WebSocket) -> None:
        if not urlparse(ws.url).path.endswith("/v1/sessions/updates"):
            return

        def on_frame(payload: str | bytes) -> None:
            try:
                parsed = json.loads(payload)
            except Exception:
                # Non-JSON frames carry no rows.
                return
            self._ingest(parsed)

        ws.on("framereceived", on_frame)

    def synced(self, session_ids: list[str], floor: int) -> bool:
        return all(self.seen.get(sid, 0) >= floor for sid in session_ids)


def _wait_for_list_refresh(
    page: Page, observer: _ListObserver, session_ids: list[str], floor: int
) -> bool:
    """Wait until the client has received the server's read-state for every session.

    Returns whether every session reached *floor* within the refresh window, so
    the caller can assert the mobile's own list — not some other path — carried
    the desktop read before checking that the UI cleared.
    """
    deadline = time.monotonic() + _LIST_REFRESH_TIMEOUT_S
    while time.monotonic() < deadline:
        if observer.synced(session_ids, floor):
            return True
        # Playwright event handlers only run while a Playwright call is pending.
        page.wait_for_timeout(500)
    return observer.synced(session_ids, floor)


def _send_and_leave(desktop: Page, session_id: str, text: str) -> None:
    """Open *session_id*, send *text*, and move to the Inbox before the reply lands."""
    _open_session(desktop, session_id)
    composer = desktop.get_by_label("Message the agent")
    expect(composer).to_be_enabled()
    composer.fill(text)
    composer.press("Enter")
    expect(
        desktop.locator('[data-testid="message-bubble"][data-role="user"]').filter(has_text=text)
    ).to_be_visible()
    desktop.locator(f'{_SIDEBAR} a[href="/inbox"]').first.click()
    expect(desktop).to_have_url(re.compile(r"/inbox$"))


def test_read_on_desktop_clears_unread_on_open_mobile_client(
    playwright: Playwright,
    browser: Browser,
    three_sessions: tuple[str, list[str]],
    mock_llm_server_url: str,
    output_path: str,
) -> None:
    """Reading sessions on desktop clears them on an already-open mobile client.

    Desktop makes three sessions unread on both clients, then opens two and uses
    "Mark as read" on the third. Once the mobile list has refreshed, and without
    a reload, it must show no Inbox "Unread" rows, no unread pill, badge 0 and
    no sidebar dots.

    :param playwright: Device registry for the phone profile.
    :param browser: Shared browser; two contexts stand in for two devices.
    :param three_sessions: ``(base_url, [ids])`` runner-bound sessions.
    :param mock_llm_server_url: Mock model to script delayed replies.
    :param output_path: Per-test artifact directory for evidence screenshots.
    """
    base_url, session_ids = three_sessions
    markers = {sid: f"xdev-{uuid.uuid4().hex[:8]}" for sid in session_ids}
    for i, sid in enumerate(session_ids, start=1):
        # Several copies per marker so a title-generation call can't drain the queue.
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": f"Task {i} finished: all tests green.", "delay": _REPLY_DELAY_S}] * 3,
            match=markers[sid],
        )
    artifacts = Path(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)

    mobile_ctx = browser.new_context(**playwright.devices["Pixel 7"])
    mobile_ctx.add_init_script(_ANDROID_SHELL_INIT_SCRIPT)
    desktop_ctx: BrowserContext | None = None
    try:
        desktop_ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        mobile = mobile_ctx.new_page()
        desktop = desktop_ctx.new_page()
        observer = _ListObserver()
        observer.attach(mobile)

        mobile.goto(f"{base_url}/?sidebar=open")
        desktop.goto(f"{base_url}/inbox")
        for sid in session_ids:
            expect(_row(desktop, sid)).to_be_visible(timeout=30_000)
            expect(_row(mobile, sid)).to_be_visible(timeout=30_000)
            expect(_unread_dot(_row(mobile, sid))).to_have_count(0)
        _wait_badge(mobile, 0, timeout_ms=30_000)

        for i, sid in enumerate(session_ids, start=1):
            _send_and_leave(desktop, sid, f"Finish task {i} and report. Marker: {markers[sid]}")

        for sid in session_ids:
            expect(_unread_dot(_row(desktop, sid))).to_be_visible(timeout=_TURN_TIMEOUT_MS)
            expect(_unread_dot(_row(mobile, sid))).to_be_visible(timeout=_TURN_TIMEOUT_MS)
        expect(desktop.locator(_UNREAD_ROW)).to_have_count(3)
        _wait_badge(mobile, 3, timeout_ms=30_000)
        mobile.locator(f'{_SIDEBAR} a[href="/inbox"]').first.tap()
        expect(mobile).to_have_url(re.compile(r"/inbox$"))
        expect(mobile.locator(_UNREAD_ROW)).to_have_count(3)
        expect(mobile.get_by_title("3 unread")).to_be_visible()
        mobile.screenshot(path=str(artifacts / "mobile-before-desktop-read.png"))

        read_floor = int(time.time())
        for sid in session_ids[:2]:
            _open_session(desktop, sid)
            expect(
                desktop.locator('[data-testid="message-bubble"][data-role="assistant"]').last
            ).to_be_visible(timeout=30_000)
            expect(_unread_dot(_row(desktop, sid))).to_have_count(0)
        third = _row(desktop, session_ids[2])
        third.hover()
        third.get_by_test_id("conversation-actions").click()
        desktop.get_by_test_id("mark-read-conversation").click()
        expect(_unread_dot(third)).to_have_count(0)
        desktop.locator(f'{_SIDEBAR} a[href="/inbox"]').first.click()
        expect(desktop.locator(_UNREAD_ROW)).to_have_count(0)
        desktop.screenshot(path=str(artifacts / "desktop-after-read.png"))

        synced = _wait_for_list_refresh(mobile, observer, session_ids, read_floor)
        print(f"mobile list carried desktop read-state: {synced} ({observer.seen})")
        mobile.screenshot(path=str(artifacts / "mobile-after-desktop-read.png"))
        print(f"mobile badge after desktop read: {_last_badge(mobile)}")

        # The mobile's own list must carry the desktop read, so a cleared badge
        # and rows below can only be the fix, not a dropped refresh.
        assert synced, f"mobile never received the desktop read-state: {observer.seen}"
        expect(mobile.locator(_UNREAD_ROW)).to_have_count(0, timeout=15_000)
        expect(mobile.get_by_title(re.compile(r"\bunread\b"))).to_have_count(0)
        _wait_badge(mobile, 0, timeout_ms=15_000)
        toggle = mobile.get_by_role("button", name="Open sidebar")
        if toggle.count() > 0:
            toggle.tap()
        for sid in session_ids:
            expect(_row(mobile, sid)).to_be_visible()
            expect(_unread_dot(_row(mobile, sid))).to_have_count(0)
    finally:
        mobile_ctx.close()
        if desktop_ctx is not None:
            desktop_ctx.close()
