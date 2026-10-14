"""UI journey: a web-composer message reaches a Claude-native session whose
title is wider than its terminal.

Claude Code labels the composer's opening rule with the session title; a title
wider than the pane overflows that rule, so the row above ``❯`` has no rule
glyph left of it. The journey renames the session from the Terminal view in a
narrow browser window, then sends from Chat: the reply must arrive and no
error pill may appear. The pane is read through the SPA's own terminal
WebSocket (rendered with ``pyte``) because the runner's tmux socket may live
in another sandbox.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable

import httpx
import pyte
import pytest
from playwright.sync_api import Browser, Page, WebSocket, expect
from wcwidth import wcswidth

from omnigent.harnesses.claude_native.bridge import _BOX_RULE_CHARS, _claude_prompt_rendered
from tests.e2e_ui.conftest import reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _ASSISTANT,
    _CLAUDE_MOCK_MODEL,
    _focus_tui,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_ERROR_PILL = '[data-testid="error-pill"]'
# Narrow enough for a ~57-column terminal, but above the SPA's 768px mobile
# breakpoint, below which the header Chat/Terminal toggle is not rendered.
_NARROW_VIEWPORT = {"width": 820, "height": 720}
_TUI_IDLE_TIMEOUT_S = 150.0
_RENAME_TIMEOUT_S = 40.0
# Readiness gate hard cap (180s) plus boot/attach slack, so the turn reaches a
# terminal outcome — a reply or an error pill — either way.
_TURN_OUTCOME_TIMEOUT_MS = 240_000

_TITLES = {
    "ascii": (
        "fix-the-billing-webhook-retry-backoff-path-and-document-every-edge-case-"
        "we-hit-in-staging-this-week-before-the-release-freeze"
    ),
    "wide-chars": (
        "請求のWebhook再試行バックオフ経路を修正し、ステージングで発生した全てのエッジケースを"
        "文書化する作業セッション"
    ),
}


class PaneMirror:
    """Render the SPA's terminal WebSocket stream with pyte at the view's size."""

    def __init__(self, page: Page) -> None:
        self.cols = 80
        self.rows = 24
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        page.on("websocket", self._on_websocket)

    def _on_websocket(self, ws: WebSocket) -> None:
        if "/attach" not in ws.url:
            return
        ws.on("framesent", self._on_sent)
        ws.on("framereceived", self._on_received)

    def _on_sent(self, payload: str | bytes) -> None:
        if not isinstance(payload, str):
            return
        try:
            msg = json.loads(payload)
        except ValueError:
            return
        if (
            isinstance(msg, dict)
            and msg.get("type") == "resize"
            and "cols" in msg
            and "rows" in msg
        ):
            with self.lock:
                self.cols, self.rows = int(msg["cols"]), int(msg["rows"])
                self.screen.resize(self.rows, self.cols)

    def _on_received(self, payload: str | bytes) -> None:
        if isinstance(payload, bytes):
            with self.lock:
                self.stream.feed(payload)

    def text(self) -> str:
        with self.lock:
            return "\n".join(line.rstrip() for line in self.screen.display)


def _wait_for(page: Page, predicate: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(500)
    if predicate():
        return
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


def _frame_around_prompt(pane: str) -> tuple[str, list[str]] | None:
    """Return the row above the last ``❯``-led row (the composer) and the rule rows below it."""
    lines = [line for line in pane.splitlines() if line.strip()]
    for idx in range(len(lines) - 1, 0, -1):
        if lines[idx].strip().startswith("❯"):
            below: list[str] = []
            for row in lines[idx + 1 :]:
                if set(row.strip()) - _BOX_RULE_CHARS:
                    break
                below.append(row.strip())
            return lines[idx - 1], below
    return None


def _frame_shape(frame: tuple[str, list[str]] | None, cols: int) -> str | None:
    """Return ``"wrapped"`` or ``"clipped"`` for a title frame around ``❯``, else ``None``."""
    if frame is None:
        return None
    above, below = frame
    if not above.rstrip().endswith("─"):
        return None
    # A wrapped box has a full-width closing row continuing onto rows no wider
    # than the pane; a clipped title sits one space in and ends in "… ─".
    if len(below) >= 2 and below[0] == "─" * cols and all(0 < len(row) <= cols for row in below):
        return "wrapped"
    if above.startswith(" ") and not above.startswith("  ") and above.rstrip().endswith("… ─"):
        return "clipped"
    return None


def _rename_from_terminal(page: Page, mirror: PaneMirror, title: str) -> None:
    """Type ``/rename <title>`` into the embedded TUI and wait for Claude Code's confirmation."""
    _focus_tui(page)
    page.keyboard.type(f"/rename {title}", delay=15)
    typed = "".join(title[-12:].split())
    _wait_for(
        page,
        lambda: typed in "".join(mirror.text().split()),
        10.0,
        "the rename draft to land in the composer",
    )
    page.keyboard.press("Enter")
    marker = "".join(title[:12].split())
    # The confirmation and the echoed title wrap at the pane width, with the
    # continuation rows indented, so match them with all whitespace removed.
    _wait_for(
        page,
        lambda: (
            "renamed" in "".join(mirror.text().lower().split())
            and marker in "".join(mirror.text().split())
        ),
        _RENAME_TIMEOUT_S,
        f"Claude Code to confirm the rename to {title[:24]!r}",
    )


