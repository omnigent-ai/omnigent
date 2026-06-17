"""E2E test: steering interrupts a running agent.

Verifies that a message delivered to a session whose latest
task is still in flight is steered into that task (rather than
starting a new one), and the agent picks it up in its next
turn.

Both turns route through a runner-bound session
(``POST /v1/sessions/{id}/events``). On the events endpoint,
the server inspects the session's active task: if one is
running with an open inbox, the new item is delivered into it
(:func:`try_deliver`) and tagged with the same ``response_id``;
otherwise a fresh task is created. The helper reads back the
persisted item's ``response_id`` so tests can compare it
against the original task id to confirm whether steering took
the steer-into-running path or fell through to a new turn.

Usage (real LLM)::

    pytest tests/e2e/test_steering.py \
        --llm-api-key $LLM_API_KEY -v

Usage (mock LLM — no key needed)::

    pytest tests/e2e/test_steering.py -v
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
    reset_mock_llm,
    send_user_message_to_session,
)

_RUNNING_POLL_INTERVAL_S = 2


def _wait_for_session_running(
    client: httpx.Client,
    session_id: str,
    timeout: float = 60,
) -> None:
    """
    Poll GET /v1/sessions/{id} until status == "running".

    Raises AssertionError if the session doesn't reach running within
    *timeout* seconds — this makes a failed wait produce a clear error
    rather than silently steering into an idle/completed session.
    """
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
    builtin_tools: list[str] | None = None,
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
        builtin_tools=builtin_tools,
    )
    return name, model


def test_steering_acknowledged(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
    mock_llm_server_url: str | None,
) -> None:
    """
    A message sent while the agent is running is steered into
    the active task and reflected in the final output.

    The agent is asked to write a long essay. While it's
    running, we send "Say only: PINEAPPLE" through the same
    session. The events endpoint must deliver this into the
    running task's inbox, the LLM must re-run with the steered
    message visible, and the final output must contain
    "PINEAPPLE".
    """
    if using_mock_llm:
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
    else:
        agent_name = archer_agent

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    task_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Call sys_read_inbox now and wait for it to return. "
            "Do not reply until sys_read_inbox has returned."
        )
        if not using_mock_llm
        else "Write a long essay about testing.",
    )

    _wait_for_session_running(http_client, session_id, timeout=60)

    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="STOP. Say only: PINEAPPLE",
    )

    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=task_id, timeout=120
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    all_text = _extract_all_text(body)
    assert "PINEAPPLE" in all_text.upper(), (
        f"Steering not acknowledged. Output was:\n{all_text[:500]}"
    )


def test_steering_with_web_search(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
) -> None:
    """
    Steering works when native tool items (web_search_call) are
    in the response. This is the exact scenario that was broken:
    native tool persistence advanced ``last_seen`` past the steer.

    Requires real LLM — web search cannot be mocked.
    """
    if using_mock_llm:
        pytest.skip("requires real LLM (web search tool)")

    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )

    task_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Do these two steps in order:\n"
            "1. Call sys_read_inbox and wait for it to return\n"
            "2. Search the web for the latest news about artificial intelligence\n"
            "Do NOT skip any steps."
        ),
    )

    _wait_for_session_running(http_client, session_id, timeout=60)

    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="STOP ALL STEPS. Say only: PINEAPPLE",
    )

    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=task_id, timeout=240
    )
    assert body["status"] == "completed"

    all_text = _extract_all_text(body)
    assert "PINEAPPLE" in all_text.upper(), (
        f"Steering with web search not acknowledged: {all_text[:300]}"
    )


def test_steering_after_completed_starts_new_turn(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
    mock_llm_server_url: str | None,
) -> None:
    """
    A message sent after the task completes creates a new turn,
    not a steer.
    """
    if using_mock_llm:
        reset_mock_llm(mock_llm_server_url)
        agent_name, model = _mock_agent(http_client, mock_llm_server_url)
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "Hello!"}, {"text": "The answer is 4."}],
            key=model,
        )
    else:
        agent_name = archer_agent

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    task_id = send_user_message_to_session(
        http_client, session_id=session_id, content="Say hello."
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=task_id, timeout=30
    )
    assert body["status"] == "completed"

    task2_id = send_user_message_to_session(
        http_client, session_id=session_id, content="What is 2+2?"
    )

    body2 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=task2_id, timeout=30
    )
    assert body2["status"] == "completed"
    text = _extract_all_text(body2)
    assert "4" in text, f"Expected answer to 2+2, got: {text[:100]}"


def test_steering_during_multi_tool_iterations(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    using_mock_llm: bool,
    mock_llm_server_url: str | None,
) -> None:
    """
    Steering is picked up between tool call iterations when the
    agent makes multiple sequential tool calls.

    In mock mode the mock server returns tool-call responses for
    ``sys_read_inbox`` and ``list_files``; the harness executes
    them as real built-in tools. The steer arrives while
    ``sys_read_inbox`` blocks, then the subsequent tool iterations
    must not skip over the steered message.
    """
    if using_mock_llm:
        reset_mock_llm(mock_llm_server_url)
        agent_name, model = _mock_agent(http_client, mock_llm_server_url)
        # Response sequence (sys_read_inbox is a runner-level system
        # tool, always registered — no spec declaration needed):
        # 1. sys_read_inbox → harness executes, blocks on inbox
        # 2. (steer arrives, inbox returns, harness posts tool result)
        #    sys_read_inbox again → returns immediately (inbox drained)
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
    else:
        agent_name = archer_agent

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    task_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Do these steps in order, one tool call at a time:\n"
            "1. Call sys_read_inbox and wait for it to return\n"
            "2. Call list_files\n"
            "3. Call list_files again\n"
            "Do NOT skip any steps."
        ),
    )

    _wait_for_session_running(http_client, session_id, timeout=60)

    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="STOP ALL STEPS. Say only: PINEAPPLE",
    )

    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=task_id, timeout=240
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    all_text = _extract_all_text(body)
    tool_count = len([i for i in body.get("output", []) if i.get("type") == "function_call"])
    assert "PINEAPPLE" in all_text.upper(), (
        f"Steering during multi-tool iterations not acknowledged. "
        f"Tool calls before steer: {tool_count}. Output: {all_text[:500]}"
    )
    assert tool_count >= 1, "Expected at least 1 tool call before the steer was processed"


def _extract_all_text(body: dict[str, Any]) -> str:
    """
    Concatenate all assistant output_text blocks.

    :param body: The terminal response body.
    :returns: All assistant text joined by newlines.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)
