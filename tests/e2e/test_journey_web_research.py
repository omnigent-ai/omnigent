"""E2E test: multi-turn context retention user journey (mock LLM).

Exercises multi-turn context retention where the agent receives
information in turn 1 and must reference it in turn 2. Driven
entirely by mock LLM responses -- no web_search or external
API calls needed.

Usage::

    pytest tests/e2e/test_journey_web_research.py -v
"""

from __future__ import annotations

import uuid

import httpx

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text


def test_multi_turn_research_workflow(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """Agent receives facts in turn 1 and reasons about them in turn 2.

    Turn 1: provide the agent with a distinctive fact ("The capital of
    Freedonia is Quuxville, founded in 1847"). Mock acknowledges it.

    Turn 2: ask a follow-up requiring the fact from turn 1 ("When was
    the capital of Freedonia founded?"). Mock references "1847",
    proving multi-turn session dispatch works.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id for session binding.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    model = f"mock-research-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"research-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a research assistant that remembers facts.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # ── Turn 1 mock: acknowledge the fact ────────────────────
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "I've noted the information: The capital of Freedonia "
                    "is Quuxville, founded in 1847. I'll remember this."
                ),
            },
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    # ── Turn 1: provide facts ─────────────────────────────
    resp_id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Here is an important fact: The capital of Freedonia is "
            "Quuxville, founded in 1847. Please acknowledge that you "
            "have received this information."
        ),
    )
    result_1 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=resp_id_1, timeout=60
    )
    text_1 = final_assistant_text(result_1).lower()
    assert "quuxville" in text_1 or "1847" in text_1, (
        f"Turn 1: agent did not acknowledge the fact. Text: {text_1[:500]!r}"
    )

    # ── Turn 2 mock: reference the year ──────────────────────
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": "The capital of Freedonia was founded in 1847."},
        ],
        key=model,
    )

    # ── Turn 2: follow-up requiring context retention ──────
    resp_id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="When was the capital of Freedonia founded? Answer with just the year.",
    )
    result_2 = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=resp_id_2, timeout=60
    )
    text_2 = final_assistant_text(result_2).lower()
    assert "1847" in text_2, (
        "Turn 2 did not reference '1847' from turn 1 -- "
        f"context retention failed. Text: {text_2[:500]!r}"
    )
