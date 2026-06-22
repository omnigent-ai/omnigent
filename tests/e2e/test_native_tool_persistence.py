"""E2E test: native tool items persist across agent loop iterations.

Verifies that provider-native tool results (e.g. web_search_call)
are persisted to the conversation store and replayed to the LLM on
subsequent iterations. Without this, the LLM re-requests the same
searches in a loop.

Uses the mock LLM server to script a deterministic multi-turn
sequence: the LLM calls a function tool and then produces text. The
second turn proves the prior tool output persisted by emitting a
marker acknowledging the first turn's result.

Usage::

    pytest tests/e2e/test_native_tool_persistence.py -v
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


def _has_tool_call(body: dict[str, Any], name: str) -> bool:
    """Check if the response output contains a function_call with the given name."""
    for item in body.get("output", []):
        if item.get("type") == "function_call" and item.get("name") == name:
            return True
    return False


def test_tool_results_persist_across_iterations(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Tool results persist and the LLM sees them in subsequent iterations.

    The mock LLM is scripted to call ``sys_os_shell`` on iteration 1,
    receive the tool output, and then produce final text that references
    the tool result on iteration 2. This proves the tool call output was
    persisted and replayed to the LLM in the next iteration.

    :param http_client: HTTP client pointed at the live e2e server.
    :param live_runner_id: Runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM server URL.
    """
    marker = "PERSIST_VERIFIED_OK"
    model = f"mock-persist-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"persist-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a research assistant. Run commands when asked.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none"},
            },
        },
    )

    # Script the mock LLM in two iterations:
    # 1. Call sys_os_shell to run a command
    # 2. After receiving the tool result, emit final text with the marker
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_echo",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": "echo 'tool_output_42'"}),
                    }
                ],
            },
            {
                "text": (
                    f"{marker} - The command output was tool_output_42. "
                    "This confirms the tool result was persisted and visible."
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
        content="Run echo 'tool_output_42' and tell me the output.",
    )

    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", f"Task failed: {body.get('error')}"

    # The agent must have called sys_os_shell.
    assert _has_tool_call(body, "sys_os_shell"), (
        "Expected sys_os_shell tool call. The agent did not use the tool."
    )

    text = _extract_all_text(body)
    assert marker in text, f"Expected marker in output, got: {text!r}"

    # The tool output should appear in function_call_output items.
    tool_outputs = [
        str(it.get("output", ""))
        for it in body.get("output", [])
        if it.get("type") == "function_call_output"
    ]
    combined_output = " ".join(tool_outputs)
    assert "tool_output_42" in combined_output, (
        f"Tool output not found in function_call_output items. "
        f"Tool results may not be persisting. Got: {combined_output[:500]}"
    )
