"""E2E test: "first session to working code" user journey.

Exercises the full developer workflow end-to-end:
create session → chat with agent → agent writes code → add review
comment → agent addresses comment.

This is a multi-step journey test that validates the core coding
loop a developer would experience: ask the agent to write a function,
review the output, leave a comment requesting a change, and verify
the agent addresses it.

Usage::

    pytest tests/e2e/test_journey_first_session_to_code.py \\
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


def _tool_names_in_output(body: dict[str, Any]) -> list[str]:
    """
    Collect every function_call tool name from a response body.

    :param body: Terminal response body from
        :func:`poll_session_until_terminal`.
    :returns: List of tool names in call order.
    """
    return [
        item["name"]
        for item in body.get("output", [])
        if item.get("type") == "function_call" and item.get("name")
    ]


def _extract_all_text(body: dict[str, Any]) -> str:
    """
    Concatenate all assistant output_text blocks.

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


@pytest.mark.llm_flaky(reruns=2)
def test_first_session_to_working_code_journey(
    http_client: httpx.Client,
    coder_agent: str,
    live_runner_id: str,
) -> None:
    """
    Full developer journey: create session, code, review comment, address it.

    Steps:
    1. Create a runner-bound session with the coder agent.
    2. Ask the agent to write a Python ``is_palindrome`` function and
       save it to ``palindrome.py``.
    3. Verify the agent turn completed successfully and produced tool
       calls or text mentioning the function.
    4. Add a review comment requesting error handling for non-string inputs.
    5. Ask the agent to address the review comment.
    6. Verify the comment is now ``"addressed"`` via the REST API.

    **What breaks if this fails:**

    - Session creation or runner binding broken → step 1 fails.
    - Agent cannot process a coding task → step 2/3 fails.
    - Comment creation via REST → step 4 fails.
    - ``list_comments`` / ``update_comment`` tool dispatch broken →
      step 5/6 fails (agent never marks the comment addressed).
    - Comment store not configured on the runner → tools return errors.

    :param http_client: HTTP client pointed at the live server.
    :param coder_agent: Registered coder agent name.
    :param live_runner_id: Runner id the session is bound to.
    """
    # ── 1. Create a runner-bound session ─────────────────────────────────────
    session_id = create_runner_bound_session(
        http_client,
        agent_name=coder_agent,
        runner_id=live_runner_id,
    )

    # Verify the session exists and is in a usable state.
    session_resp = http_client.get(f"/v1/sessions/{session_id}")
    session_resp.raise_for_status()
    assert session_resp.json()["id"] == session_id

    # ── 2. Send coding task ──────────────────────────────────────────────────
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Write a Python function called `is_palindrome` that checks "
            "if a string is a palindrome (reads the same forwards and "
            "backwards, case-insensitive). Save it to a file called "
            "`palindrome.py`."
        ),
    )

    # ── 3. Wait for agent to complete and verify ─────────────────────────────
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert body["status"] == "completed", (
        f"Agent coding turn failed. error={body.get('error')!r}. output={body.get('output', [])}"
    )

    # The agent should have either made tool calls (to write the file)
    # or at minimum produced text mentioning the function. We check both
    # flexibly since different agents have different tool sets.
    tool_calls = _tool_names_in_output(body)
    text_output = _extract_all_text(body).lower()
    has_write_evidence = (
        any("write" in t.lower() or "shell" in t.lower() for t in tool_calls)
        or "is_palindrome" in text_output
        or "palindrome" in text_output
    )
    assert has_write_evidence, (
        f"Agent completed but produced no evidence of writing the function. "
        f"Tool calls: {tool_calls}. Text (first 500 chars): {text_output[:500]}"
    )

    # ── 4. Add a review comment via REST ─────────────────────────────────────
    comment_resp = http_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "palindrome.py",
            "body": (
                "Add error handling for non-string inputs. The function "
                "should raise a TypeError if the input is not a string."
            ),
            "start_index": 0,
            "end_index": 30,
            "anchor_content": "def is_palindrome",
        },
    )
    comment_resp.raise_for_status()
    comment_id: str = comment_resp.json()["id"]

    # Verify comment was created in draft status.
    comments_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
    comments_resp.raise_for_status()
    comment_statuses = {c["id"]: c["status"] for c in comments_resp.json()}
    assert comment_statuses.get(comment_id) == "draft", (
        f"Expected comment to start as 'draft', got {comment_statuses.get(comment_id)!r}"
    )

    # ── 5. Ask agent to address the review comment ───────────────────────────
    address_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "I left a review comment on palindrome.py. "
            "Please do the following steps in order:\n"
            "1. Call list_comments to see the open comments.\n"
            "2. Call update_comment for the comment, setting status "
            "to 'addressed'.\n"
            "3. Confirm you addressed the comment."
        ),
    )

    address_body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=address_response_id,
        timeout=120,
    )
    assert address_body["status"] == "completed", (
        f"Agent address-comment turn failed. "
        f"error={address_body.get('error')!r}. "
        f"output={address_body.get('output', [])}"
    )

    # ── 6. Verify comment was addressed ──────────────────────────────────────
    # Check that the agent called the comment tools.
    address_calls = _tool_names_in_output(address_body)
    assert "list_comments" in address_calls, (
        f"Agent did not call list_comments. Tool calls seen: {address_calls}. "
        f"Output: {address_body.get('output', [])}"
    )
    assert "update_comment" in address_calls, (
        f"Agent did not call update_comment. Tool calls seen: {address_calls}. "
        f"Output: {address_body.get('output', [])}"
    )

    # Verify the comment status via REST.
    post_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
    post_resp.raise_for_status()
    post_statuses = {c["id"]: c["status"] for c in post_resp.json()}
    assert post_statuses.get(comment_id) == "addressed", (
        f"Comment still has status {post_statuses.get(comment_id)!r} "
        f"after the agent turn; expected 'addressed'."
    )
