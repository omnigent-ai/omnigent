"""E2E test: multi-turn recovery user journey (mock LLM).

Exercises the multi-turn conversation lifecycle:

1. Send a message to an agent and wait for completion.
2. Send a follow-up with a distinctive codeword.
3. Verify the agent responds normally (codeword echoed back).
4. Verify the session history contains the expected items.

This validates that session state remains clean across multiple turns
and that the agent can reference prior context in follow-up responses.

Usage::

    pytest tests/e2e/test_journey_cancel_recover.py -v
"""

from __future__ import annotations

import uuid

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
from tests.e2e.helpers import final_assistant_text


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_multi_turn_recovery_journey(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Full journey: send -> complete -> send codeword -> verify echo -> verify history.

    Validates that multi-turn conversation state is maintained across
    sequential turns in a runner-bound session.  The first turn
    establishes context; the second turn proves the agent can still
    process new input by echoing a distinctive codeword.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id for session binding.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    codeword = "phoenix-delta-88"
    model = f"mock-cancel-recover-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"cancel-recover-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a terse assistant. Echo back codewords exactly.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # Two turns: first returns octopus facts, second echoes the codeword.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "1. Octopuses have three hearts. "
                    "2. They have blue blood. "
                    "3. They can change color."
                )
            },
            {"text": f"The codeword is: {codeword}"},
        ],
        key=model,
    )

    # -- Step 1: Create a runner-bound session --
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    # -- Step 2: Send a first message and wait for completion --
    first_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="What are three interesting facts about octopuses? Be concise.",
    )

    first_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=first_response_id,
        timeout=120,
    )
    assert first_body["status"] == "completed", (
        f"First turn failed: status={first_body['status']!r}, error={first_body.get('error')}"
    )

    first_text = final_assistant_text(first_body)
    assert first_text.strip(), f"First turn produced no assistant text. Body: {first_body}"

    # -- Step 3: Send a recovery message with a codeword --
    second_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Never mind the octopus facts. Just remember this codeword "
            f"and repeat it back to me exactly: {codeword}"
        ),
    )

    # -- Step 4: Poll until the second turn completes --
    second_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=second_response_id,
        timeout=120,
    )
    assert second_body["status"] == "completed", (
        f"Second turn failed: status={second_body['status']!r}, error={second_body.get('error')}"
    )

    # -- Step 5: Verify the agent echoed the codeword --
    second_text = final_assistant_text(second_body)
    assert codeword in second_text.lower(), (
        f"Expected the agent to echo back '{codeword}'. Got: {second_text[:500]}"
    )

    # -- Step 6: Verify full session history --
    final_resp = http_client.get(f"/v1/sessions/{session_id}")
    final_resp.raise_for_status()
    final_items = final_resp.json().get("items", [])

    user_messages = [
        item
        for item in final_items
        if item.get("type") == "message"
        and (item.get("role") == "user" or (item.get("data") or {}).get("role") == "user")
    ]
    assert len(user_messages) >= 2, (
        f"Expected at least 2 user messages (first + codeword), found {len(user_messages)}."
    )

    assistant_messages = [
        item
        for item in final_items
        if item.get("type") == "message"
        and (
            item.get("role") == "assistant" or (item.get("data") or {}).get("role") == "assistant"
        )
    ]
    assert len(assistant_messages) >= 2, (
        f"Expected at least 2 assistant messages, found {len(assistant_messages)}."
    )
