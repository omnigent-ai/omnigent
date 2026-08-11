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
from tests.e2e.helpers import POLL_INTERVAL_S

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


def _session_items(http_client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    """Return persisted session items in chronological order."""
    response = http_client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
    )
    response.raise_for_status()
    items: list[dict[str, Any]] = response.json()["data"]
    return items


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
        items = _session_items(http_client, session_id)
        if text in json.dumps(items):
            return items
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"{text!r} did not appear in session {session_id} within {timeout}s; last items={items!r}"
    )


def _wait_for_child_session(
    http_client: httpx.Client,
    parent_id: str,
    *,
    timeout: float = 120,
) -> str:
    """Poll until the parent exposes its child session."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = http_client.get(f"/v1/sessions/{parent_id}/child_sessions")
        response.raise_for_status()
        children = response.json().get("data", [])
        if children:
            return str(children[0]["id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"child session did not appear for parent {parent_id}")


def _history_text(body: dict[str, Any], call_id: str) -> str:
    """Extract the message text returned by one history tool call."""
    output_item = next(
        item
        for item in body["output"]
        if item.get("type") == "function_call_output" and item.get("call_id") == call_id
    )
    payload = json.loads(output_item["output"])
    text: str = payload["items"][-1]["text"]
    return text


def _read_child_history(
    http_client: httpx.Client,
    *,
    parent_id: str,
    parent_model: str,
    child_id: str,
    mock_llm_server_url: str,
    call_id: str,
    content_max_chars: int | None,
) -> str:
    """Run one parent turn that reads the child session history."""
    arguments: dict[str, object] = {
        "conversation_id": child_id,
        "tail_items": 1,
    }
    if content_max_chars is not None:
        arguments["content_max_chars"] = content_max_chars

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _tool_call("sys_session_get_history", arguments, call_id),
                ]
            },
            {"text": f"{call_id}_COMPLETE"},
        ],
        key=parent_model,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=parent_id,
        content="Read the child session history.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=parent_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", body.get("error")
    return _history_text(body, call_id)


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

    child_id = _wait_for_child_session(http_client, parent_id)
    _wait_for_session_text(http_client, child_id, "|CHILD_END")
    _wait_for_session_text(http_client, parent_id, "AUTO_WAKE_COMPLETE")

    default_text = _read_child_history(
        http_client,
        parent_id=parent_id,
        parent_model=parent_model,
        child_id=child_id,
        mock_llm_server_url=mock_llm_server_url,
        call_id="call_default_history",
        content_max_chars=None,
    )
    assert default_text == long_child_text[:2000] + " [truncated]"

    raised_text = _read_child_history(
        http_client,
        parent_id=parent_id,
        parent_model=parent_model,
        child_id=child_id,
        mock_llm_server_url=mock_llm_server_url,
        call_id="call_raised_history",
        content_max_chars=5000,
    )
    assert raised_text == long_child_text
