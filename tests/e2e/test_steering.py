"""E2E test: steering interrupts a running agent.

Verifies that a message delivered to a session whose latest
task is still in flight is steered into that task (rather than
starting a new one), and the agent picks it up in its next
turn. Runs against the mock LLM server.

``test_steering_with_web_search`` uses a mock ``web_search_call``
native tool item to exercise the steering cursor fix without
requiring a real web search.

Usage::

    pytest tests/e2e/test_steering.py -v
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

_RUNNING_POLL_INTERVAL_S = 2


def _wait_for_session_running(
    client: httpx.Client,
    session_id: str,
    timeout: float = 60,
) -> None:
    """Poll GET /v1/sessions/{id} until status == "running"."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/v1/sessions/{session_id}")
        r.raise_for_status()
        if r.json().get("status") == "running":
            return
        time.sleep(_RUNNING_POLL_INTERVAL_S)
    raise AssertionError(
        f"Session {session_id} did not reach 'running' within {timeout}s; "
        f"last status={client.get(f'/v1/sessions/{session_id}').json().get('status')!r}"
    )


def _mock_agent(
    http_client: httpx.Client,
    mock_llm_server_url: str | None,
    *,
    prompt: str = "You are a test assistant. Follow instructions exactly.",
) -> tuple[str, str]:
    """Register an inline agent for mock mode, return (name, model)."""
    model = f"mock-steer-{uuid.uuid4().hex[:6]}"
    name = register_inline_agent(
        http_client,
        name=f"steer-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=prompt,
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    return name, model


def test_steering_acknowledged(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    A message sent while the agent is running is steered into
    the active task and reflected in the final output.
    """

    reset_mock_llm(mock_llm_server_url)
    agent_name, model = _mock_agent(http_client, mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "Working on your request..."},
            {"text": "PINEAPPLE"},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    task_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Write a long essay about testing.",
    )

    _wait_for_session_running(http_client, session_id, timeout=60)

    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="STOP. Say only: PINEAPPLE",
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=task_id,
        timeout=120,
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    all_text = _extract_all_text(body)
    assert "PINEAPPLE" in all_text.upper(), (
        f"Steering not acknowledged. Output was:\n{all_text[:500]}"
    )


def test_steering_with_tool_items(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    Steering works when tool items are in the response.

    Originally tested with ``web_search_call`` native items; now
    uses ``sys_read_inbox`` tool calls to exercise the same cursor
    fix: tool items must not advance ``last_seen`` past the steer.
    """
    model = f"mock-ws-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"ws-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a test assistant.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_inbox1",
                        "name": "sys_read_inbox",
                        "arguments": "{}",
                    },
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_inbox2",
                        "name": "sys_read_inbox",
                        "arguments": "{}",
                    },
                ],
            },
            {"text": "PINEAPPLE"},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    task_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Do two inbox reads then respond.",
    )

    _wait_for_session_running(http_client, session_id, timeout=60)

    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="STOP ALL STEPS. Say only: PINEAPPLE",
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=task_id,
        timeout=240,
    )
    assert body["status"] == "completed"

    all_text = _extract_all_text(body)
    assert "PINEAPPLE" in all_text.upper(), (
        f"Steering with tool items not acknowledged: {all_text[:300]}"
    )


def test_steering_after_completed_starts_new_turn(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    A message sent after the task completes creates a new turn,
    not a steer.
    """

    reset_mock_llm(mock_llm_server_url)
    agent_name, model = _mock_agent(http_client, mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello!"}, {"text": "The answer is 4."}],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    task_id = send_user_message_to_session(
        http_client, session_id=session_id, content="Say hello."
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=task_id,
        timeout=30,
    )
    assert body["status"] == "completed"

    task2_id = send_user_message_to_session(
        http_client, session_id=session_id, content="What is 2+2?"
    )
    body2 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=task2_id,
        timeout=30,
    )
    assert body2["status"] == "completed"
    text = _extract_all_text(body2)
    assert "4" in text, f"Expected answer to 2+2, got: {text[:100]}"


def test_steering_during_multi_tool_iterations(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    Steering is picked up between tool call iterations when the
    agent makes multiple sequential sys_read_inbox calls.
    """

    reset_mock_llm(mock_llm_server_url)
    agent_name, model = _mock_agent(http_client, mock_llm_server_url)
    # 1. sys_read_inbox → blocks on inbox
    # 2. (steer arrives, inbox returns) → sys_read_inbox again
    # 3. Final text with PINEAPPLE
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_inbox1",
                        "name": "sys_read_inbox",
                        "arguments": "{}",
                    },
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_inbox2",
                        "name": "sys_read_inbox",
                        "arguments": "{}",
                    },
                ],
            },
            {"text": "PINEAPPLE"},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    task_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Call sys_read_inbox twice, then respond.",
    )

    _wait_for_session_running(http_client, session_id, timeout=60)

    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="STOP ALL STEPS. Say only: PINEAPPLE",
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=task_id,
        timeout=240,
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    all_text = _extract_all_text(body)
    tool_count = len([i for i in body.get("output", []) if i.get("type") == "function_call"])
    assert "PINEAPPLE" in all_text.upper(), (
        f"Steering during multi-tool iterations not acknowledged. "
        f"Tool calls: {tool_count}. Output: {all_text[:500]}"
    )
    assert tool_count >= 1, "Expected at least 1 tool call before the steer was processed"


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all assistant output_text blocks."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)
