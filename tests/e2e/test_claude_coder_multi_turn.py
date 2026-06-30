"""E2E test: Claude SDK executor multi-turn tool call awareness (mock LLM).

Verifies that the Claude SDK subprocess persists across tasks and
retains awareness of tool calls from prior turns. The mock LLM
returns canned responses for both turns. The LLM judge is skipped
because it requires a real OpenAI key.

Usage::

    pytest tests/e2e/test_claude_coder_multi_turn.py -v --timeout=120
"""

from __future__ import annotations

import json
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

    :param body: The terminal response body from GET /v1/responses/{id}.
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


def _has_tool_call_named(body: dict[str, Any], substring: str) -> bool:
    """
    Check if the response output contains a function_call with
    a name containing ``substring`` (case-insensitive).

    :param body: The terminal response body.
    :param substring: Substring to match against tool names.
    :returns: True if any matching function_call found.
    """
    lower = substring.lower()
    for item in body.get("output", []):
        if item.get("type") == "function_call":
            name = item.get("name", "")
            if lower in name.lower():
                return True
    return False


def test_claude_coder_remembers_tool_calls(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Multi-turn test: Claude SDK subprocess retains tool call context.

    Turn 1: The mock LLM issues a Bash tool call and then returns
    a star count. Turn 2: The mock returns text about the tools
    used. The LLM judge is skipped.
    """
    reset_mock_llm(mock_llm_server_url)

    model = f"mock-multiturn-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"multiturn-{uuid.uuid4().hex[:6]}",
        harness="claude-sdk",
        model=model,
        profile="",
        prompt=("You are a coding assistant. You can run shell commands using the Bash tool."),
        mock_llm_base_url=mock_llm_server_url,
    )

    # Turn 1: LLM issues a Bash tool call, then reports the result.
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Turn 1, message 1: issue Bash tool call
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": "Bash",
                        "arguments": json.dumps(
                            {"command": "gh repo view mlflow/mlflow --json stargazerCount"}
                        ),
                    }
                ]
            },
            # Turn 1, message 2: report the result
            {
                "text": ("The mlflow/mlflow repository has approximately 19,500 GitHub stars."),
            },
            # Turn 2: respond about tools used
            {
                "text": (
                    "I used the Bash tool to run the `gh repo view` "
                    "command to fetch the star count from GitHub."
                ),
            },
        ],
        key=model,
    )

    # ---- Turn 1: fetch GitHub stars ----
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id_1 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "How many GitHub stars does the mlflow/mlflow "
            "repository have? Use the gh CLI to find out."
        ),
    )

    body_1 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id_1,
        timeout=120,
    )
    assert body_1["status"] == "completed", f"Turn 1 failed: {body_1.get('error', 'unknown')}"

    text_1 = _extract_all_text(body_1)
    assert _has_tool_call_named(body_1, "bash") or "star" in text_1.lower(), (
        f"Turn 1 didn't seem to fetch stars. Output: {text_1[:500]}"
    )

    # ---- Turn 2: ask about tools used ----
    response_id_2 = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="What tools did you use to find that out?",
    )

    body_2 = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id_2,
        timeout=120,
    )
    assert body_2["status"] == "completed", f"Turn 2 failed: {body_2.get('error', 'unknown')}"

    text_2 = _extract_all_text(body_2)
    assert len(text_2) > 10, f"Turn 2 produced no meaningful output. Text: {text_2!r}"
