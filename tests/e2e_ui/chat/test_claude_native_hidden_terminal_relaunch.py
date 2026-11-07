"""E2E: a claude-native message after a pane relaunch survives a display:none terminal view.

The stock chat view (``visibility:hidden``) is the control. Closing the pane through the
terminal resource API is the same registry close the idle reaper performs.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest
from playwright.sync_api import Page, WebSocket, expect

from omnigent.terminals.control_bridge import MIN_FORWARDED_COLS, MIN_FORWARDED_ROWS
from tests.e2e_ui.conftest import set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_log = logging.getLogger(__name__)

_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_MAIN_TERMINAL_VIEW = "main-terminal-view"
_HIDE_TERMINAL_VIEW_CSS = f'[data-testid="{_MAIN_TERMINAL_VIEW}"] {{ display: none !important; }}'

_FIRST_TOKEN = "FIRST_TURN_ANSWERED"
_RELAUNCH_TOKEN = "RELAUNCH_TURN_ANSWERED"

# Claude boots in the pane before the first prompt can land.
_FIRST_TURN_TIMEOUT_MS = 180_000
_PANE_CLOSE_TIMEOUT_S = 30.0
# 30 s pane re-create wait + 180 s readiness cap + attach latency.
_RELAUNCH_OUTCOME_TIMEOUT_MS = 330_000
_RESIZE_TIMEOUT_S = 15.0


def _running_terminal_ids(base_url: str, session_id: str) -> list[str]:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        params={"limit": 100},
        timeout=10.0,
    )
    response.raise_for_status()
    return [
        str(item.get("id"))
        for item in response.json().get("data", [])
        if (item.get("metadata") or {}).get("running") is not False
    ]


def _close_running_terminals(page: Page, base_url: str, session_id: str) -> None:
    """Close the session's native pane through the resource API and wait for it to go."""
    for terminal_id in _running_terminal_ids(base_url, session_id):
        httpx.delete(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals/{terminal_id}",
            timeout=30.0,
        ).raise_for_status()
    started = time.monotonic()
    while time.monotonic() - started < _PANE_CLOSE_TIMEOUT_S:
        if not _running_terminal_ids(base_url, session_id):
            return
        page.wait_for_timeout(1_000)
    raise AssertionError(
        f"the Claude pane was still listed {_PANE_CLOSE_TIMEOUT_S:.0f}s after closing it"
    )


def _record_terminal_frames(page: Page) -> list[dict[str, object]]:
    """Collect the text frames the browser sends on terminal attach sockets."""
    frames: list[dict[str, object]] = []

    def on_websocket(ws: WebSocket) -> None:
        if "terminal" not in ws.url or "attach" not in ws.url:
            return
        # A direct-attach URL carries its bearer token in the query.
        attach_url = ws.url.split("?", 1)[0]

        def on_sent(payload: str | bytes) -> None:
            if isinstance(payload, str):
                frames.append(
                    {"t": round(time.monotonic(), 3), "url": attach_url, "sent": payload}
                )

        ws.on("framesent", on_sent)

    page.on("websocket", on_websocket)
    return frames


def _resize_size(frame: dict[str, object]) -> tuple[int, int] | None:
    """``(cols, rows)`` of a resize frame, or ``None`` for any other or malformed frame."""
    try:
        control = json.loads(str(frame["sent"]))
        if not isinstance(control, dict) or control.get("type") != "resize":
            return None
        return int(control["cols"]), int(control["rows"])
    except (KeyError, TypeError, ValueError):
        return None


def _wait_for_usable_resize(
    page: Page, frames: list[dict[str, object]], start: int, what: str
) -> dict[str, object]:
    """Wait for a resize frame the bridge forwards (at least 20x5) sent after ``start``."""
    deadline = time.monotonic() + _RESIZE_TIMEOUT_S
    while time.monotonic() < deadline:
        for frame in frames[start:]:
            size = _resize_size(frame)
            if (
                size is not None
                and size[0] >= MIN_FORWARDED_COLS
                and size[1] >= MIN_FORWARDED_ROWS
            ):
                return frame
        page.wait_for_timeout(250)
    raise AssertionError(
        f"no usable resize frame within {_RESIZE_TIMEOUT_S:.0f}s of {what}; "
        f"frames: {frames[start:]}"
    )


