"""E2E test: Claude SDK executor discovering and loading skills (mock LLM).

Verifies that the Claude SDK's native Skill tool discovers skills
written to ``.claude/skills/`` by the executor's ``on_task_start``
and can load their content.

The mock LLM returns a canned response claiming to have found and
loaded skills. The LLM judge is skipped because it requires a real
OpenAI key and the skill content verification is not testable with
mock responses.

Usage::

    pytest tests/e2e/test_claude_coder_skills.py -v --timeout=120
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


def test_claude_coder_lists_and_loads_skills(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Claude discovers custom skills via the SDK's Skill tool and
    can load their content.

    The mock LLM is configured to return text about skills. The
    test verifies the dispatch flow completes successfully. The
    LLM judge is skipped because verifying skill content requires
    a real LLM + real OpenAI key for the judge.
    """
    reset_mock_llm(mock_llm_server_url)

    model = f"mock-skills-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"skills-test-{uuid.uuid4().hex[:6]}",
        harness="claude-sdk",
        model=model,
        profile="",
        prompt=(
            "You are a coding assistant with custom skills. "
            "When asked to list skills, report what you find."
        ),
        mock_llm_base_url=mock_llm_server_url,
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    "I found the following custom skills:\n\n"
                    "1. **code-review** - A structured code review skill that "
                    "prioritizes security > correctness > performance > style.\n\n"
                    "2. **systematic-debugging** - A debugging skill.\n\n"
                    "Here is the content of the code-review skill:\n\n"
                    "## Critical Issues\n"
                    "- [file:line] Description\n\n"
                    "## Improvements\n"
                    "- [file:line] Description\n\n"
                    "## Looks Good\n"
                    "- Items that are well implemented"
                ),
            },
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "List your available skills. Then load the "
            "code-review skill and show me its full content."
        ),
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    text = _extract_all_text(body)
    assert len(text) > 0, "Expected non-empty response text"
