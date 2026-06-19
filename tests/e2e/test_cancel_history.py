"""End-to-end test for cancellation history markers (mock LLM).

Exercises:
- Cancelling an in-progress response via the interrupt endpoint
- Verifying a cancellation marker is appended to the conversation
- Verifying a follow-up turn sees the cancellation context

Usage::

    pytest tests/e2e/test_cancel_history.py -v
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
)

_POLL_INTERVAL_SECONDS = 0.3
_SESSION_ITEMS_PAGE_SIZE = 1000
_SESSION_RUNNING_STATUSES = {"running"}
_SESSION_PRE_RUNNING_STATUSES = {"idle"}
_SESSION_NONTERMINAL_STATUSES = {"idle", "running"}
_SESSION_TERMINAL_ERROR_STATUSES = {"failed"}

# The server persists cancellation history as a synthetic user message today.
# There is no stable structured cancellation item type yet, so keep the
# wording dependency centralized and documented for future server changes.
_CANCELLATION_MARKER_TEXT = "interrupted"

# Sequencing heuristic: the interrupt endpoint can acknowledge before async
# teardown has fully settled. We do not have a response-level in-flight signal
# here, so keep the stable-idle hold explicit rather than weaker.
_INTERRUPT_IDLE_HOLD_SECONDS = 1.0


def _wait_for_session_running(
    client: httpx.Client,
    session_id: str,
    timeout: float = 60,
) -> None:
    """
    Poll until the runner-native session transitions to ``running``.

    :param client: HTTP client.
    :param session_id: The session ID to poll.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If not in_progress within timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        body = resp.json()
        status = body["status"]
        if status in _SESSION_RUNNING_STATUSES:
            return
        if status not in _SESSION_PRE_RUNNING_STATUSES:
            raise AssertionError(f"Session reached state {status!r} before running: {body}")
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"Session {session_id} didn't reach running within {timeout}s")


def _extract_all_text(body: dict[str, Any]) -> str:
    """
    Concatenate all output_text blocks from a response body.

    :param body: The terminal response body from
        GET /v1/responses/{id}.
    :returns: All assistant text joined by newlines.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _wait_for_cancellation_marker(
    client: httpx.Client,
    session_id: str,
    timeout: float = 30,
) -> list[dict[str, Any]]:
    """Poll persisted session items until the interrupt marker appears."""
    deadline = time.monotonic() + timeout
    last_items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        last_items = _list_all_session_items(client, session_id)
        cancellation_items = _filter_cancellation_marker_items(last_items)
        if cancellation_items:
            return cancellation_items
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"Expected a cancellation marker within {timeout}s. Last items: {last_items}"
    )


def _list_all_session_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return all currently persisted session items in one paginated snapshot."""
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {"order": "asc", "limit": _SESSION_ITEMS_PAGE_SIZE}
        if after is not None:
            params["after"] = after
        items_resp = client.get(f"/v1/sessions/{session_id}/items", params=params)
        items_resp.raise_for_status()
        page = items_resp.json()
        page_items = page["data"]
        items.extend(page_items)
        if not page.get("has_more"):
            return items
        after = page.get("last_id")
        if after is None:
            raise AssertionError(f"Items page had has_more without last_id: {page}")


def _filter_cancellation_marker_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return persisted synthetic user messages that mark an interrupted turn."""
    return [
        item
        for item in items
        if item.get("type") == "message"
        and item.get("role") == "user"
        and any(_CANCELLATION_MARKER_TEXT in c.get("text", "") for c in item.get("content", []))
    ]


def _wait_for_idle(
    client: httpx.Client,
    session_id: str,
    *,
    timeout: float = 30,
) -> None:
    """Poll until the interrupted session finishes teardown."""
    deadline = time.monotonic() + timeout
    last_body: dict[str, Any] = {}
    idle_since: float | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        last_body = resp.json()
        status = last_body.get("status")
        if status in _SESSION_TERMINAL_ERROR_STATUSES:
            raise AssertionError(f"Session failed during interrupt teardown: {last_body}")
        if status not in _SESSION_NONTERMINAL_STATUSES:
            raise AssertionError(
                f"Session reached unexpected terminal state during interrupt teardown: {last_body}"
            )
        if status == "idle":
            if idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= _INTERRUPT_IDLE_HOLD_SECONDS:
                return
        else:
            idle_since = None
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"Session {session_id} did not become idle within {timeout}s: {last_body}"
    )


def _wait_for_gate_pending(mock_llm_server_url: str, timeout: float = 30) -> None:
    """Poll until a request is blocked on the mock LLM gate."""
    import httpx as _httpx

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = _httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=2.0)
        resp.raise_for_status()
        if resp.json().get("pending"):
            return
        time.sleep(0.1)
    raise AssertionError(f"No gate pending within {timeout}s")


def test_cancel_appends_history_marker_and_followup_sees_it(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Cancel a blocked mock response and verify the follow-up sees the
    cancellation in conversation history.

    Flow:
    1. Open a runner-bound session and send a message. The mock LLM's
       first response blocks on a gate, keeping the session running.
    2. Wait for the mock gate to be pending, then cancel via the
       interrupt endpoint.
    3. Verify the conversation has a cancellation marker item.
    4. Send a follow-up in the same session.
    5. Assert the follow-up completes (the cancellation marker was
       persisted and doesn't break the next turn).
    """
    model = f"mock-cancel-hist-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"cancel-hist-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful assistant. Answer concisely.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # First response blocks so we can interrupt; second is the follow-up.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "This long essay about the Byzantine Empire...", "block": True},
            {"text": "YES, the previous response was cancelled/interrupted."},
        ],
        key=model,
    )

    # Step 1: open session, send a message that triggers the blocking response.
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Write a detailed 2000-word essay about the Byzantine Empire.",
    )

    # Step 2: wait for the mock LLM to be blocked on the gate, then interrupt.
    _wait_for_gate_pending(mock_llm_server_url)
    cancel_resp = http_client.post(f"/v1/sessions/{session_id}/events", json={"type": "interrupt"})
    cancel_resp.raise_for_status()
    assert cancel_resp.status_code in (202, 204)

    # Release the gate so the mock server unblocks and the session can settle.
    release_mock_gate(mock_llm_server_url)

    # Step 3: verify the conversation has the cancellation marker.
    cancellation_items = _wait_for_cancellation_marker(http_client, session_id)
    assert len(cancellation_items) == 1, (
        f"Expected exactly 1 cancellation marker, found {len(cancellation_items)}. "
        f"Cancellation items: {cancellation_items}. "
        f"Items: {_list_all_session_items(http_client, session_id)}"
    )
    _wait_for_idle(http_client, session_id)

    # Step 4: send a follow-up in the same session.
    followup_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Was the previous response cancelled? Answer YES or NO.",
    )

    # Step 5: wait for the follow-up to complete.
    followup_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=followup_id,
        timeout=120,
    )
    assert followup_body["status"] == "completed", (
        f"Follow-up failed: {followup_body.get('error')}"
    )
    text = _extract_all_text(followup_body).upper()
    assert "YES" in text, (
        f"Expected the follow-up to acknowledge the cancellation with 'YES'. Got: {text[:500]}"
    )


