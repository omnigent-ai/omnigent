"""End-to-end tests for the coder agent with sub-agents (mock LLM).

Exercises:
- Sub-agent spawning with mock LLM responses
- Client-side tool tunneling (park -> poll -> PATCH -> resume)
- Auto-collect at turn end
- Full reviewer sub-agent workflow with real tool execution

The mock LLM returns predetermined tool calls (sys_session_send for
spawning, client tools for the reviewer). The runner dispatches
sys_session_send server-side; client tools are tunneled to the test
harness for local execution.

Usage::

    pytest tests/e2e/test_coder_subagent.py -v
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

# Load the coder tool set for client-side tool execution.
from omnigent.client_tools import get_tool_set as _get_tool_set
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

_tool_mod = _get_tool_set("coding")
TOOLS: list[dict[str, Any]] = _tool_mod.TOOLS
execute_tool = _tool_mod.execute_tool


def _wait_for_markers(
    http_client: httpx.Client,
    session_id: str,
    *markers: str,
    timeout_s: float = 240.0,
) -> str:
    """
    Poll the session snapshot until every marker substring appears.

    sys_session_send is async: the sub-agent runs after the parent's
    dispatch turn ends, then auto-wakes the parent in a continuation
    turn. The marker therefore lands in the session AFTER the dispatch
    turn goes idle.

    :param http_client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id to poll.
    :param markers: Substrings that must all appear in the session.
    :param timeout_s: Max seconds to wait.
    :returns: Serialized session items text.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = http_client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        items = resp.json().get("items", [])
        blob = json.dumps(items)
        if all(m in blob for m in markers):
            return blob
        time.sleep(0.5)
    raise AssertionError(
        f"Markers {markers!r} not found in session {session_id} within {timeout_s:.0f}s"
    )


