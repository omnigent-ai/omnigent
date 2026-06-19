"""
E2E for steering responsiveness during the end-of-turn async drain (mock LLM).

Reproduces the ``omnigent chat``-visible bug where a user typing
mid-flight (while the parent workflow is blocked on
``_drain_async_completions(block_for_one=True)`` waiting for
async client tools to finish) saw no response until all the
tasks completed. The drain's ``DBOS.recv`` had no signal for
steering messages -- they landed in the conversation via
``try_deliver`` but the workflow wasn't polling.

Fix (committed separately): the blocking drain now polls the
conversation store every ``_STEERING_POLL_INTERVAL_S`` (1 s)
alongside the DBOS recv; if steering is detected, the drain
returns early and the outer loop iterates so the LLM sees the
new message.

Test strategy:
- Register an inline agent with the async_compute client tool.
- Start the parent with a query. The mock LLM calls async_compute.
- Once the handle FCO appears (proves the client_tool was
  dispatched and the parent has entered the drain wait),
  POST a steering message via session events.
- Assert: a NEW assistant message appears within the steering
  latency cap. Without the fix this would take ~1 h.
- The client_tool task is never PATCHed, so it stays in_progress
  throughout.

Usage::

    pytest tests/e2e/test_steering_during_async_drain_e2e.py -v
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

# Bound on how long the steering message may sit unseen by the
# agent after being POSTed. The server's steering poll runs
# every _STEERING_POLL_INTERVAL_S (=1 s) and the LLM needs
# another round-trip to emit the acknowledgement, so 15 s is
# a comfortable ceiling that still demonstrates the fix works.
_STEERING_MAX_LATENCY_S = 15.0


_ASYNC_CLIENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "async_compute",
        "description": (
            "Long-running client-side computation. Always call with "
            "synchronous=false. The result is delivered later as a "
            "system message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "description": "Echo this string back as the result.",
                },
                "synchronous": {
                    "type": "boolean",
                    "description": (
                        "MUST be set to false. Dispatches as a "
                        "background task and returns a handle."
                    ),
                },
            },
            "required": ["value", "synchronous"],
        },
    },
}


def _session_items(http_client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Fetch all session items, flattened."""
    resp = http_client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    raw = resp.json().get("items", [])
    flat: list[dict[str, Any]] = []
    for item in raw:
        data = item.get("data")
        if isinstance(data, dict):
            flat.append({**item, **data})
        else:
            flat.append(item)
    return flat


def _wait_for_handle(
    http_client: httpx.Client,
    session_id: str,
    tool_name: str,
    timeout_s: float = 60.0,
) -> str:
    """
    Poll until the async client-tool handle FCO appears.

    :param http_client: HTTP client.
    :param session_id: Session id to scan.
    :param tool_name: Name on the preceding function_call.
    :param timeout_s: Max seconds to wait for the handle.
    :returns: The handle's ``task_id``.
    :raises AssertionError: On timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        items = _session_items(http_client, session_id)
        last_call_name: str | None = None
        for item in items:
            if item.get("type") == "function_call":
                last_call_name = item.get("name")
            elif item.get("type") == "function_call_output":
                if last_call_name != tool_name:
                    continue
                try:
                    handle = json.loads(item.get("output") or "")
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(handle, dict)
                    and handle.get("kind") == "client_tool"
                    and handle.get("task_id")
                ):
                    return str(handle["task_id"])
        time.sleep(0.25)
    raise AssertionError(
        f"No async client-tool handle appeared in session {session_id} within {timeout_s}s"
    )


def _count_assistant_text_items(items: list[dict[str, Any]]) -> int:
    """
    Count assistant ``output_text`` items with non-empty text.

    :param items: Flattened items list.
    :returns: Number of non-empty assistant text items.
    """
    count = 0
    for item in items:
        if item.get("role") != "assistant":
            continue
        content = item.get("content") or []
        if not isinstance(content, list) or not content:
            continue
        first = content[0]
        if not isinstance(first, dict) or first.get("type") != "output_text":
            continue
        text = first.get("text") or ""
        if text.strip():
            count += 1
    return count


def test_steering_breaks_blocked_async_drain(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    using_mock_llm: bool,
) -> None:
    """
    The agent must react to a steering message within
    ``_STEERING_MAX_LATENCY_S`` seconds, even while the
    end-of-turn async-drain is blocked waiting for a
    client_tool task the test never PATCHes.
    """
    if using_mock_llm:
        pytest.skip(
            "steering-during-async-drain requires the client_tool holder "
            "workflow which is only triggered by the legacy POST /v1/responses "
            "route with request-level tools; that route was removed, and "
            "session-dispatch does not create client_tool tasks from "
            "request-level tool schemas"
        )
    model = f"mock-steer-drain-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"steer-drain-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are a test agent. When asked to compute, call "
            "async_compute with synchronous=false. After dispatching, "
            "wait for the result. If you receive a steering message, "
            "respond to it immediately."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    steer_marker = "STEER_PINEAPPLE_99"

    # Turn 1: call async_compute. Turn 2 (after steering): acknowledge.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_compute",
                        "name": "async_compute",
                        "arguments": json.dumps(
                            {"value": "MID_DRAIN_STEER", "synchronous": False}
                        ),
                    }
                ]
            },
            {"text": f"Acknowledged steering. {steer_marker}"},
        ],
        key=model,
    )

    # Step 1: create session and send the initial message with the
    # client tool registered.
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Compute on the value 'MID_DRAIN_STEER'.",
        tools=[_ASYNC_CLIENT_TOOL],
    )

    # Step 2: wait for the async-client-tool handle to appear.
    _wait_for_handle(http_client, session_id, "async_compute")

    # Count assistant text items BEFORE steering.
    pre_steer_items = _session_items(http_client, session_id)
    pre_steer_assistant_count = _count_assistant_text_items(pre_steer_items)

    # Step 3: POST steering via session events.
    steer_start = time.monotonic()
    send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(f"Stop waiting. Forget the async task. Reply only with the word {steer_marker}."),
    )

    # Step 4: poll for a new non-empty assistant message.
    deadline = steer_start + _STEERING_MAX_LATENCY_S
    observed_new_message = False
    final_items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        final_items = _session_items(http_client, session_id)
        if _count_assistant_text_items(final_items) > pre_steer_assistant_count:
            observed_new_message = True
            break
        time.sleep(0.25)

    elapsed = time.monotonic() - steer_start
    assert observed_new_message, (
        f"Steering not processed within {_STEERING_MAX_LATENCY_S}s "
        f"(waited {elapsed:.1f}s). The drain's steering-poll path "
        f"is broken. Pre-steer assistant count: "
        f"{pre_steer_assistant_count}; "
        f"current: {_count_assistant_text_items(final_items)}."
    )

    # Step 5: the acknowledgement should contain the marker.
    joined_new_texts = "\n".join(
        (item["content"][0].get("text") or "")
        for item in final_items
        if item.get("role") == "assistant"
        and isinstance(item.get("content"), list)
        and item["content"]
        and item["content"][0].get("type") == "output_text"
    )
    assert steer_marker in joined_new_texts, (
        f"The LLM's new reply should acknowledge the steering "
        f"content (look for marker {steer_marker!r}). "
        f"Joined assistant texts: {joined_new_texts[:800]!r}"
    )