def _transcript_errors(base_url: str, session_id: str) -> list[str]:
    items = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "desc"},
        timeout=10.0,
    )
    items.raise_for_status()
    return [
        str(item.get("message", ""))
        for item in items.json()["data"]
        if item.get("type") == "error"
    ]


@pytest.mark.timeout(600)
@pytest.mark.parametrize("title", list(_TITLES.values()), ids=list(_TITLES))
def test_native_claude_composer_delivers_under_title_wider_than_terminal(
    browser: Browser,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    title: str,
) -> None:
    """A message sent from web chat lands in a session titled wider than its terminal."""
    base_url, session_id = native_claude_mock_session
    _log.info("title-wider-than-terminal journey: base_url=%s session_id=%s", base_url, session_id)
    token = f"TITLE-RULE-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, token)

    context = browser.new_context(viewport=_NARROW_VIEWPORT)
    try:
        page = context.new_page()
        mirror = PaneMirror(page)
        page.goto(f"{base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)
        _wait_for(
            page,
            lambda: _claude_prompt_rendered(mirror.text()),
            _TUI_IDLE_TIMEOUT_S,
            "Claude Code's idle composer",
        )
        assert wcswidth(title) > mirror.cols, (
            f"title spans {wcswidth(title)} columns but the terminal is {mirror.cols} wide; "
            "the journey needs a title wider than the terminal"
        )

        _rename_from_terminal(page, mirror, title)
        _wait_for(
            page,
            lambda: _frame_shape(_frame_around_prompt(mirror.text()), mirror.cols) is not None,
            10.0,
            "the retitled frame around ❯ to settle",
        )
        pane = mirror.text()
        frame = _frame_around_prompt(pane)
        shape = _frame_shape(frame, mirror.cols)
        assert frame is not None and shape is not None, (
            f"expected a wrapped or clipped title frame around ❯; pane was:\n{pane}"
        )
        above, below = frame
        _log.info(
            "terminal %dx%d; %s frame after /rename: above=%r below=%r",
            mirror.cols,
            mirror.rows,
            shape,
            above,
            below,
        )
        page.wait_for_timeout(2000)

        _ensure_chat_view(page)
        _send(page, f"Reply with exactly this token and nothing else: {token}")
        reply = page.locator(_ASSISTANT, has_text=token).first
        error_pill = page.locator(_ERROR_PILL).first
        expect(reply.or_(error_pill)).to_be_visible(timeout=_TURN_OUTCOME_TIMEOUT_MS)

        if error_pill.is_visible():
            error_pill.click()
            page.wait_for_timeout(4000)
            pill_text = error_pill.inner_text()
            errors = _transcript_errors(base_url, session_id)
            _log.info("error pill: %s\ntranscript errors: %s", pill_text, errors)
            raise AssertionError(
                "the message was not delivered although the terminal showed an idle ❯ "
                f"under {above!r}:\n{pill_text}\n\ntranscript errors: {errors}"
            )
        expect(reply).to_be_visible()
        page.wait_for_timeout(2000)
    finally:
        context.close()
