"""Opening a session on a phone must not shrink the desktop's terminal pane.

xterm draws to a canvas, so widths are measured by replaying attach WebSocket bytes in pyte.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pyte
import pytest
from playwright.sync_api import Browser, Page, WebSocket, expect

from tests._helpers.browser import user_page
from tests._helpers.polling import poll_until
from tests.e2e_ui.messages.test_message_render_parity import _select_view_mode

_DESKTOP_VIEWPORT = {"width": 1280, "height": 800}
_PHONE_VIEWPORT = {"width": 390, "height": 844}
_IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
_TERMINAL_READY_TIMEOUT_MS = 120_000
_MAIN_TERMINAL = '[data-testid="main-terminal-view"]'
_TERMINAL_VIEW = f'{_MAIN_TERMINAL} [data-testid="terminal-view"]'
_ATTACH_URL_RE = re.compile(r"/resources/terminals/[^/]+/attach")
_HOLD_S = 10.0
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
        # A reattach starts a fresh grid; ignore the previous socket's late frames.
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
            message = json.loads(payload)
        except ValueError:
            return
        if isinstance(message, dict) and message.get("type") == "resize":
            self.resizes.append((int(message["cols"]), int(message["rows"])))

    def _on_received(self, ws: WebSocket, payload: str | bytes) -> None:
        if ws is self._active and isinstance(payload, bytes):
            self.received.extend(payload)

    def screen_lines(self) -> list[str]:
        if not self.resizes:
            return []
        cols, rows = self.resizes[-1]
        screen = pyte.Screen(cols, rows)
        pyte.ByteStream(screen).feed(bytes(self.received))
        return [line.rstrip() for line in screen.display]

    def drawn_width(self) -> int | None:
        """Widest row of the TUI's box frame, or None before the current attach draws one."""
        lines = self.screen_lines()
        return max(
            (len(line) for line in lines if any(ch in _FRAME_CHARS for ch in line)), default=None
        )


def _sample_drawn_width(
    page: Page, observer: _AttachObserver, seconds: float
) -> list[tuple[float, int | None]]:
    """Timestamped desktop frame widths; wait_for_timeout lets WS callbacks dispatch."""
    deadline = time.monotonic() + seconds
    samples = [(time.monotonic(), observer.drawn_width())]
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        samples.append((time.monotonic(), observer.drawn_width()))
    return samples


def _wait_terminal_connected(page: Page) -> None:
    expect(page.locator(f'{_MAIN_TERMINAL}[data-visible="true"]')).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    expect(page.locator(_TERMINAL_VIEW).last).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _session_terminal_listed(base_url: str, session_id: str) -> bool:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/terminals", timeout=10)
    return response.status_code == 200 and bool(response.json().get("data"))


