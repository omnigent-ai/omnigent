"""A web-triggered claude-native /compact stays visible in chat while a follow-up is sent."""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui.chat.test_always_steer import _record_message_posts, _wait_for
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _ensure_chat_view,
)
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

# Claude Code's manual-compaction summarization prompt contains this phrase.
_COMPACT_PROMPT = "create a detailed summary of the conversation so far"
_COMPACT_SUMMARY = "<analysis>Mock analysis.</analysis>\n<summary>The user said hello.</summary>"
_COMPACT_DELAY_S = 25.0
_COMPACTING = '[data-testid="compacting-indicator"]'
_QUEUED_STRIP = '[data-testid="composer-queued-strip"]'


def _session_status(base_url: str, session_id: str) -> str | None:
    return httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json().get("status")


def _model_requests_containing(mock_url: str, text: str) -> int:
    return sum(1 for body in get_mock_requests(mock_url) if text in json.dumps(body))


@pytest.mark.nightly
@pytest.mark.timeout(600)
@pytest.mark.parametrize("always_steer", [False, True], ids=["default", "always_steer"])
def test_web_compact_keeps_progress_visible_and_delivers_followup(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    always_steer: bool,
) -> None:
    base_url, session_id = native_claude_mock_session
    nonce = uuid.uuid4().hex[:8]
    # Longer than _COMPACT_PROMPT so the mock routes the follow-up to its own queue.
    marker = f"follow-up typed while the conversation was compacting, ref {nonce}"
    reply = f"follow-up reply {nonce}"
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, "default", "first reply")
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, "first reply")
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _COMPACT_SUMMARY, "delay": _COMPACT_DELAY_S}],
        match=_COMPACT_PROMPT,
    )
    configure_mock_llm(mock_llm_server_url, [{"text": reply}] * 3, match=marker)

    page: Page = request.getfixturevalue("page")
    if always_steer:
        page.add_init_script("window.localStorage.setItem('omnigent:always-steer', 'true')")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=60_000)

    composer.fill("hello")
    composer.press("Enter")
    expect(page.locator(_ASSISTANT).first).to_contain_text("first reply", timeout=180_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    poll_until(
        lambda: _session_status(base_url, session_id) == "idle", timeout_s=90.0, interval_s=1.0
    )
    page.wait_for_timeout(2_000)

    composer.click()
    page.keyboard.type("/compact", delay=60)
    page.wait_for_timeout(800)
    composer.press("Enter")
    poll_until(
        lambda: _model_requests_containing(mock_llm_server_url, _COMPACT_PROMPT),
        timeout_s=30.0,
        interval_s=0.25,
    )
    compaction_requested = time.monotonic()
    compacting = page.locator(_COMPACTING)
    compacted = page.get_by_text("Conversation compacted")
    expect(compacting).to_be_visible(timeout=10_000)

    posts = _record_message_posts(page, session_id)
    composer.click()
    page.keyboard.type(marker, delay=40)
    composer.press("Enter")
    if always_steer:
        _wait_for(page, lambda: marker in posts, timeout_s=10.0)
        assert time.monotonic() - compaction_requested < _COMPACT_DELAY_S
        assert not compacted.is_visible(), "follow-up was sent only after compaction"
        expect(page.locator(_QUEUED_STRIP)).to_have_count(0)
    else:
        expect(page.locator(_QUEUED_STRIP)).to_contain_text(marker, timeout=5_000)
        assert marker not in posts

    while (elapsed := time.monotonic() - compaction_requested) < _COMPACT_DELAY_S - 1:
        assert compacting.is_visible() or compacted.is_visible(), (
            f"chat stopped showing compaction {elapsed:.1f}s after it started"
        )
        page.wait_for_timeout(1_000)
    expect(compacted).to_be_visible(timeout=60_000)
    expect(compacting).to_have_count(0)

    expect(page.locator(_USER).filter(has_text=marker)).to_have_count(1, timeout=120_000)
    expect(page.locator(_ASSISTANT).filter(has_text=reply)).to_be_visible(timeout=120_000)
