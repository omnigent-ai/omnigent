"""A Claude follow-up is delivered while a backgrounded sub-agent outlives the turn."""

from __future__ import annotations

import contextlib
import json
import time
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
# Spans many 0.2s status-file polls, so a misread status cannot flip back unseen.
_STATUS_STABLE_S = 2.0
_STATUS_SETTLE_TIMEOUT_S = 30.0
_MOCK_REQUEST_TIMEOUT_S = 30.0


def _user_text_seen(mock_llm_server_url: str, token: str) -> bool:
    """Return whether any captured model request carries *token* in user content."""
    for captured in get_mock_requests(mock_llm_server_url):
        for message in captured.get("messages") or []:
            if message.get("role") == "user" and token in json.dumps(message.get("content")):
                return True
    return False


def _task_notification_seen(mock_llm_server_url: str, conversation_token: str) -> bool:
    """Return whether a request carrying *conversation_token* has a task notification."""
    for captured in get_mock_requests(mock_llm_server_url):
        user_texts = [
            json.dumps(message.get("content"))
            for message in captured.get("messages") or []
            if message.get("role") == "user"
        ]
        if any(conversation_token in text for text in user_texts) and any(
            "<task-notification>" in text for text in user_texts
        ):
            return True
    return False


def _wait_for_stable_status(base_url: str, session_id: str, status: str) -> None:
    """Wait until the session has read *status* for ``_STATUS_STABLE_S`` without leaving it."""
    deadline = time.monotonic() + _STATUS_SETTLE_TIMEOUT_S
    status_since: float | None = None
    while time.monotonic() < deadline:
        response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        response.raise_for_status()
        now = time.monotonic()
        if response.json().get("status") != status:
            status_since = None
        elif status_since is None:
            status_since = now
        elif now - status_since >= _STATUS_STABLE_S:
            return
        time.sleep(0.2)
    pytest.fail(f"session did not stay {status} for {_STATUS_STABLE_S:.0f}s")


def _gate_pending(mock_llm_server_url: str) -> bool:
    response = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0)
    response.raise_for_status()
    return bool(response.json().get("pending"))


def _release_gate(mock_llm_server_url: str) -> bool:
    response = httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0)
    response.raise_for_status()
    return bool(response.json().get("released"))


def _release_all_gates(mock_llm_server_url: str) -> None:
    with contextlib.suppress(httpx.HTTPError):
        while _release_gate(mock_llm_server_url):
            pass


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    composer.press("Enter")


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_follow_up_is_delivered_while_background_agent_runs(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Claude takes a prompt once its turn ends, and the agent's completion turn reads running."""
    base_url, session_id = native_claude_mock_session
    nonce = uuid.uuid4().hex[:8]
    launch_token = f"launch-{nonce}"
    launch_reply = f"launch-reply-{nonce}"
    worker_token = f"worker-{nonce}-background-task"
    # Longest tokens, because later parent requests also carry the earlier prompts.
    follow_up_token = f"follow-up-{nonce}-sent-after-the-agent-launched"
    follow_up_reply = f"follow-up-reply-{nonce}"
    worker_result = f"worker-result-{nonce}-reported-back-to-the-parent-on-completion"
    completion_reply = f"completion-reply-{nonce}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": worker_result, "block": True}],
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
        mock_llm_server_url,
        [{"text": follow_up_reply}],
        match=follow_up_token,
        required_tools=["Bash"],
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": completion_reply, "block": True}],
        match=worker_result,
        required_tools=["Bash"],
    )

    # A held request would wedge the shared mock-backed runner for later tests.
    request.addfinalizer(lambda: _release_all_gates(mock_llm_server_url))

    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _send(page, f"{launch_token} Start the background worker agent, then reply.")
    expect(page.locator(_ASSISTANT, has_text=launch_reply).first).to_be_visible(
        timeout=_FIRST_TURN_TIMEOUT_MS
    )
    # The worker's first model request races the parent's reply request.
    poll_until(
        lambda: _user_text_seen(mock_llm_server_url, worker_token),
        timeout_s=_MOCK_REQUEST_TIMEOUT_S,
        interval_s=0.2,
    )
    _wait_for_stable_status(base_url, session_id, "idle")

    _send(page, f"{follow_up_token} Reply to this follow-up.")
    expect(page.get_by_test_id("composer-queued-strip")).to_have_count(0, timeout=10_000)
    expect(page.locator(_ASSISTANT, has_text=follow_up_reply).first).to_be_visible(timeout=30_000)
    # Claude notifies the parent when a background agent finishes, fails, or is killed.
    assert not _task_notification_seen(mock_llm_server_url, launch_token), (
        "background sub-agent ended before the follow-up was answered"
    )
    _wait_for_stable_status(base_url, session_id, "idle")

    assert _release_gate(mock_llm_server_url), "background sub-agent was not held"
    # Claude asks the parent model to handle the finished agent.
    poll_until(
        lambda: _gate_pending(mock_llm_server_url),
        timeout_s=_MOCK_REQUEST_TIMEOUT_S,
        interval_s=0.2,
    )
    assert _task_notification_seen(mock_llm_server_url, launch_token)
    _wait_for_stable_status(base_url, session_id, "running")
    assert _release_gate(mock_llm_server_url)
    expect(page.locator(_ASSISTANT, has_text=completion_reply).first).to_be_visible(timeout=30_000)
    _wait_for_stable_status(base_url, session_id, "idle")
