"""A Claude follow-up that leaves a background agent running still ends its turn."""

from __future__ import annotations

import contextlib
import json
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_requests
from tests.e2e_ui.conftest import configure_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_FIRST_TURN_TIMEOUT_MS = 180_000
_SETTLE_TIMEOUT_MS = 30_000
_MOCK_REQUEST_TIMEOUT_S = 30.0
_IDLE_HOLD_MS = 3_000


def _user_text_seen(mock_llm_server_url: str, token: str) -> bool:
    """Return whether any captured model request carries *token* in user content."""
    for captured in get_mock_requests(mock_llm_server_url):
        for message in captured.get("messages") or []:
            if message.get("role") == "user" and token in json.dumps(message.get("content")):
                return True
    return False


def _release_all_gates(mock_llm_server_url: str) -> None:
    with contextlib.suppress(httpx.HTTPError):
        while (
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0).json().get("released")
        ):
            pass


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    composer.press("Enter")


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_follow_up_after_background_agent_launch_settles_and_delivers_next_message(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Working clears after Claude replies, and the next follow-up is not queued."""
    base_url, session_id = native_claude_mock_session
    nonce = uuid.uuid4().hex[:8]
    # Later requests repeat earlier prompts, so each newer routing token is longer.
    first_token = f"t1-{nonce}"
    first_reply = f"first-reply-{nonce}"
    launch_token = f"launch-{nonce}-agent"
    launch_reply = f"launch-reply-{nonce}"
    worker_token = f"worker-{nonce}-background-task"
    next_token = f"next-follow-up-{nonce}-sent-after-the-agent-launched"
    next_reply = f"next-reply-{nonce}"

    configure_mock_llm(
        mock_llm_server_url, [{"text": first_reply}], match=first_token, required_tools=["Bash"]
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"worker-result-{nonce}", "block": True}],
        match=worker_token,
    )
    agent_arguments = {
        "description": "Background worker",
        "prompt": f"{worker_token}: do the work, then report back.",
        "subagent_type": "general-purpose",
        "run_in_background": True,
    }
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"toolu_{nonce}",
                        "name": "Agent",
                        "arguments": json.dumps(agent_arguments),
                    }
                ]
            },
            {"text": launch_reply},
        ],
        match=launch_token,
        required_tools=["Agent"],
    )
    configure_mock_llm(
        mock_llm_server_url, [{"text": next_reply}], match=next_token, required_tools=["Bash"]
    )
    # A held sub-agent request would wedge the shared mock-backed runner for later tests.
    request.addfinalizer(lambda: _release_all_gates(mock_llm_server_url))

    page = request.getfixturevalue("page")
    # With Always steer off, a follow-up sent while the session reads busy waits in the queue.
    page.add_init_script("localStorage.setItem('omnigent:always-steer', 'false')")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    working = page.get_by_test_id("working-indicator")

    _send(page, f"{first_token} Reply to this first question.")
    expect(page.locator(_ASSISTANT, has_text=first_reply).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    expect(working).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)

    _send(page, f"{launch_token} Start the background worker agent, then reply.")
    expect(page.locator(_ASSISTANT, has_text=launch_reply).first).to_be_visible(timeout=60_000)
    poll_until(
        lambda: _user_text_seen(mock_llm_server_url, worker_token),
        timeout_s=_MOCK_REQUEST_TIMEOUT_S,
        interval_s=0.2,
    )
    expect(working).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)
    # A brief idle flash does not count; the cleared state must persist.
    page.wait_for_timeout(_IDLE_HOLD_MS)
    expect(working).to_have_count(0)

    _send(page, f"{next_token} Reply to this follow-up.")
    expect(page.locator(_ASSISTANT, has_text=next_reply).first).to_be_visible(
        timeout=_SETTLE_TIMEOUT_MS
    )
    expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0)
    expect(working).to_have_count(0, timeout=_SETTLE_TIMEOUT_MS)
