"""E2E test: OpenAI Agents SDK executor basics (mock LLM).

Verifies that the AgentsSdkExecutor runs single-turn and
multi-turn conversations correctly. An inline agent is registered
pointing at the mock LLM server; the mock response queue supplies
the expected answers directly.

Both turns route through a runner-bound session — the alpha
runner-state contract requires ``conversations.runner_id``
to be set before dispatch, so we create the session and PATCH a
runner before any ``/events`` POST.

Usage::

    pytest tests/e2e/test_agents_sdk_basic.py -v
"""

from __future__ import annotations

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


def test_agents_sdk_single_turn_completes(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Basic smoke test: the Agents SDK executor runs a single
    turn and produces a completed response with correct text.

    **What breaks if wrong:**

    - If ``_ensure_sdk()`` fails, ``from_spec`` raises
      ``ImportError`` and the task fails immediately.
    - If ``_build_model_settings`` maps config incorrectly,
      the LLM rejects the parameters (400 error).
    - If ``_map_event`` doesn't map ``TextChunk`` correctly,
      no text appears in the response output.
    - If ``TurnComplete`` is never yielded, the response
      stays in ``in_progress`` forever and the poll times out.
    """
    model = f"mock-basic-single-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"basic-single-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful math assistant.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    configure_mock_llm(mock_llm_server_url, [{"text": "The answer is 4."}], key=model)

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="What is 2 + 2? Reply with just the number.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=60,
    )
    assert body["status"] == "completed", (
        f"Expected completed, got {body['status']}. Error: {body.get('error')}."
    )

    text = _extract_all_text(body)
    assert "4" in text, f"Expected '4' in response: {text[:300]}"


def test_agents_sdk_multi_turn_remembers(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Two-turn conversation: the agent remembers turn 1 content
    in turn 2 via history replay.

    Turn 1: state a fact. Turn 2: ask about it. Both turns share
    the same session so the second turn dispatches against the
    same persisted conversation items (no ``previous_response_id``
    plumbing needed — same-session continuation is implicit).

    **What breaks if wrong:**

    - If ``_messages_to_input`` doesn't pass history correctly,
      the SDK sees no prior context and can't answer.
    - If the workflow doesn't load prior items into ``messages``,
      the executor receives an empty history.
    """
    model = f"mock-basic-multi-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"basic-multi-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful assistant.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "OK, noted."},
            {"text": "Your name is Zephyr and you live in Portland."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    # Turn 1: state a fact.
    id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="My name is Zephyr and I live in Portland.",
    )
    body_1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=id_1, timeout=60
    )
    assert body_1["status"] == "completed", f"Turn 1 failed: {body_1.get('error')}"

    # Turn 2: ask about the fact, same session.
    id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="What is my name and where do I live?",
    )
    body_2 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=id_2, timeout=60
    )
    assert body_2["status"] == "completed", f"Turn 2 failed: {body_2.get('error')}"

    text = _extract_all_text(body_2).lower()
    assert "zephyr" in text, f"Expected 'zephyr' in turn 2 response: {text[:300]}"
    assert "portland" in text, f"Expected 'portland' in turn 2 response: {text[:300]}"