def _expanded_error_pill_texts(page: Page) -> list[str]:
    pills = page.locator(_ERROR_PILL)
    texts: list[str] = []
    for index in range(pills.count()):
        pill = pills.nth(index)
        if pill.get_by_test_id("error-disclosure-icon").count():
            pill.click()
        texts.append(pill.inner_text())
    return texts


@pytest.mark.timeout(900)
@pytest.mark.parametrize(
    "hide_terminal_view",
    [True, False],
    ids=["terminal-view-display-none", "stock-chat-view"],
)
def test_message_after_pane_relaunch_is_delivered(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    hide_terminal_view: bool,
) -> None:
    base_url, session_id = native_claude_mock_session
    _log.info("native-claude session %s on %s", session_id, base_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", _FIRST_TOKEN)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _FIRST_TOKEN)

    page: Page = request.getfixturevalue("page")
    _log.info("browser page opened")
    frames = _record_terminal_frames(page)
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=_FIRST_TURN_TIMEOUT_MS)
    _ensure_chat_view(page)
    _send(page, f"Reply with exactly this token and nothing else: {_FIRST_TOKEN}")
    expect(page.locator(_ASSISTANT, has_text=_FIRST_TOKEN).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    _log.info("first turn answered; terminal frames so far: %s", json.dumps(frames))

    hide_style = None
    if hide_terminal_view:
        # The chat view keeps the terminal view mounted (hidden) behind it.
        expect(page.get_by_test_id(_MAIN_TERMINAL_VIEW)).to_be_attached()
        hide_style = page.add_style_tag(content=_HIDE_TERMINAL_VIEW_CSS)
        expect(page.get_by_test_id(_MAIN_TERMINAL_VIEW)).to_have_css("display", "none")

    _close_running_terminals(page, base_url, session_id)
    _log.info("Claude pane closed")
    frames_before_relaunch = len(frames)

    set_fallback_mock_llm(mock_llm_server_url, "default", _RELAUNCH_TOKEN)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, _RELAUNCH_TOKEN)
    _send(
        page,
        f"After the relaunch, reply with exactly this token and nothing else: {_RELAUNCH_TOKEN}",
    )
    sent_at = time.monotonic()
    outcome = page.locator(_ERROR_PILL).or_(page.locator(_ASSISTANT, has_text=_RELAUNCH_TOKEN))
    expect(outcome.first).to_be_visible(timeout=_RELAUNCH_OUTCOME_TIMEOUT_MS)
    elapsed = time.monotonic() - sent_at

    pill_texts = _expanded_error_pill_texts(page)
    relaunch_frames = frames[frames_before_relaunch:]
    _log.info(
        "relaunch outcome after %.0fs; terminal frames after relaunch: %s; error pills: %s",
        elapsed,
        json.dumps(relaunch_frames),
        json.dumps(pill_texts),
    )
    resize_frames = [
        frame["sent"] for frame in relaunch_frames if '"resize"' in str(frame["sent"])
    ]
    assert not any("did not become ready" in text for text in pill_texts), (
        f"the relaunched Claude pane never became ready ({elapsed:.0f}s after Send); "
        f"browser resize frames after the relaunch: {resize_frames}; error: {pill_texts}"
    )
    expect(page.locator(_ASSISTANT, has_text=_RELAUNCH_TOKEN).first).to_be_visible(timeout=30_000)

    if hide_style is None:
        # The stock hidden mount keeps its layout, so its re-attach still sizes the pane.
        _wait_for_usable_resize(page, frames, frames_before_relaunch, "the relaunch")
        return
    # Revealing the view lays the terminal out again, so its real size must now
    # reach the pane.
    revealed_at = len(frames)
    hide_style.evaluate("style => style.remove()")
    frame = _wait_for_usable_resize(page, frames, revealed_at, "revealing the terminal view")
    _log.info("resize after revealing the terminal view: %s", frame["sent"])
