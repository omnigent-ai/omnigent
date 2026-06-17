"""E2E test: default executor auto-collects sub-agent results.

Verifies that sub-agent auto-collection works for the default (LLM)
executor path — the same task store query that was added for the
Claude SDK executor. Uses the archer agent which has fact_checker
and summarizer sub-agents.

Usage::

    pytest tests/e2e/test_default_executor_auto_collect.py \
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_until_terminal,
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


def test_agent_spawns_and_auto_collects(
    http_client: httpx.Client,
    archer_agent: str,
    live_runner_id: str,
    llm_api_key: str,
    openai_judge_api_key: str,
) -> None:
    """
    Single message triggers spawn + auto-collect for the default
    executor. The archer agent spawns a sub-agent and the workflow
    auto-collects the results before completing.

    This verifies the unified spawn tracking path (task store query)
    works for the default executor, not just the Claude SDK executor.

    **What breaks if the feature is wrong:**

    - If the task store query for child tasks doesn't work, spawned
      sub-agents are never discovered → auto-collect skips → the
      parent completes without sub-agent results.
    """
    session_id = create_runner_bound_session(
        http_client, agent_name=archer_agent, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use sys_session_send to spawn the summarizer. "
            "Ask it to summarize the concept of photosynthesis "
            "in exactly 2 sentences."
        ),
    )

    body = poll_until_terminal(http_client, response_id, timeout=240)
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    text = _extract_all_text(body)
    assert len(text) > 50, f"Expected substantial output, got: {text!r}"

    # Use LLM judge to verify the response contains collected
    # sub-agent results about photosynthesis.
    from mlflow.genai.judges import make_judge

    os.environ["OPENAI_API_KEY"] = openai_judge_api_key

    judge = make_judge(
        name="default_executor_auto_collect",
        instructions=(
            "You are evaluating whether an AI assistant's response "
            "contains results from a sub-agent that was spawned to "
            "summarize photosynthesis.\n\n"
            "The assistant's response is:\n"
            "{{ outputs }}\n\n"
            "Does the response contain a summary of photosynthesis "
            "that appears to have been produced by the sub-agent "
            "(not just the assistant saying 'I spawned it')? The "
            "summary should mention key concepts like sunlight, "
            "carbon dioxide, or energy conversion.\n\n"
            "Return True if the response includes substantive "
            "photosynthesis content. Return False if it just says "
            "the sub-agent was spawned without showing results."
        ),
        feedback_value_type=bool,
    )

    feedback = judge(outputs=text)
    assert feedback.value is True, (
        f"LLM judge: response did not contain sub-agent results.\n"
        f"Rationale: {feedback.rationale}\n"
        f"Output: {text[:500]}"
    )
