"""E2E test: list_comments and update_comment tools.

Verifies the full round-trip: user adds comments to a session via
the REST API, asks the agent to address them, and the agent calls
``list_comments`` to retrieve them and ``update_comment`` to mark
each one as "addressed". The test then confirms the server reflects
the expected "addressed" status on all comments.

Runs against the mock LLM server — the mock returns tool call
responses for ``list_comments`` and ``update_comment``, and the
runner executes them as real runner-level tools.

Usage::

    pytest tests/e2e/test_comment_tools.py -v
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


def _tool_names_in_output(body: dict[str, Any]) -> list[str]:
    """
    Collect every function_call tool name from a response body.

    :param body: Terminal response body from
        :func:`poll_session_until_terminal`.
    :returns: List of tool names in call order, e.g.
        ``["list_comments", "update_comment", "update_comment"]``.
    """
    return [
        item["name"]
        for item in body.get("output", [])
        if item.get("type") == "function_call" and item.get("name")
    ]


def test_agent_lists_and_addresses_comments(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    Agent uses list_comments + update_comment to address review comments.

    Flow:
    1. Create a runner-bound session.
    2. POST two draft comments on ``app.py`` via the REST API.
    3. Ask the agent (with an explicit tool instruction) to address all
       comments on ``app.py``.
    4. Assert ``list_comments`` and ``update_comment`` appear in the
       agent's tool calls.
    5. Assert both comments now have status ``"addressed"`` via the REST API.

    **What breaks if this fails:**

    - ``list_comments`` / ``update_comment`` not in local dispatch table
      (runner tool_dispatch.py missing _COMMENT_TOOLS).
    - ``comment store not configured`` (cli.py not passing comment_store to
      init_runtime, or _execute_comment_tool not using server_client).
    - Comments still in ``"draft"`` status → update_comment dispatch or REST
      PATCH not working.
    - Agent never called the tools → prompt not explicit enough, or tools
      missing from the schema sent to the harness.

    :param http_client: HTTP client pointed at the live server.
    :param archer_agent: Registered archer agent name.
    :param live_runner_id: Runner id the session is bound to.
    """
    # ── 1. Create a runner-bound session ──────────────────────────────────────
    model = f"mock-comment-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"comment-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a code review assistant.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    # ── 2. Add two draft comments to the session via REST ────────────────────
    # Comments are intentionally simple so the LLM response is fast and the
    # test focuses on tool mechanics rather than code-fix correctness.
    r1 = http_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "app.py",
            "body": "Typo: 'recieve' should be 'receive'.",
            "start_index": 0,
            "end_index": 20,
            "anchor_content": "def recieve_data():",
        },
    )
    r1.raise_for_status()
    comment1_id: str = r1.json()["id"]

    r2 = http_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "app.py",
            "body": "Variable name 'x' is not descriptive; rename to 'count'.",
            "start_index": 100,
            "end_index": 110,
            "anchor_content": "x = 0",
        },
    )
    r2.raise_for_status()
    comment2_id: str = r2.json()["id"]

    # Confirm both comments are in draft state before asking the agent.
    pre_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
    pre_resp.raise_for_status()
    pre_statuses = {c["id"]: c["status"] for c in pre_resp.json()}
    assert pre_statuses.get(comment1_id) == "draft", (
        f"Expected comment 1 to start as 'draft', got {pre_statuses.get(comment1_id)!r}"
    )
    assert pre_statuses.get(comment2_id) == "draft", (
        f"Expected comment 2 to start as 'draft', got {pre_statuses.get(comment2_id)!r}"
    )

    # ── 3. Configure mock LLM responses ──────────────────────────────────────
    # The mock LLM returns: list_comments → update_comment(c1) →
    # update_comment(c2) → final text. The runner executes the real
    # comment tools (runner-level, always registered).
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_list",
                        "name": "list_comments",
                        "arguments": json.dumps({"path": "app.py"}),
                    },
                ],
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_upd1",
                        "name": "update_comment",
                        "arguments": json.dumps(
                            {"comment_id": comment1_id, "status": "addressed"}
                        ),
                    },
                    {
                        "call_id": "call_upd2",
                        "name": "update_comment",
                        "arguments": json.dumps(
                            {"comment_id": comment2_id, "status": "addressed"}
                        ),
                    },
                ],
            },
            {"text": "Both comments addressed."},
        ],
        key=model,
    )

    # ── 4. Ask the agent to address the comments ─────────────────────────────
    # The prompt names the tools explicitly so the LLM reliably uses them
    # rather than trying to "answer" without tool calls.
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "I left two review comments on app.py. "
            "Please do the following steps in order:\n"
            "1. Call list_comments to see the open comments on app.py.\n"
            "2. Call update_comment for each comment, setting status to 'addressed'.\n"
            "3. Confirm you addressed all comments."
        ),
    )

    # ── 5. Wait for the agent turn to complete ───────────────────────────────
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", (
        f"Agent turn failed. error={body.get('error')!r}. output={body.get('output', [])}"
    )

    # ── 6. Verify tool calls in the agent output ─────────────────────────────
    calls = _tool_names_in_output(body)

    # list_comments must have been called at least once — that's how the
    # agent sees the comments. If missing, the tool isn't in the harness
    # schema or the runner dispatch is broken.
    assert "list_comments" in calls, (
        f"Agent did not call list_comments. Tool calls seen: {calls}. "
        f"Output: {body.get('output', [])}"
    )

    # update_comment must appear at least twice — once per comment.
    # A single call means the agent only addressed one comment; zero
    # calls means it never tried to mark them done.
    update_call_count = calls.count("update_comment")
    assert update_call_count >= 2, (
        f"Expected at least 2 update_comment calls (one per comment), "
        f"got {update_call_count}. Tool calls seen: {calls}"
    )

    # ── 7. Verify comment statuses via REST ───────────────────────────────────
    post_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
    post_resp.raise_for_status()
    post_statuses = {c["id"]: c["status"] for c in post_resp.json()}

    # Both comments must be "addressed" — if either is still "draft",
    # update_comment dispatched but the PATCH to the server didn't work,
    # or the wrong comment_id was passed.
    assert post_statuses.get(comment1_id) == "addressed", (
        f"Comment 1 still has status {post_statuses.get(comment1_id)!r} "
        f"after the agent turn; expected 'addressed'."
    )
    assert post_statuses.get(comment2_id) == "addressed", (
        f"Comment 2 still has status {post_statuses.get(comment2_id)!r} "
        f"after the agent turn; expected 'addressed'."
    )
