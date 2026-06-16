"""E2E journey test: skill loading and execution.

Verifies the full user journey of loading a bundled skill and
using its content in a follow-up turn:

1. Create session with the archer agent (has ``deep-research`` skill).
2. Ask the agent to load the skill.
3. Verify ``load_skill`` tool was called.
4. Verify the tool output contains skill instructions (not an error).
5. Ask a follow-up that leverages the loaded skill's reference file.
6. Verify the agent's response references the skill's content.

Usage::

    pytest tests/e2e/test_journey_skill_loading.py \
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
)


def _extract_all_text(body: dict[str, Any]) -> str:
    """Concatenate all assistant output_text blocks from a response body."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _extract_tool_names(body: dict[str, Any]) -> list[str]:
    """Extract all function_call tool names from a response body."""
    return [
        item.get("name", "")
        for item in body.get("output", [])
        if item.get("type") == "function_call"
    ]


def _extract_tool_results(body: dict[str, Any]) -> list[str]:
    """Extract all function_call_output strings from a response body."""
    return [
        item.get("output", "")
        for item in body.get("output", [])
        if item.get("type") == "function_call_output"
    ]


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_skill_loading_journey(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
) -> None:
    """Full journey: load a skill, read its reference file, use its content.

    Steps:
    1. Create a session with the archer agent.
    2. Ask the agent to load the ``deep-research`` skill.
    3. Verify ``load_skill("deep-research")`` was called.
    4. Verify the tool output contains the skill instructions.
    5. Send a follow-up asking the agent to use the skill knowledge.
    6. Verify the response references the skill's content.

    :param http_client: HTTP client pointed at the live e2e server.
    :param archer_agent: The uploaded archer agent name.
    :param live_runner_id: Runner id bound to the session.
    """
    # ── Step 1: Create session ──────────────────────────────
    session_id = create_runner_bound_session(
        http_client,
        agent_name=archer_agent,
        runner_id=live_runner_id,
    )

    # ── Step 2: Ask agent to load the deep-research skill ───
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Call load_skill with name=deep-research. "
            "Then call read_skill_file with "
            "skill_name=deep-research and "
            "path=references/research-checklist.md. "
            "Tell me what the checklist says."
        ),
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=300,
    )

    assert body["status"] == "completed", (
        f"Expected completed, got {body['status']}. Error: {body.get('error')}"
    )

    # ── Step 3: Verify load_skill was called ────────────────
    tool_names = _extract_tool_names(body)
    assert "load_skill" in tool_names, (
        f"Expected load_skill tool call. Tool calls: {tool_names}. "
        f"The agent may not have loaded the skill."
    )

    # ── Step 4: Verify skill loaded (no error in output) ────
    tool_results = _extract_tool_results(body)

    # The load_skill result should contain the skill instructions
    # (from SKILL.md) — not an error message.
    skill_loaded = any(
        "deep-research" in r.lower() or "research" in r.lower() for r in tool_results
    )
    assert skill_loaded, (
        f"Expected skill instructions in load_skill output. "
        f"Tool results: {[r[:200] for r in tool_results]}. "
        f"The skill may not have been found or loaded correctly."
    )

    # The read_skill_file result should contain the checklist content.
    assert "read_skill_file" in tool_names, (
        f"Expected read_skill_file tool call. Tool calls: {tool_names}. "
        f"The agent may not have read the bundled reference file."
    )

    checklist_found = any("3 independent sources" in r for r in tool_results)
    assert checklist_found, (
        f"Expected '3 independent sources' in read_skill_file result "
        f"(from research-checklist.md). "
        f"Tool results: {[r[:200] for r in tool_results]}. "
        f"The bundled file may not have been extracted correctly."
    )

    # ── Step 5: Follow-up using skill knowledge ─────────────
    followup_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Using the research checklist from the deep-research skill "
            "you just loaded, what are the key steps before presenting "
            "a conclusion?"
        ),
    )

    followup_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=followup_response_id,
        timeout=300,
    )

    assert followup_body["status"] == "completed", (
        f"Follow-up failed: {followup_body['status']}. Error: {followup_body.get('error')}"
    )

    # ── Step 6: Verify skill context was used ───────────────
    followup_text = _extract_all_text(followup_body)

    # The agent should reference the checklist content: verifying
    # against 3 independent sources, preferring primary sources, etc.
    text_lower = followup_text.lower()
    assert "source" in text_lower or "verify" in text_lower, (
        f"Expected the agent to reference the research checklist content "
        f"(sources, verification). Got: {followup_text[:500]}"
    )