@pytest.mark.timeout(600)
def test_phone_opening_session_keeps_desktop_terminal_width(
    browser: Browser,
    native_claude_mock_session: tuple[str, str],
    output_path: str,
) -> None:
    """The desktop TUI keeps its columns while a phone views the session, and after it leaves."""
    base_url, session_id = native_claude_mock_session
    session_url = f"{base_url}/c/{session_id}"
    poll_until(lambda: _session_terminal_listed(base_url, session_id), timeout_s=90)
    recording = bool(os.environ.get("OMNIGENT_E2E_RECORD_DIR"))
    artifacts = Path(output_path)
    artifacts.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    steps: list[dict[str, object]] = []
    report: dict[str, object] = {"session_id": session_id, "steps": steps}

    def step(
        name: str, samples: list[tuple[float, int | None]], phone: _AttachObserver
    ) -> list[int]:
        # A desktop reattach redraws from scratch; a window with no frame at all counts as 0.
        widths = [width for _, width in samples if width is not None] or [0]
        steps.append(
            {
                "step": name,
                "desktop_min_drawn_width": min(widths),
                "desktop_last_drawn_width": samples[-1][1] or 0,
                "phone_attach_urls": len(phone.urls),
                "phone_resizes": list(phone.resizes),
                "samples": [[round(at - started, 2), width] for at, width in samples],
            }
        )
        return widths

    desktop_obs = _AttachObserver()
    phone_obs = _AttachObserver()
    try:
        with user_page(
            browser,
            headers={},
            viewport=_DESKTOP_VIEWPORT,
            **({"record_video_size": _DESKTOP_VIEWPORT} if recording else {}),
        ) as desktop:
            report["desktop_page_opened_s"] = round(time.monotonic() - started, 2)
            desktop_obs.watch(desktop)
            desktop.goto(session_url)
            expect(desktop.get_by_test_id("view-mode-toggle")).to_be_visible(
                timeout=_TERMINAL_READY_TIMEOUT_MS
            )
            _select_view_mode(desktop, "Terminal")
            _wait_terminal_connected(desktop)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not (
                desktop_obs.resizes
                and (desktop_obs.drawn_width() or 0) >= desktop_obs.resizes[-1][0] * 0.8
            ):
                desktop.wait_for_timeout(500)
            assert desktop_obs.resizes, "desktop terminal never sent its size"
            desktop_cols = desktop_obs.resizes[-1][0]
            baseline = min(
                step(
                    "desktop terminal drawn",
                    _sample_drawn_width(desktop, desktop_obs, 3),
                    phone_obs,
                )
            )
            report["desktop_cols"] = desktop_cols
            report["desktop_attach_read_only"] = "read_only=true" in desktop_obs.urls[-1]
            assert baseline >= desktop_cols * 0.8, "Claude TUI never drew a full-width frame"

            with user_page(
                browser,
                headers={},
                viewport=_PHONE_VIEWPORT,
                is_mobile=True,
                has_touch=True,
                user_agent=_IPHONE_UA,
                **({"record_video_size": _PHONE_VIEWPORT} if recording else {}),
            ) as phone:
                report["phone_page_opened_s"] = round(time.monotonic() - started, 2)
                phone_obs.watch(phone)
                phone.goto(base_url)
                phone.wait_for_load_state("load")
                at_app_root = step(
                    "phone opened app root",
                    _sample_drawn_width(desktop, desktop_obs, _HOLD_S),
                    phone_obs,
                )

                phone.goto(session_url)
                session_menu = phone.get_by_test_id("header-conversation-actions").or_(
                    phone.get_by_test_id("session-actions-menu")
                )
                expect(session_menu).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
                in_default_view = step(
                    "phone opened session (default view)",
                    _sample_drawn_width(desktop, desktop_obs, _HOLD_S),
                    phone_obs,
                )
                report["phone_default_view_main_terminal_visible"] = (
                    phone.locator(f'{_MAIN_TERMINAL}[data-visible="true"]').count() > 0
                )

                session_menu.click()
                phone.get_by_test_id("view-mode-menu-terminal").click()
                _wait_terminal_connected(phone)
                in_terminal_view = step(
                    "phone switched to Terminal view",
                    _sample_drawn_width(desktop, desktop_obs, _HOLD_S),
                    phone_obs,
                )
                report["phone_video"] = str(phone.video.path()) if phone.video else None
            closed_samples = _sample_drawn_width(desktop, desktop_obs, _HOLD_S)
            # Judge the end state by the current attach's frame, not one from before a reattach.
            redraw_deadline = time.monotonic() + 30
            while closed_samples[-1][1] is None and time.monotonic() < redraw_deadline:
                desktop.wait_for_timeout(250)
                closed_samples.append((time.monotonic(), desktop_obs.drawn_width()))
            step("phone closed", closed_samples, phone_obs)
            final_width = closed_samples[-1][1] or 0
            report["desktop_video"] = str(desktop.video.path()) if desktop.video else None
            (artifacts / "desktop-final-screen.txt").write_text(
                "\n".join(desktop_obs.screen_lines()) + "\n", encoding="utf-8"
            )
    finally:
        (artifacts / "measurements.json").write_text(json.dumps(report, indent=2))

    attach_urls = desktop_obs.urls + phone_obs.urls
    assert phone_obs.resizes, "phone never attached to the session terminal and sent its size"
    assert len({urlparse(url).path for url in attach_urls}) == 1, attach_urls
    assert not any("read_only=true" in url for url in attach_urls), attach_urls
    assert phone_obs.resizes[-1][0] < desktop_cols * 0.9, phone_obs.resizes
    narrowed = {
        name: width
        for name, width in [
            ("app root", min(at_app_root)),
            ("session default view", min(in_default_view)),
            ("session Terminal view", min(in_terminal_view)),
            ("10s after the phone closed", final_width),
        ]
        if width < baseline * 0.9
    }
    assert not narrowed, (
        f"desktop Claude frame shrank from {baseline} columns while the phone was on: {narrowed}"
    )
