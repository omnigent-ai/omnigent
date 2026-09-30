"""UI journey: a web message lost in a host reconnect must not corrupt the next one."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import uuid
from collections.abc import Callable

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state, reset_mock_llm, set_fallback_mock_llm

from .test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _ensure_chat_view,
    _item_text,
    _ordered_message_items,
    _select_view_mode,
    _send,
    _turn_prompt,
)
from .test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _RESUMED_TURN_TIMEOUT_MS,
    _UNDELIVERED_HEADLINE,
    _assert_user_bubbles_are_their_committed_items,
    _open_terminal_view,
    _pane_process_ids,
    _queued_input_texts,
    _send_turn_and_settle,
    _signal_each,
    _tmux_advert,
    _wait_for_queued_input,
    _wait_for_runner_offline,
    _wait_for_transcript_message,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_BUBBLE = '[data-testid="message-bubble"]'


def _log_reconnect_state(page: Page, base_url: str, session_id: str) -> None:
    """Log the transcript items, queued inputs and bubbles after the reconnect turn."""
    items = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=15.0,
    ).json()["data"]
    _log.info(
        "transcript items: %r",
        [
            (item.get("type"), item.get("role"), item.get("code"), _item_text(item))
            for item in items
        ],
    )
    _log.info("still queued on the server: %r", _queued_input_texts(base_url, session_id))
    bubbles = page.locator(_BUBBLE).evaluate_all(
        "els => els.map(e => [e.getAttribute('data-role'), e.getAttribute('data-message-id'),"
        " e.innerText.slice(0, 90)])"
    )
    _log.info("bubbles in the tab: %r", bubbles)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_lost_message_after_host_reconnect_stays_visible_and_next_renders_once(
    page: Page,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    _recover_shared_runner: Callable[[], None],
) -> None:
    """After a host reconnect the next message renders once and the lost one stays flagged."""
    base_url, session_id = native_claude_mock_session
    _log.info("reconnect journey: base_url=%s session_id=%s", base_url, session_id)
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    reset_mock_llm(mock_llm_server_url)

    nonce = uuid.uuid4().hex[:8]
    first_marker, first_token = f"usr-1-{nonce}", f"ast-1-{nonce}"
    lost_marker, lost_token = f"usr-2-{nonce}", f"ast-2-{nonce}"
    next_marker, next_token = f"usr-3-{nonce}", f"ast-3-{nonce}"

    _send_turn_and_settle(
        page,
        mock_llm_server_url,
        base_url=base_url,
        session_id=session_id,
        index=1,
        user_marker=first_marker,
        assistant_token=first_token,
        expected_assistant_bubbles=1,
    )
    _log.info("turn 1 settled")

    advert = _tmux_advert(base_url, session_id)
    assert advert is not None, "the Claude terminal advertised no tmux pane"
    pane_pids = _pane_process_ids(advert)
    assert pane_pids, "the Claude terminal pane owns no process"
    _signal_each(pane_pids, signal.SIGSTOP)
    _log.info("froze the Claude Code pane (pids=%s); sending the message it will lose", pane_pids)
    with page.expect_response(
        lambda response: (
            response.request.method == "POST"
            and response.url.endswith(f"/v1/sessions/{session_id}/events")
        ),
        timeout=60_000,
    ) as posted:
        _send(page, _turn_prompt(2, lost_marker, lost_token))
    assert posted.value.ok, f"the frozen-TUI message was refused: {posted.value.status}"
    expect(page.locator(_USER, has_text=lost_marker)).to_have_count(1)
    _wait_for_queued_input(page, base_url, session_id, lost_marker)
    _log.info("server holds the lost message as a queued input")

    _signal_each(pane_pids, signal.SIGKILL)
    subprocess.run(
        ["tmux", "-S", advert["socket_path"], "kill-server"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    os.kill(int(str(_server_state["runner_pid"])), signal.SIGKILL)
    _wait_for_runner_offline(page, base_url, session_id)
    _log.info("runner and terminal are gone; bringing a fresh runner back")
    _recover_shared_runner()

    next_prompt = _turn_prompt(3, next_marker, next_token)
    set_fallback_mock_llm(mock_llm_server_url, "default", next_token)
    set_fallback_mock_llm(mock_llm_server_url, _CLAUDE_MOCK_MODEL, next_token)
    _send(page, next_prompt)
    expect(page.locator(_ASSISTANT, has_text=next_token).first).to_be_visible(
        timeout=_RESUMED_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_RESUMED_TURN_TIMEOUT_MS)
    _wait_for_transcript_message(page, base_url, session_id, next_marker, role="user")
    _log.info("turn 3 answered after the reconnect")
    _log_reconnect_state(page, base_url, session_id)

    committed_next = [
        _item_text(item)
        for item in _ordered_message_items(base_url, session_id)
        if item.get("role") == "user" and next_marker in _item_text(item)
    ]
    assert committed_next == [next_prompt], (
        f"the resumed Claude Code recorded the next message as {committed_next!r}, "
        f"not the text that was sent {next_prompt!r}"
    )
    expect(page.locator(_USER, has_text=next_marker)).to_have_count(1)
    expect(page.locator(_USER, has_text=lost_marker)).to_have_count(1)
    expect(
        page.get_by_test_id("error-headline").filter(has_text=_UNDELIVERED_HEADLINE)
    ).to_have_count(1)
    expect(page.locator(_ASSISTANT, has_text=first_token)).to_have_count(1)
    expect(page.locator(_ASSISTANT, has_text=next_token)).to_have_count(1)
    expect(page.locator(_ASSISTANT, has_text=lost_token)).to_have_count(0)
    _assert_user_bubbles_are_their_committed_items(
        page, base_url, session_id, [first_marker, lost_marker, next_marker]
    )

    fresh = page.context.new_page()
    try:
        fresh.goto(f"{base_url}/c/{session_id}")
        expect(fresh.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=30_000)
        _select_view_mode(fresh, "Chat")
        for marker in (first_marker, lost_marker, next_marker):
            expect(fresh.locator(_USER, has_text=marker)).to_have_count(1, timeout=30_000)
    finally:
        fresh.close()
    _log.info("lost message flagged undelivered; next message rendered once in both tabs")
