"""E2E test: Claude SDK executor spawning a sub-agent (mock LLM).

Verifies that the Claude SDK executor can call ``sys_session_send``
(a server-side omnigent tool) through the unified ``call_tool``
callback, and that the sub-agent executes and returns results.

The mock LLM is configured to issue a ``sys_session_send`` tool
call and then summarise the result. The LLM judge is skipped
because verifying review quality requires a real OpenAI key.

Usage::

    pytest tests/e2e/test_claude_coder_subagent.py -v --timeout=180
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

    :param body: The terminal response body.
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



def test_claude_coder_spawns_reviewer(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The Claude SDK executor spawns a reviewer sub-agent via
    ``sys_session_send`` and collects the result.

    The mock LLM for the parent agent is configured to issue a
    ``sys_session_send`` tool call, then a ``check_sub_agents``
    call, then summarise. The reviewer sub-agent mock returns a
    canned review. The LLM judge is skipped.
    """
    reset_mock_llm(mock_llm_server_url)

    parent_model = f"mock-parent-{uuid.uuid4().hex[:6]}"
    reviewer_model = f"mock-reviewer-{uuid.uuid4().hex[:6]}"

    # Register parent agent with reviewer sub-agent.
    agent_name = register_inline_agent(
        http_client,
        name=f"coder-subagent-{uuid.uuid4().hex[:6]}",
        harness="claude-sdk",
        model=parent_model,
        profile="",
        prompt=(
            "You are a coding assistant. You have a sub-agent "
            "called 'reviewer' that reviews code."
        ),
        mock_llm_base_url=mock_llm_server_url,
        extra_config={
            "tools": {
                "reviewer": {
                    "type": "agent",
                    "description": "Reviews code for bugs and quality.",
                    "executor": {
                        "harness": "claude-sdk",
                        "model": reviewer_model,
                    },
                    "prompt": "You are a code reviewer.",
                },
            },
        },
    )

    # Configure parent LLM responses: issue sys_session_send, then
    # summarise after collecting the result.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "reviewer",
                                "message": "Review /tmp/review_target.py for bugs.",
                            }
                        ),
                    }
                ]
            },
            {
                "text": (
                    "I spawned the reviewer sub-agent to review the file. "
                    "The reviewer found a division-by-zero risk in the "
                    "divide function and a range(len) antipattern in "
                    "process. Here is the detailed review output from "
                    "the sub-agent."
                ),
            },
        ],
        key=parent_model,
    )

    # Configure reviewer LLM responses.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "## Code Review: /tmp/review_target.py\n\n"
                    "### Critical Issues\n"
                    "- divide(a, b): No zero-division guard.\n\n"
                    "### Improvements\n"
                    "- process(items): Use `for item in items` "
                    "instead of range(len(items)).\n\n"
                    "### Summary\n"
                    "Two issues found: missing error handling and "
                    "an anti-pattern."
                ),
            },
        ],
        key=reviewer_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "You have a sub-agent called 'reviewer'. Use the "
            "sys_session_send tool to spawn it and ask it to "
            "review /tmp/review_target.py."
        ),
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert body["status"] == "completed", f"Sub-agent task failed: {body.get('error')}"

    text = _extract_all_text(body)
    assert len(text) > 20, f"Expected substantial output, got: {text!r}"
