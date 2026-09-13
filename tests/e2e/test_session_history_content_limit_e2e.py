"""End-to-end coverage for configurable child-session history reads.

Uses the mock LLM with a live server and runner. Invoke with::

    pytest tests/e2e/test_session_history_content_limit_e2e.py -v --timeout=600
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
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S, get_output_items

pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.min_server_version("0.3.0"),
    pytest.mark.min_runner_version("0.9.0"),
]


def _tool_call(
    name: str,
    arguments: dict[str, object],
    call_id: str,
) -> dict[str, str]:
    """Build a mock LLM tool-call entry."""
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _wait_for_session_text(
    http_client: httpx.Client,
    session_id: str,
    text: str,
    *,
    timeout: float = 120,
) -> list[dict[str, Any]]:
    """Poll persisted items until *text* appears."""
    deadline = time.monotonic() + timeout
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        response = http_client.get(
            f"/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "asc"},
        )
        response.raise_for_status()
        items = response.json()["data"]
        if text in json.dumps(items):
            return items
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"{text!r} did not appear in session {session_id} within {timeout}s; last items={items!r}"
    )


def test_session_history_raised_content_limit_recovers_full_child_response_e2e(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A raised history limit recovers a child response truncated by default.

    This test exercises the runner REST dispatch path.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-history-parent-{uid}"
    child_model = f"mock-history-child-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"
    long_child_text = "CHILD_BEGIN|" + ("0123456789" * 408)[:4074] + "|CHILD_END"
    assert len(long_child_text) == 4096

    parent_name = register_inline_agent(
        http_client,
        name=f"history-content-limit-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Follow the scripted mock tool calls exactly.",
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "writer": {
                    "type": "agent",
                    "description": "Deterministic long-response writer.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base,
                        },
                    },
                    "prompt": "Return the scripted response.",
                }
            }
        },
    )

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _tool_call(
                        "sys_session_send",
                        {"agent": "writer", "title": "long", "args": "Emit long text"},
                        "call_spawn",
                    )
                ]
            },
            {"text": "CHILD_DISPATCHED"},
            {"text": "AUTO_WAKE_COMPLETE"},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": long_child_text}],
        key=child_model,
    )

    parent_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=parent_id,
        content="Create the scripted writer child.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=parent_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", body.get("error")

    _wait_for_session_text(http_client, parent_id, "AUTO_WAKE_COMPLETE")
    response = http_client.get(f"/v1/sessions/{parent_id}/child_sessions")
    response.raise_for_status()
    child_sessions = response.json()["data"]
    assert child_sessions, f"child session did not appear for parent {parent_id}"
    child_id = child_sessions[0]["id"]
    _wait_for_session_text(http_client, child_id, "|CHILD_END")

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _tool_call(
                        "sys_session_get_history",
                        {"conversation_id": child_id, "tail_items": 1},
                        "call_default_history",
                    ),
                    _tool_call(
                        "sys_session_get_history",
                        {
                            "conversation_id": child_id,
                            "tail_items": 1,
                            "content_max_chars": 5000,
                        },
                        "call_raised_history",
                    ),
                ]
            },
            {"text": "HISTORY_READ_COMPLETE"},
        ],
        key=parent_model,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=parent_id,
        content="Read the child session history with both limits.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=parent_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", body.get("error")

    outputs = {
        item["call_id"]: json.loads(item["output"])
        for item in get_output_items(body, "function_call_output")
    }
    default_text = outputs["call_default_history"]["items"][-1]["text"]
    raised_text = outputs["call_raised_history"]["items"][-1]["text"]
    assert default_text == long_child_text[:2000] + " [truncated]"
    assert raised_text == long_child_text
