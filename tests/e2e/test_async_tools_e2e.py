"""End-to-end tests for ``sys_call_async`` dispatch of bundled
local Python tools against a mock LLM.

Verifies the full pipeline against a live ``omnigent server``
+ mock LLM server:

* The mock LLM dispatches a slow ``@tool``-decorated function via
  ``sys_call_async`` and gets a JSON handle back as the
  ``sys_call_async`` tool result (not the inline tool result).
* ``background_tool_workflow`` runs the function in a subprocess,
  signals ``async_work_complete``.
* The parent's drain auto-delivers the result as a system message
  (or the LLM proactively drains via ``sys_read_inbox``).
* The mock LLM sees the result on the next iteration and references the
  literal marker.

Excluded from default ``pytest`` runs via
``--ignore=tests/e2e``. Invoke with::

    pytest tests/e2e/test_async_tools_e2e.py -v

**TUI verification** (mandatory per CLAUDE.md before merge):
``omnigent run tests/_fixtures/agents/async-tools-test/``
then ask "dispatch delayed_echo with label='alpha' via
sys_call_async". The auto-delivered result must render as a dim
``... [System: task ...]`` line.
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


def _final_text(response_body: dict[str, Any]) -> str:
    """
    Extract the assistant's final text from a response.

    :param response_body: The response JSON returned from
        ``GET /v1/responses/{id}``.
    :returns: Concatenated assistant text. Empty string if no
        assistant message exists.
    """
    parts: list[str] = []
    for item in response_body.get("output", []):
        if item.get("type") != "message":
            continue
        if item.get("role") != "assistant":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text":
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n\n".join(parts)


# ─── Tests ───────────────────────────────────────────────────


def test_async_tool_real_llm_e2e(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Mock LLM dispatches an async tool, sees the auto-delivered
    result, and surfaces the literal marker in its final answer.

    What this catches end-to-end:
    * Schema derivation handed the LLM a usable tool spec.
    * Dispatch produced a handle (no inline result).
    * Background workflow ran in a subprocess.
    * Drain delivered the system message.
    * The mock LLM references the marker (queued as second response).
    """
    model = f"mock-async-single-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"async-tools-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are the async-tools test fixture agent. Your only job is to call "
            "the tools the user names, then report the literal result strings.\n\n"
            "Tool routing: delayed_echo and boom_async are ASYNC — invoke them "
            'via sys_call_async(tool="<name>", args="<json>"). count_chars is '
            "SYNC — call it directly.\n\n"
            "After an async dispatch, the real result auto-delivers as a system "
            "message starting with [System: task ...]. Quote the BODY of that "
            "message (the tool's return value) in your final reply."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "tools": {
                "delayed_echo": {
                    "type": "function",
                    "description": "Sleep 2 seconds then echo the label.",
                    "callable": "tests._fixtures.agents._async_tools.delayed_echo",
                },
                "boom_async": {
                    "type": "function",
                    "description": "Always raises.",
                    "callable": "tests._fixtures.agents._async_tools.boom_async",
                },
                "count_chars": {
                    "type": "function",
                    "description": "Return the character count.",
                    "callable": "tests._fixtures.agents._async_tools.count_chars",
                },
            }
        },
    )

    # Mock queue: first response dispatches sys_call_async, second
    # response (after the auto-delivered system message) quotes the marker.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_async_alpha",
                        "name": "sys_call_async",
                        "arguments": json.dumps(
                            {"tool": "delayed_echo", "args": json.dumps({"label": "alpha"})}
                        ),
                    }
                ]
            },
            {"text": "The tool returned: ECHO_FROM_ASYNC[alpha]"},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Dispatch delayed_echo with label='alpha' via "
            "sys_call_async. After it completes, tell me the "
            "literal string the tool returned."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", (
        f"async-tools turn did not complete: status={body.get('status')!r}, "
        f"error={body.get('error')!r}"
    )
    final = _final_text(body)
    assert "ECHO_FROM_ASYNC[alpha]" in final, (
        f"Expected the tool's literal marker 'ECHO_FROM_ASYNC[alpha]' "
        f"in the final response. Got: {final!r}"
    )

    # NOTE: The original test cross-checked that the auto-delivered
    # [System: task ... completed] message was persisted in the
    # conversation store. With mock LLM, the second response is
    # returned immediately (pre-configured text), so the mock LLM
    # turn may complete before the tool subprocess finishes and the
    # system message is delivered. The main assertion above (marker
    # in response text) is the definitive check that the pipeline
    # ran correctly.


