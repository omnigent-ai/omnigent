"""E2E: opening the session on a phone must not shrink the desktop's terminal pane.

xterm paints to a canvas, not the DOM, so each tab's grid is read from its real
attach WebSocket: the ``resize`` frames give its columns, and the bytes the
desktop tab receives are replayed through a VT emulator (``pyte``) to measure
how wide the TUI is actually drawn.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pyte
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, WebSocket, expect

from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode

_DESKTOP_VIEWPORT = {"width": 1400, "height": 900}
# iPhone-class portrait viewport, below the Tailwind ``md`` breakpoint so the
# mobile header (kebab-folded Chat/Terminal switch) renders.
_MOBILE_VIEWPORT = {"width": 390, "height": 844}
_IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

# claude-native auto-launch + first-run pre-accept + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
_MAIN_TERMINAL = '[data-testid="main-terminal-view"]'
_TERMINAL_VIEW = f'{_MAIN_TERMINAL} [data-testid="terminal-view"]'
_ATTACH_URL_RE = re.compile(r"/resources/terminals/[^/]+/attach")
# tmux applies ``refresh-client -C`` synchronously; the TUI redraw follows
# within a second. Budget for a slow CI box.
_SETTLE_TIMEOUT_S = 8.0
_FRAME_CHARS = "─━═╭╮╰╯│┃"


@dataclass
class _AttachObserver:
    """Capture one tab's terminal-attach WebSocket traffic."""

    urls: list[str] = field(default_factory=list)
    resizes: list[tuple[int, int]] = field(default_factory=list)
    received: bytearray = field(default_factory=bytearray)
    _active: WebSocket | None = field(default=None, init=False)

    def watch(self, page: Page) -> None:
        page.on("websocket", self._on_websocket)

    def _on_websocket(self, ws: WebSocket) -> None:
        if not _ATTACH_URL_RE.search(ws.url):
            return
        # A reattach opens a fresh socket with its own grid; drop the previous
        # socket's captures and ignore its late frames so screen_lines() replays
        # only the live connection even if the old socket overlaps the new one.
        self.urls.append(ws.url)
        self.resizes.clear()
        self.received.clear()
        self._active = ws
        ws.on("framesent", lambda payload: self._on_sent(ws, payload))
        ws.on("framereceived", lambda payload: self._on_received(ws, payload))

    def _on_sent(self, ws: WebSocket, payload: str | bytes) -> None:
        if ws is not self._active or isinstance(payload, bytes):
            return
        try:
            ctl = json.loads(payload)
        except ValueError:
            return
        if isinstance(ctl, dict) and ctl.get("type") == "resize":
            self.resizes.append((int(ctl["cols"]), int(ctl["rows"])))

    def _on_received(self, ws: WebSocket, payload: str | bytes) -> None:
        if ws is self._active and isinstance(payload, bytes):
            self.received.extend(payload)

    @property
    def read_only(self) -> bool | None:
        if not self.urls:
            return None
        return "read_only=true" in self.urls[-1]

    @property
    def cols_rows(self) -> tuple[int, int]:
        assert self.resizes, "tab never sent a resize frame"
        return self.resizes[-1]

    def screen_lines(self) -> list[str]:
        """Replay every byte the tab received through a VT emulator at its grid."""
        cols, rows = self.cols_rows
        screen = pyte.Screen(cols, rows)
        stream = pyte.ByteStream(screen)
        stream.feed(bytes(self.received))
        return [line.rstrip() for line in screen.display]


def _drawn_frame_width(lines: list[str]) -> int:
    """Widest row of the TUI's box frame, i.e. how many columns the TUI uses."""
    widths = [len(line) for line in lines if any(ch in _FRAME_CHARS for ch in line)]
    return max(widths, default=0)


def _wait_for_frame(
    page: Page, observer: _AttachObserver, *, min_width: int, timeout_s: float
) -> int:
    """Pump the page until the TUI has drawn at least ``min_width`` columns.

    The sync Playwright API only dispatches the attach-WebSocket callbacks while
    the main greenlet is inside a Playwright call, so the wait yields through
    ``page.wait_for_timeout`` (not ``time.sleep``) to let received frames arrive.
    """
    deadline = time.monotonic() + timeout_s
    width = 0
    while time.monotonic() < deadline:
        if observer.resizes:
            width = _drawn_frame_width(observer.screen_lines())
            if width >= min_width:
                return width
        page.wait_for_timeout(500)
    return width


def _min_frame_width_over(page: Page, observer: _AttachObserver, *, settle_s: float) -> int:
    """Smallest TUI frame width seen while pumping the desktop for ``settle_s``.

    Proving the pane did *not* shrink needs a bounded wait: a regression resizes
    the shared window the moment the phone attaches, so we sample across the
    settle (catching a shrink even if it later recovers) rather than reading once.
    ``page.wait_for_timeout`` yields so received frames dispatch in the sync API.
    """
    deadline = time.monotonic() + settle_s
    widths: list[int] = []
    while True:
        # Skip samples while a reconnect has momentarily cleared the grid, so a
        # mid-settle reattach can't trip the cols_rows assert with no verdict.
        if observer.resizes:
            widths.append(_drawn_frame_width(observer.screen_lines()))
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(250)
    return min(widths, default=0)


