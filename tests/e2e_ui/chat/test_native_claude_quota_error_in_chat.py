"""A Claude Code quota error (HTTP 429) must reach the Chat view as a retryable failure.

Drives the real claude-native stack: a live ``claude`` CLI in the session
terminal, fed by the mock LLM scripted to reject every request with a 429
``Quota exceeded`` error. The message is sent from the web composer. Claude
Code retries the 429 with backoff for a few minutes, then prints
``API Error: Request rejected (429) · Quota exceeded …`` in its own pane and
ends the turn. The Chat view must show that same failure as the rate-limit
error pill with a Retry action that survives a reload — never a silently
unanswered turn, and not an unclassified generic error.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _select_view_mode,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_QUOTA_ERROR = (
    "Quota exceeded for model serving endpoint claude-sonnet-4: the workspace "
    "quota for this endpoint has been exhausted. Request rejected (429)."
)
# Enough 429s to outlast Claude Code's whole retry loop plus its background
# requests; an exhausted mock queue without a fallback answers 200 instead.
_QUOTA_RESPONSES = 400
_RATE_LIMIT_HEADLINE = "The model's rate limit was reached. You can retry this turn."

# Claude Code retries a 429 with backoff for roughly three minutes before it
# gives up on the turn.
_GIVE_UP_TIMEOUT_S = 330.0
_FIRST_REQUEST_TIMEOUT_S = 120.0
# Once the provider has seen no new request for this long, Claude Code has
# stopped retrying.
_RETRY_QUIET_S = 30.0
_PANE_READ_S = 4.0
# Long enough for a viewer of the recording to read each state.
_HOLD_MS = 4_000


def _mock_request_count(mock_url: str) -> int:
    return int(httpx.get(f"{mock_url}/stats", timeout=10.0).json()["request_count"])


def _transcript_items(base_url: str, session_id: str) -> list[dict[str, object]]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return list(resp.json().get("data", []))


def _item_texts(items: list[dict[str, object]]) -> list[str]:
    texts: list[str] = []
    for item in items:
        content = item.get("content")
        if isinstance(content, list):
            texts.append(
                " ".join(
                    block["text"]
                    for block in content
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                )
            )
        message = item.get("message")
        if isinstance(message, str):
            texts.append(message)
    return texts


def _terminal_screen_text(base_url: str, session_id: str) -> str:
    """Render the session terminal's read-only attach stream to plain text.

    The runner's tmux socket lives on the runner host, so the pane is read the
    way a viewer would: over the server's read-only attach WebSocket, replayed
    through a virtual terminal. Best effort; returns ``""`` when unavailable.
    """
    try:
        import pyte
        from websockets.sync.client import connect
    except ImportError as exc:
        _log.warning("terminal pane read skipped: %s", exc)
        return ""
    resources = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources", timeout=10.0)
    resources.raise_for_status()
    terminal_ids = [
        str(r["id"]) for r in resources.json().get("data", []) if r.get("type") == "terminal"
    ]
    if not terminal_ids:
        _log.warning("terminal pane read skipped: no terminal resource advertised")
        return ""
    ws_base = base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    ws_url = (
        f"{ws_base}/v1/sessions/{session_id}/resources/terminals/{terminal_ids[-1]}"
        "/attach?read_only=true"
    )
    screen = pyte.Screen(240, 80)
    stream = pyte.ByteStream(screen)
    try:
        with connect(ws_url, max_size=None, open_timeout=20) as ws:
            deadline = time.monotonic() + _PANE_READ_S
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    frame = ws.recv(timeout=remaining)
                except (TimeoutError, OSError):
                    break
                stream.feed(frame if isinstance(frame, bytes) else frame.encode())
    except Exception as exc:
        _log.warning("terminal pane read failed: %s", exc)
    return "\n".join(line.rstrip() for line in screen.display).strip()


def _wait_until_claude_gives_up(page: Page, mock_url: str, baseline: int) -> int:
    """Block until the provider stops seeing retries; return the request count."""
    started = time.monotonic()
    deadline = started + _GIVE_UP_TIMEOUT_S
    while _mock_request_count(mock_url) <= baseline:
        if time.monotonic() - started > _FIRST_REQUEST_TIMEOUT_S:
            raise AssertionError("Claude Code never contacted the model provider after Send")
        page.wait_for_timeout(1_000)
    last_count = _mock_request_count(mock_url)
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        page.wait_for_timeout(2_000)
        count = _mock_request_count(mock_url)
        if count != last_count:
            last_count, quiet_since = count, time.monotonic()
            continue
        if (
            time.monotonic() - quiet_since >= _RETRY_QUIET_S
            and page.locator(_WORKING).count() == 0
        ):
            return count
    raise AssertionError(
        f"Claude Code was still retrying after {_GIVE_UP_TIMEOUT_S:.0f}s "
        f"({last_count - baseline} provider requests)"
    )


def _pill_state(page: Page) -> dict[str, object]:
    """Describe what the Chat view shows for the failed turn."""
    pill = page.get_by_test_id("error-pill")
    state: dict[str, object] = {
        "pill_count": pill.count(),
        "api_error_bubbles": page.locator(_ASSISTANT, has_text="API Error").count(),
    }
    if state["pill_count"]:
        first = pill.first
        headline = first.get_by_test_id("error-headline")
        state["headline"] = headline.inner_text() if headline.count() else first.inner_text()
        state["controls"] = [
            text.strip() for text in first.get_by_role("button").all_inner_texts() if text.strip()
        ]
        state["retry_button"] = first.get_by_role("button", name="Retry", exact=True).count()
    return state


def _expand_pill(page: Page) -> str:
    pill = page.get_by_test_id("error-pill").first
    toggle = pill.locator('button[aria-expanded="false"]')
    if toggle.count():
        toggle.first.click()
    body = pill.get_by_test_id("error-message-content")
    return body.inner_text() if body.count() else ""


def _save_evidence(name: str, payload: object) -> None:
    if evidence_dir := os.environ.get("E2E_SCREENSHOT_DIR"):
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        (Path(evidence_dir) / name).write_text(json.dumps(payload, indent=2, default=str))


def _screenshot(page: Page, name: str) -> None:
    if screenshot_dir := os.environ.get("E2E_SCREENSHOT_DIR"):
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(screenshot_dir) / name))


@pytest.mark.nightly
@pytest.mark.timeout(900)
def test_native_claude_quota_error_surfaces_in_chat_as_retryable_pill(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A 429 quota failure printed by Claude Code shows in Chat as a retryable rate limit.

    :param request: Used to create the recorded page only after the session
        and terminal setup, so a recording starts at the user's first action.
    :param native_claude_mock_session: ``(base_url, session_id)`` on the real
        claude-native wrapper, backed by the mock LLM.
    :param mock_llm_server_url: The mock LLM server base URL.
    """
    base_url, session_id = native_claude_mock_session
    _log.info("claude-native session %s on %s", session_id, base_url)
    reset_mock_llm(mock_llm_server_url)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    # Let the TUI paint its idle prompt before the provider starts failing.
    page.wait_for_timeout(8_000)
    idle_pane = _terminal_screen_text(base_url, session_id)
    _log.info("idle pane:\n%s", idle_pane)
    _screenshot(page, "1-terminal-idle.png")
    _select_view_mode(page, "Chat")

    # The provider rejects every request from here on.
    quota_responses = [{"error": _QUOTA_ERROR, "status_code": 429}] * _QUOTA_RESPONSES
    configure_mock_llm(mock_llm_server_url, quota_responses, key=_CLAUDE_MOCK_MODEL)
    configure_mock_llm(mock_llm_server_url, quota_responses, key="default")
    baseline = _mock_request_count(mock_llm_server_url)

    prompt = f"quota-{uuid.uuid4().hex[:8]} please say hello"
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_USER, has_text=prompt)).to_have_count(1, timeout=60_000)
    _screenshot(page, "2-chat-sent.png")

    requests_seen = _wait_until_claude_gives_up(page, mock_llm_server_url, baseline) - baseline
    _log.info("Claude Code gave up after %d provider requests", requests_seen)

    # Terminal half: Claude Code's own pane reports the quota error.
    _select_view_mode(page, "Terminal")
    _wait_terminal_connected(page)
    page.wait_for_timeout(3_000)
    failed_pane = _terminal_screen_text(base_url, session_id)
    _log.info("pane after failure:\n%s", failed_pane)
    _screenshot(page, "3-terminal-api-error.png")
    page.wait_for_timeout(_HOLD_MS)
    items = _transcript_items(base_url, session_id)

    # Chat half: capture everything the user sees before judging it, so a
    # failing build still yields the full picture (and a complete recording).
    _select_view_mode(page, "Chat")
    expect(page.locator(_USER, has_text=prompt)).to_be_visible(timeout=30_000)
    page.wait_for_timeout(3_000)
    live_state = _pill_state(page)
    _screenshot(page, "4-chat-after-failure.png")
    live_expanded = _expand_pill(page)
    page.wait_for_timeout(_HOLD_MS)
    _screenshot(page, "5-chat-pill-expanded.png")

    page.reload()
    if page.get_by_test_id("view-mode-toggle").count():
        _select_view_mode(page, "Chat")
    expect(page.locator(_USER, has_text=prompt)).to_be_visible(timeout=30_000)
    page.wait_for_timeout(3_000)
    reloaded_state = _pill_state(page)
    _screenshot(page, "6-chat-after-reload.png")
    page.wait_for_timeout(_HOLD_MS)

    _save_evidence(
        "evidence.json",
        {
            "session_id": session_id,
            "prompt": prompt,
            "provider_requests_during_turn": requests_seen,
            "idle_pane": idle_pane,
            "failed_pane": failed_pane,
            "items": items,
            "chat_live": {**live_state, "expanded": live_expanded},
            "chat_after_reload": reloaded_state,
        },
    )
    _log.info("chat live: %s | expanded: %r", live_state, live_expanded)
    _log.info("chat after reload: %s", reloaded_state)

    api_error_texts = [text for text in _item_texts(items) if "API Error" in text]
    assert api_error_texts, (
        f"Claude Code's API error never reached the session transcript; pane:\n{failed_pane}"
    )
    assert any("429" in text for text in api_error_texts), api_error_texts
    if failed_pane:
        assert "API Error" in failed_pane, failed_pane

    # The failed turn must be visible in Chat at all...
    assert live_state["pill_count"] == 1, (
        f"Chat shows no error pill for the failed 429 turn: {live_state}"
    )
    # ...and as the retryable rate-limit failure the quota error is.
    assert live_state["headline"] == _RATE_LIMIT_HEADLINE and live_state["retry_button"] == 1, (
        "Chat shows the 429 quota failure as "
        f"{live_state['headline']!r} with controls {live_state['controls']!r} "
        f"(expanded: {live_expanded!r}) instead of {_RATE_LIMIT_HEADLINE!r} with Retry"
    )
    assert "429" in live_expanded and "Quota exceeded" in live_expanded, live_expanded
    assert reloaded_state.get("headline") == _RATE_LIMIT_HEADLINE, reloaded_state
    assert reloaded_state.get("retry_button") == 1, reloaded_state