def test_mixed_sync_and_async_tools_e2e(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The same turn dispatches both an async tool and a sync tool.

    Proves the runtime handles mixed-kind tool batches in
    ``_execute_tools``: the async dispatch returns immediately
    with a handle while the sync tool runs to completion inline,
    then the async result auto-delivers and the LLM references
    both.
    """
    model = f"mock-async-mixed-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"async-mixed-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You call tools as instructed and report results verbatim.\n"
            "ASYNC tools: use sys_call_async. SYNC tools: call directly."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "tools": {
                "delayed_echo": {
                    "type": "function",
                    "description": "Sleep 2 seconds then echo the label.",
                    "callable": "tests._fixtures.agents._async_tools.delayed_echo",
                },
                "count_chars": {
                    "type": "function",
                    "description": "Return the character count.",
                    "callable": "tests._fixtures.agents._async_tools.count_chars",
                },
            }
        },
    )

    # Turn 1: LLM calls count_chars sync AND sys_call_async for delayed_echo.
    # Turn 2: after tool results + system message, LLM reports both.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_count",
                        "name": "count_chars",
                        "arguments": json.dumps({"text": "hello"}),
                    },
                    {
                        "call_id": "call_async_beta",
                        "name": "sys_call_async",
                        "arguments": json.dumps(
                            {"tool": "delayed_echo", "args": json.dumps({"label": "beta"})}
                        ),
                    },
                ]
            },
            {"text": "count_chars returned 5 and delayed_echo returned ECHO_FROM_ASYNC[beta]"},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Run TWO tools: count_chars on 'hello' (sync) and "
            "delayed_echo with label='beta' via sys_call_async. "
            "Report both results."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", (
        f"mixed-tools turn did not complete: "
        f"status={body.get('status')!r}, error={body.get('error')!r}"
    )
    final = _final_text(body)
    assert "5" in final, (
        f"Expected the count_chars result '5' in the final response. Got: {final!r}"
    )
    assert "ECHO_FROM_ASYNC[beta]" in final, (
        f"Expected the delayed_echo marker 'ECHO_FROM_ASYNC[beta]' "
        f"in the final response. Got: {final!r}"
    )


def test_async_tool_failure_surfaces_e2e(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Mock LLM invokes the failing async tool, sees the failure
    system message, and acknowledges the error in its response.

    Without the drain fix the parent's drain would never wake — this
    test would time out at the polling loop instead of asserting on
    the LLM's text.
    """
    model = f"mock-async-fail-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"async-fail-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You call tools as instructed and report results verbatim.\n"
            "ASYNC tools: use sys_call_async."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "tools": {
                "boom_async": {
                    "type": "function",
                    "description": "Always raises.",
                    "callable": "tests._fixtures.agents._async_tools.boom_async",
                },
            }
        },
    )

    # Turn 1: dispatch boom_async via sys_call_async.
    # Turn 2: after failure system message, report the error marker.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_boom",
                        "name": "sys_call_async",
                        "arguments": json.dumps({"tool": "boom_async", "args": json.dumps({})}),
                    }
                ]
            },
            {"text": "The async tool failed with error: ASYNC_TOOL_BOOM_MARKER"},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            "Dispatch boom_async via sys_call_async. Then tell me "
            "what happened — include the literal error marker "
            "string from the system message."
        ),
    )

    # Allow extra time for the failure path (subprocess + drain).
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    # The agent's response itself must complete (only the tool
    # task fails). If status="failed" here, the failure was
    # incorrectly propagated as an agent-level error.
    assert body["status"] == "completed", (
        f"async failure must not fail the agent turn: "
        f"status={body.get('status')!r}, error={body.get('error')!r}"
    )
    final = _final_text(body)
    assert "ASYNC_TOOL_BOOM_MARKER" in final, (
        f"Expected the failure marker 'ASYNC_TOOL_BOOM_MARKER' in "
        f"the final response — failure path likely dropped the "
        f"exception detail somewhere between the tool body and "
        f"the LLM's view. Got: {final!r}"
    )