def _wait_for_resize(page: Page, observer: _AttachObserver, *, timeout_s: float) -> None:
    """Wait until the tab records its initial resize frame (sent async on WS open)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if observer.resizes:
            return
        page.wait_for_timeout(100)
    raise TimeoutError(f"attach WebSocket sent no resize frame within {timeout_s:.0f}s")


def _show_terminal_view(page: Page) -> None:
    """Bring up the Terminal view: header segment on desktop, kebab item on mobile."""
    if page.locator(f'{_MAIN_TERMINAL}[data-visible="true"]').count():
        return
    if page.get_by_test_id("view-mode-toggle").count():
        _select_view_mode(page, "Terminal")
        return
    trigger = page.get_by_test_id("header-conversation-actions").or_(
        page.get_by_test_id("session-actions-menu")
    )
    expect(trigger).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
    trigger.click()
    page.get_by_test_id("view-mode-menu-terminal").click()


def _open_session_terminal(context: BrowserContext, url: str, observer: _AttachObserver) -> Page:
    page = context.new_page()
    observer.watch(page)
    page.goto(url)
    expect(page.locator(_MAIN_TERMINAL)).to_be_attached(timeout=_TERMINAL_READY_TIMEOUT_MS)
    _show_terminal_view(page)
    expect(page.locator(f'{_MAIN_TERMINAL}[data-visible="true"]')).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    # The resize frame is delivered asynchronously over CDP, so wait for the
    # observer to record it before callers read ``cols_rows``.
    _wait_for_resize(page, observer, timeout_s=_SETTLE_TIMEOUT_S)
    return page


def _dump(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.timeout(600)
def test_phone_attach_keeps_desktop_terminal_pane_width(
    browser: Browser,
    native_claude_mock_session: tuple[str, str],
    output_path: str,
) -> None:
    """The desktop TUI keeps its columns while a phone views the session, and recovers after."""
    base_url, session_id = native_claude_mock_session
    url = f"{base_url}/c/{session_id}"
    print(f"[shared-pane] session_id={session_id}")
    artifacts = Path(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)

    desktop_obs = _AttachObserver()
    phone_obs = _AttachObserver()
    desktop = browser.new_context(viewport=_DESKTOP_VIEWPORT)
    phone: BrowserContext | None = None
    try:
        desktop_page = _open_session_terminal(desktop, url, desktop_obs)
        desktop_cols, _ = desktop_obs.cols_rows
        baseline = _wait_for_frame(
            desktop_page, desktop_obs, min_width=int(desktop_cols * 0.8), timeout_s=60
        )
        _dump(artifacts / "desktop-before-phone.txt", desktop_obs.screen_lines())
        desktop_page.screenshot(path=str(artifacts / "desktop-before-phone.png"))
        print(
            f"[shared-pane] desktop attach read_only={desktop_obs.read_only} "
            f"cols={desktop_cols} drawn_frame_width={baseline}"
        )
        assert desktop_obs.read_only is False
        assert baseline >= desktop_cols * 0.8, "Claude TUI never drew a full-width frame"

        phone = browser.new_context(
            viewport=_MOBILE_VIEWPORT,
            is_mobile=True,
            has_touch=True,
            user_agent=_IPHONE_UA,
        )
        phone_page = _open_session_terminal(phone, url, phone_obs)
        phone_cols, _ = phone_obs.cols_rows
        phone_page.screenshot(path=str(artifacts / "phone-terminal-view.png"))
        print(f"[shared-pane] phone attach read_only={phone_obs.read_only} cols={phone_cols}")
        assert phone_obs.read_only is False
        assert phone_cols < desktop_cols * 0.5

        with_phone = _min_frame_width_over(desktop_page, desktop_obs, settle_s=_SETTLE_TIMEOUT_S)
        _dump(artifacts / "desktop-with-phone.txt", desktop_obs.screen_lines())
        desktop_page.screenshot(path=str(artifacts / "desktop-with-phone.png"))
        print(f"[shared-pane] desktop while phone attached: min drawn_frame_width={with_phone}")
        assert with_phone >= baseline * 0.9, (
            f"desktop pane shrank from {baseline} to {with_phone} columns while a "
            f"{phone_cols}-column phone was attached"
        )

        phone.close()
        phone = None
        after_close = _min_frame_width_over(desktop_page, desktop_obs, settle_s=_SETTLE_TIMEOUT_S)
        _dump(artifacts / "desktop-after-phone-closed.txt", desktop_obs.screen_lines())
        desktop_page.screenshot(path=str(artifacts / "desktop-after-phone-closed.png"))
        print(f"[shared-pane] desktop after phone closed: min drawn_frame_width={after_close}")
        assert after_close >= baseline * 0.9, (
            f"desktop pane did not recover after the phone closed: "
            f"{after_close} vs baseline {baseline}"
        )
    finally:
        # Close the phone first and swallow its errors so a failing close
        # cannot skip the desktop teardown and leak a browser context.
        if phone is not None:
            with contextlib.suppress(Exception):
                phone.close()
        desktop.close()