def test_cancel_mid_response_followup_succeeds(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Cancel a blocked response and verify the follow-up turn succeeds.

    The cancellation handler must clean up session state so a
    subsequent turn doesn't fail with a 400 or stale-state error.
    This is the mock-LLM equivalent of the original
    ``test_cancel_mid_tool_call_followup_succeeds``: the mock
    server's gate mechanism blocks the LLM response mid-flight
    so we can reliably interrupt and verify recovery.
    """
    model = f"mock-cancel-followup-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"cancel-followup-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful assistant.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # First response blocks so we can interrupt; second is the follow-up.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "This response will be interrupted...", "block": True},
            {"text": "Hello! The previous request was cancelled."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Tell me a very long story.",
    )

    # Wait for the mock to be blocked, then cancel.
    _wait_for_gate_pending(mock_llm_server_url)
    cancel_resp = http_client.post(f"/v1/sessions/{session_id}/events", json={"type": "interrupt"})
    cancel_resp.raise_for_status()
    assert cancel_resp.status_code in (202, 204)

    release_mock_gate(mock_llm_server_url)
    _wait_for_idle(http_client, session_id)

    # Follow-up must succeed — session state should be clean.
    followup_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Never mind. Just say hello.",
    )

    followup_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=followup_id,
        timeout=120,
    )
    assert followup_body["status"] == "completed", (
        f"Follow-up after cancel failed: "
        f"status={followup_body['status']!r}, "
        f"error={followup_body.get('error')}"
    )


def test_cancel_mid_tool_call_followup_succeeds(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
) -> None:
    """
    Cancel a response while tools are executing, then verify the
    follow-up turn succeeds (doesn't fail with 400).

    When a response is cancelled mid-tool-call, dangling
    ``function_call`` items exist without matching
    ``function_call_output``. The cancellation handler must inject
    synthetic outputs for these, otherwise OpenAI rejects the next
    turn with "No tool output found for function call".

    **What breaks if wrong:**

    - If synthetic function_call_output items are not inserted,
      every subsequent message in the conversation fails with
      ``[llm] failed``.
    """
    if using_mock_llm:
        pytest.skip(
            "cancel-mid-tool-call requires real tool execution (web_search) "
            "that the mock LLM cannot orchestrate"
        )

    # Step 1: open session; ask archer to use tools (web_search triggers tool calls).
    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Search the web for 'latest Python release date' "
            "and then search for 'latest Rust release date'. "
            "Report both results."
        ),
    )

    # Step 2: wait for running (tools should be executing), cancel.
    _wait_for_session_running(http_client, session_id, timeout=60)
    # Brief delay so tool calls are persisted.
    time.sleep(2)
    cancel_resp = http_client.post(f"/v1/sessions/{session_id}/events", json={"type": "interrupt"})
    cancel_resp.raise_for_status()
    assert cancel_resp.status_code in (202, 204)
    _wait_for_idle(http_client, session_id)

    # Step 3: follow-up in the same session — would fail with 400 before the fix.
    followup_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Never mind the search. Just say hello.",
    )

    followup_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=followup_id,
        timeout=120,
    )
    # The follow-up must complete, not fail with an LLM error.
    assert followup_body["status"] == "completed", (
        f"Follow-up after tool-call cancel failed: "
        f"status={followup_body['status']!r}, "
        f"error={followup_body.get('error')}"
    )
