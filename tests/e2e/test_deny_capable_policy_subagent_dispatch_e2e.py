"""Verify deny-capable tool policies do not break sub-agent dispatch."""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S, final_assistant_text, get_output_items

pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    pytest.mark.timeout(420, method="signal"),
]


@pytest.mark.parametrize("terminal_verdict", ["DENY", "ALLOW"])
def test_policy_allows_subagent_dispatch(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    terminal_verdict: str,
) -> None:
    uid = uuid.uuid4().hex[:6]
    parent_model, child_model = f"mock-parent-{uid}", f"mock-child-{uid}"
    child_marker = f"PING_{uid}"
    mock_base = f"{mock_llm_server_url}/v1"
    expression = (
        'event.type != "tool_call" ? {"result": "ALLOW"} : '
        "has(event.data.name) && type(event.data.name) == string && "
        'event.data.name.matches("^(ToolSearch|sys_session_send|sys_read_inbox)$") '
        f'? {{"result": "ALLOW"}} : {{"result": "{terminal_verdict}"}}'
    )
    parent_name = register_inline_agent(
        http_client,
        name=f"policy-parent-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Dispatch the child and report its reply.",
        mock_llm_base_url=mock_base,
        extra_config={
            "async": True,
            "tools": {
                "child": {
                    "type": "agent",
                    "description": "Dispatch regression child.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": "Answer briefly and literally.",
                }
            },
            "policies": {
                "allowlist": {
                    "type": "function",
                    "on": ["tool_call"],
                    "function": {
                        "path": "omnigent.policies.builtins.cel.cel_policy",
                        "arguments": {"expression": expression},
                    },
                }
            },
            "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
        },
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_dispatch_child",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {"agent": "child", "title": "ping", "args": "Reply with PING."}
                        ),
                    }
                ]
            },
            {"text": "Dispatched child, waiting for result."},
            {"text": "Child result received."},
        ],
        key=parent_model,
    )
    configure_mock_llm(mock_llm_server_url, [{"text": child_marker}], key=child_model)

    session_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client, session_id=session_id, content="Dispatch the child."
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )
    assert body["status"] == "completed", body
    outputs = {
        item["call_id"]: item["output"] for item in get_output_items(body, "function_call_output")
    }
    handle = json.loads(outputs["call_dispatch_child"])
    assert handle["status"] == "launching", handle
    assert handle["kind"] == "sub_agent", handle
    assert isinstance(handle["task_id"], str) and handle["task_id"], handle

    # The launching handle precedes the child's first turn completing.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = http_client.get(f"/v1/sessions/{handle['task_id']}/items")
        response.raise_for_status()
        if child_marker in final_assistant_text({"output": response.json()["data"]}):
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        pytest.fail(f"Child never replied: {response.text}")
