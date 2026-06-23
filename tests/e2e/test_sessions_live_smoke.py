"""Tiny live smoke for the maintained ``/v1/sessions`` path.

Proves one harness-backed turn works through the production session
flow: register an inline agent, create and runner-bind a session,
POST a user message, poll to idle, assert the response contains the
expected marker.

Runs against the mock LLM server — no API key needed.

Usage::

    pytest tests/e2e/test_sessions_live_smoke.py -v
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
def test_live_sessions_path_round_trips_through_openai_agents_harness(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """A harness-backed session turn reaches the LLM and returns text."""
    marker = "SESSIONS_LIVE_SMOKE_OK"
    model = f"mock-smoke-{uuid.uuid4().hex[:6]}"

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"smoke-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a terse smoke-test assistant.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    configure_mock_llm(mock_llm_server_url, [{"text": marker}], key=model)

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Reply with exactly the literal string {marker} "
            "and nothing else. Do not call tools or sub-agents."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )

    assert body["status"] == "completed", (
        f"sessions live smoke failed: status={body['status']!r}, "
        f"error={body.get('error')!r}, output={body.get('output')!r}"
    )
    text = final_assistant_text(body)
    assert marker in text, f"marker {marker!r} missing from assistant text: {text!r}"
