"""E2E test: default executor auto-collects sub-agent results (mock LLM).

Verifies that sub-agent auto-collection works for the default (LLM)
executor path — the same task store query that was added for the
Claude SDK executor. Uses inline agents (parent + summarizer) with
mock LLM responses so no real API key is needed.

Usage::

    pytest tests/e2e/test_default_executor_auto_collect.py -v
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
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


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_agent_spawns_and_auto_collects(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Single message triggers spawn + auto-collect for the default
    executor. The parent agent spawns a sub-agent and the workflow
    auto-collects the results before completing.

    This verifies the unified spawn tracking path (task store query)
    works for the default executor, not just the Claude SDK executor.

    **What breaks if the feature is wrong:**

    - If the task store query for child tasks doesn't work, spawned
      sub-agents are never discovered -> auto-collect skips -> the
      parent completes without sub-agent results.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-autocollect-parent-{uid}"
    child_model = f"mock-autocollect-child-{uid}"
    marker = "PHOTOSYNTHESIS_MOCK_SUMMARY"

    reset_mock_llm(mock_llm_server_url)

    # Register parent agent with a summarizer sub-agent.
    parent_name = register_inline_agent(
        http_client,
        name=f"autocollect-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are a research assistant. You have a summarizer sub-agent. "
            "Call sys_session_send(agent='summarizer', title='photosynthesis', "
            "args='Summarize photosynthesis in 2 sentences') to spawn it. "
            "After its result arrives, quote it in your reply."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "tools": {
                "summarizer": {
                    "type": "agent",
                    "description": "Summarizes topics.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": f"{mock_llm_server_url}/v1",
                        },
                    },
                    "prompt": "You are a summarizer. Summarize the given topic.",
                },
            },
        },
    )

    # Configure mock responses:
    # 1. Parent dispatches sys_session_send
    # 2. Parent after tool result (acknowledges dispatch)
    # 3. Child responds with marker
    # 4. Parent auto-wake quotes the marker
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
                                "agent": "summarizer",
                                "title": "photosynthesis",
                                "args": "Summarize photosynthesis in 2 sentences",
                            }
                        ),
                    },
                ],
            },
            {"text": "Dispatched summarizer, waiting for result."},
            # Auto-wake continuation: parent quotes the child marker
            {"text": f"The summarizer returned: {marker}"},
        ],
        key=parent_model,
    )

    # Child mock: returns a photosynthesis summary with the marker
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "text": (
                    f"{marker} Photosynthesis converts sunlight, water, and "
                    "carbon dioxide into glucose and oxygen. This process is "
                    "fundamental to life on Earth as it produces both food "
                    "and the oxygen we breathe."
                ),
            },
        ],
        key=child_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Use sys_session_send to spawn the summarizer. "
            "Ask it to summarize the concept of photosynthesis "
            "in exactly 2 sentences."
        ),
    )

    # Wait for the marker to appear (auto-wake delivers it)
    blob = _wait_for_markers(http_client, session_id, marker)
    assert marker in blob, f"Expected marker {marker!r} in session items"
