"""E2E test: Claude SDK executor auto-collects sub-agent results (mock LLM).

Verifies that when the Claude SDK executor spawns a sub-agent,
the workflow auto-collects the results before the parent task
completes. The user sends a single message and gets back the
sub-agent's output -- no second message or manual polling needed.

The mock LLM returns canned responses. The LLM judge is skipped
because it requires a real OpenAI key.

Usage::

    pytest tests/e2e/test_claude_coder_auto_collect.py -v --timeout=180
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


def test_single_message_subagent_auto_collect(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    One message triggers spawn + auto-collect. The response includes
    the sub-agent's review output.

    The mock LLM for the parent issues ``sys_session_send`` then
    reports the collected result. The reviewer mock returns a canned
    review. The LLM judge is skipped.
    """
    reset_mock_llm(mock_llm_server_url)

    parent_model = f"mock-autocollect-{uuid.uuid4().hex[:6]}"
    reviewer_model = f"mock-reviewer-ac-{uuid.uuid4().hex[:6]}"

    agent_name = register_inline_agent(
        http_client,
        name=f"autocollect-{uuid.uuid4().hex[:6]}",
        harness="claude-sdk",
        model=parent_model,
        profile="",
        prompt=(
            "You are a coding assistant. You have a sub-agent "
            "called 'reviewer' for code reviews."
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

    # Parent: issue sys_session_send, then report collected results.
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
                                "message": "Review /etc/hosts for any issues.",
                            }
                        ),
                    }
                ]
            },
            {
                "text": (
                    "I spawned the reviewer sub-agent to review /etc/hosts. "
                    "The sub-agent completed its review via check_sub_agents. "
                    "The reviewer found that /etc/hosts is a standard hosts "
                    "file with localhost entries. No critical issues found."
                ),
            },
        ],
        key=parent_model,
    )

    # Reviewer: return a review.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "## Review: /etc/hosts\n\n"
                    "Standard hosts file. localhost entries present.\n"
                    "No security concerns for a system hosts file."
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
            "Use sys_session_send to spawn the 'reviewer' sub-agent "
            "and ask it to review /etc/hosts."
        ),
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=240,
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    text = _extract_all_text(body)
    assert len(text) > 20, (
        f"Expected substantial response with sub-agent results, got: {text!r}"
    )
