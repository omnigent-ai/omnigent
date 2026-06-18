"""End-to-end tests for the coder agent with sub-agents.

Requires ``--llm-api-key`` and a real server. Run with::

    pytest tests/e2e/test_coder_subagent.py \\
        --llm-api-key $LLM_API_KEY -v

Tests exercise:
- Sub-agent spawning with real LLM
- Client-side tool tunneling (park → poll → PATCH → resume)
- Auto-collect at turn end
- Full reviewer sub-agent workflow with real tool execution
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

# Load the coder tool set for client-side tool execution.
from omnigent.client_tools import get_tool_set as _get_tool_set
from tests.e2e.conftest import (
    create_runner_bound_session,
    poll_for_pending_tool_calls,
    poll_session_until_terminal,
    send_user_message_to_session,
)

_tool_mod = _get_tool_set("coding")
TOOLS: list[dict[str, Any]] = _tool_mod.TOOLS
execute_tool = _tool_mod.execute_tool


def _handle_tunneled_calls(
    client: httpx.Client,
    session_id: str,
    pending: list[dict[str, Any]],
) -> None:
    """
    Execute tunneled tool calls and post results back to the session.

    Each result is a ``function_call_output`` event on the session's
    events endpoint (the sessions-API equivalent of the removed
    ``PATCH /v1/responses/{id}`` tool-result path).

    :param client: HTTP client.
    :param session_id: Runner-bound session id the tools tunneled from.
    :param pending: List of action_required function_call items.
    """
    for fc in pending:
        name = fc["name"]
        call_id = fc["call_id"]
        arguments = json.loads(fc.get("arguments", "{}"))
        result = execute_tool(name, arguments)
        resp = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "function_call_output",
                "data": {"call_id": call_id, "output": result},
            },
        )
        assert resp.status_code in (200, 202), (
            f"function_call_output POST failed: {resp.status_code} {resp.text[:300]}"
        )


def _run_with_tunneling(
    client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
    user_input: str,
) -> dict[str, Any]:
    """
    Run a turn that may tunnel client-side tool calls.

    Opens a runner-bound session, posts the user message along with
    the coder ``TOOLS`` schemas, polls for tunneled tool calls,
    executes them locally, PATCHes the results back, and repeats
    until the root task reaches a terminal state.

    :param client: HTTP client.
    :param agent_name: Display name of the uploaded agent.
    :param runner_id: Registered runner id.
    :param user_input: User message text.
    :returns: The terminal response body for the root task.
    """
    session_id = create_runner_bound_session(client, agent_name=agent_name, runner_id=runner_id)
    response_id = send_user_message_to_session(
        client,
        session_id=session_id,
        content=user_input,
        tools=TOOLS,
    )

    while True:
        # Check for pending tunneled tool calls.
        pending = poll_for_pending_tool_calls(
            client, session_id=session_id, response_id=response_id, timeout=120
        )
        if pending:
            _handle_tunneled_calls(client, session_id, pending)
            continue
        # No pending calls — check if the session turn is terminal.
        result = poll_session_until_terminal(
            client,
            session_id=session_id,
            response_id=response_id,
            timeout=120,
        )
        if result["status"] in ("completed", "failed"):
            return result
        # Still in progress but no pending calls yet — keep polling.


def test_coder_spawns_reviewer_and_collects(
    http_client: httpx.Client,
    coder_agent: str,
    live_runner_id: str,
    sample_code_dir: Path,
) -> None:
    """
    Coder agent spawns the reviewer sub-agent, the reviewer
    uses client-side tools (Read, Glob, etc.) to inspect files,
    and the parent auto-collects and produces a final response
    incorporating the review.

    This is the full end-to-end flow that caught:
    - Empty sub-agent output (client tools not tunneled)
    - "Unknown tool" errors (client re-executing server tools)
    - Deadlock (time.sleep polling exhausting DBOS threads)
    - Turn completing before sub-agent finishes (no auto-collect)
    """
    result = _run_with_tunneling(
        http_client,
        agent_name=coder_agent,
        runner_id=live_runner_id,
        user_input=(
            f"Use sys_session_send to spawn the reviewer sub-agent. "
            f"Tell it to review the Python code in {sample_code_dir}. "
            f"Do NOT read the files yourself — delegate to the reviewer. "
            f"After the reviewer finishes, show me its findings."
        ),
    )

    assert result["status"] == "completed", (
        f"Expected completed, got {result['status']}. Error: {result.get('error')}"
    )

    output = result["output"]

    # The response must contain sys_session_send tool call,
    # proving the LLM actually spawned instead of acting
    # directly.
    spawn_calls = [
        item
        for item in output
        if item.get("type") == "function_call" and item.get("name") == "sys_session_send"
    ]
    assert len(spawn_calls) >= 1, (
        "LLM didn't call sys_session_send — it may have used "
        "client tools directly instead of delegating. Output: "
        + str([i.get("name") for i in output if i.get("type") == "function_call"])
    )

    # The parent merged the collected review into a non-empty assistant
    # reply. We assert non-empty text rather than a magic length: the
    # invariant under test is "spawn + collect + merge produced a reply",
    # already backed by the spawn_calls assertion above. The exact length
    # of an LLM review is not a behavioral invariant.
    text_items = [item for item in output if item.get("type") == "message"]
    assert len(text_items) >= 1, f"Expected at least one message, got: {output}"
    all_text = " ".join(
        c.get("text", "") for item in text_items for c in item.get("content", [])
    ).strip()
    assert all_text, (
        f"Expected a non-empty merged review reply, got empty assistant text: {output}"
    )


def test_coder_spawns_parallel_subagents(
    http_client: httpx.Client,
    coder_agent: str,
    live_runner_id: str,
    sample_code_dir: Path,
) -> None:
    """
    Coder agent spawns BOTH reviewer and researcher sub-agents.

    Scope: this test asserts durable delegation behavior, not the
    nondeterministic LLM scheduling detail of whether both
    ``sys_session_send`` calls are emitted in one response or across
    sequential turns. Omnigent dispatches each ``sys_session_send``
    asynchronously; the meaningful invariant is that the completed root
    turn delegated to both requested sub-agents instead of doing the work
    directly or dropping one branch.
    """
    result = _run_with_tunneling(
        http_client,
        agent_name=coder_agent,
        runner_id=live_runner_id,
        user_input=(
            f"Spawn BOTH sub-agents in parallel by emitting TWO "
            f"sys_session_send tool_calls in your next response — "
            f"AP will dispatch them concurrently:\n"
            f"1. sys_session_send(agent='reviewer', title='code-review', "
            f"args='review the Python code in {sample_code_dir}')\n"
            f"2. sys_session_send(agent='researcher', title='py314', "
            f'args="find what\'s new in Python 3.14")\n'
            f"Do NOT read files or search yourself — delegate to the "
            f"sub-agents. After they finish, show me both results."
        ),
    )

    assert result["status"] == "completed", (
        f"Expected completed, got {result['status']}. Error: {result.get('error')}"
    )

    output = result["output"]

    spawn_calls = [
        item
        for item in output
        if item.get("type") == "function_call" and item.get("name") == "sys_session_send"
    ]
    assert len(spawn_calls) >= 2, (
        f"Expected at least 2 sys_session_send tool_calls (one per sub-agent); "
        f"got {len(spawn_calls)}. Output: {output}"
    )

    all_spawn_args = " ".join(c.get("arguments", "") for c in spawn_calls)
    assert "reviewer" in all_spawn_args, (
        f"sys_session_send calls didn't include reviewer: {all_spawn_args}"
    )
    assert "researcher" in all_spawn_args, (
        f"sys_session_send calls didn't include researcher: {all_spawn_args}"
    )