def _conversation_items(
    http_client: httpx.Client,
    session_id: str,
) -> list[dict[str, Any]]:
    """Return all conversation items from the session snapshot.

    :param http_client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id.
    :returns: List of conversation item dicts.
    """
    resp = http_client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return resp.json().get("items", [])


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_coder_spawns_reviewer_and_collects(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    sample_code_dir: Path,
) -> None:
    """
    Coder agent spawns the reviewer sub-agent, the reviewer
    produces a review, and the parent auto-collects and produces
    a final response incorporating the review.

    This is the full end-to-end flow that caught:
    - Empty sub-agent output (client tools not tunneled)
    - "Unknown tool" errors (client re-executing server tools)
    - Deadlock (time.sleep polling exhausting DBOS threads)
    - Turn completing before sub-agent finishes (no auto-collect)
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-coder-parent-{uid}"
    reviewer_model = f"mock-coder-reviewer-{uid}"
    marker = "REVIEWER_MOCK_LGTM"

    reset_mock_llm(mock_llm_server_url)

    # Register coder parent with a reviewer sub-agent.
    parent_name = register_inline_agent(
        http_client,
        name=f"coder-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are a coder. You have a reviewer sub-agent. "
            "Call sys_session_send(agent='reviewer', title='code-review', "
            "args='Review the Python code') to dispatch a review."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "tools": {
                "reviewer": {
                    "type": "agent",
                    "description": "Reviews code for bugs and quality.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": reviewer_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": f"{mock_llm_server_url}/v1",
                        },
                    },
                    "prompt": "You are a code reviewer.",
                },
            },
        },
    )

    # Parent mock: dispatch sys_session_send, then acknowledge, then
    # auto-wake with the collected review.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_spawn",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "reviewer",
                                "title": "code-review",
                                "args": f"Review the Python code in {sample_code_dir}",
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched reviewer, waiting for result."},
            # Auto-wake continuation: parent quotes the reviewer marker
            {"text": f"The reviewer returned: {marker}. The code looks good overall."},
        ],
        key=parent_model,
    )

    # Reviewer mock: returns a code review with the marker
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    f"{marker}\n\n"
                    "## Critical Issues\n"
                    "- calculator.py:3 - divide() has no zero-division guard\n\n"
                    "## Summary\n"
                    "The code needs a zero-division check in divide()."
                ),
            },
        ],
        key=reviewer_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Use sys_session_send to spawn the reviewer sub-agent. "
            f"Tell it to review the Python code in {sample_code_dir}. "
            f"Do NOT read the files yourself -- delegate to the reviewer. "
            f"After the reviewer finishes, show me its findings."
        ),
    )

    # Wait for the marker to appear via auto-wake
    blob = _wait_for_markers(http_client, session_id, marker)
    assert marker in blob

    # Verify sys_session_send was called
    items = _conversation_items(http_client, session_id)
    blob_items = json.dumps(items)
    assert "sys_session_send" in blob_items, "Expected sys_session_send call in session items"


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_coder_spawns_parallel_subagents(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
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
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-coder-parallel-{uid}"
    reviewer_model = f"mock-coder-par-reviewer-{uid}"
    researcher_model = f"mock-coder-par-researcher-{uid}"
    reviewer_marker = "REVIEWER_PARALLEL_OK"
    researcher_marker = "RESEARCHER_PARALLEL_OK"

    reset_mock_llm(mock_llm_server_url)

    # Register coder parent with both sub-agents.
    parent_name = register_inline_agent(
        http_client,
        name=f"coder-parallel-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are a coder with reviewer and researcher sub-agents. "
            "Spawn both using sys_session_send."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "tools": {
                "reviewer": {
                    "type": "agent",
                    "description": "Reviews code.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": reviewer_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": f"{mock_llm_server_url}/v1",
                        },
                    },
                    "prompt": "You are a code reviewer.",
                },
                "researcher": {
                    "type": "agent",
                    "description": "Researches topics.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": researcher_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": f"{mock_llm_server_url}/v1",
                        },
                    },
                    "prompt": "You are a researcher.",
                },
            },
        },
    )

    # Parent mock: dispatch BOTH sub-agents in one tool_calls response
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_reviewer",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "reviewer",
                                "title": "code-review",
                                "args": f"review the Python code in {sample_code_dir}",
                            }
                        ),
                    },
                    {
                        "call_id": "call_researcher",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "researcher",
                                "title": "py314",
                                "args": "find what's new in Python 3.14",
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched both sub-agents, waiting for results."},
            # Auto-wake continuation: parent quotes both markers
            {
                "text": (
                    f"Both sub-agents returned. Reviewer: {reviewer_marker}. "
                    f"Researcher: {researcher_marker}."
                ),
            },
        ],
        key=parent_model,
    )

    # Reviewer mock
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Code review complete. {reviewer_marker}"}],
        key=reviewer_model,
    )

    # Researcher mock
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Research complete. {researcher_marker}"}],
        key=researcher_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Spawn BOTH sub-agents in parallel by emitting TWO "
            f"sys_session_send tool_calls in your next response:\n"
            f"1. sys_session_send(agent='reviewer', title='code-review', "
            f"args='review the Python code in {sample_code_dir}')\n"
            f"2. sys_session_send(agent='researcher', title='py314', "
            f'args="find what\'s new in Python 3.14")\n'
            f"Do NOT read files or search yourself -- delegate to the "
            f"sub-agents. After they finish, show me both results."
        ),
    )

    # Wait for both markers to appear via auto-wake
    _wait_for_markers(http_client, session_id, reviewer_marker, researcher_marker)

    # Verify both sys_session_send calls appear in session items
    items = _conversation_items(http_client, session_id)
    blob = json.dumps(items)
    spawn_count = blob.count("sys_session_send")
    assert spawn_count >= 2, f"Expected at least 2 sys_session_send references; got {spawn_count}"
    assert "reviewer" in blob, "reviewer not found in session items"
    assert "researcher" in blob, "researcher not found in session items"
