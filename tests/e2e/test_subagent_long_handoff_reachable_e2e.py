"""End-to-end: a long sub-agent handoff must be fully reachable by its parent.

Journey (mock LLM, live server + runner):

1. An orchestrator parent dispatches a writer sub-agent via
   ``sys_session_send``.
2. The writer completes its turn with a report longer than the inbox
   delivery cap (12000 chars).
3. The parent is auto-woken and drains ``sys_read_inbox`` — the handoff
   arrives truncated (``...[truncated N chars]``).
4. The parent then tries the documented retrieval path —
   ``sys_session_get_history`` on the child with the maximum
   ``content_max_chars`` — to recover the complete report.

The contract under test: a handoff may be *delivered* truncated to bound
the wake prompt, but the complete text must remain directly reachable by
the parent through some retrieval tool. A regression that clamps both the
delivery and every retrieval to the same ceiling makes the tail of a
longer handoff unreachable — this test fails on that.

Invoke with::

    pytest tests/e2e/test_subagent_long_handoff_reachable_e2e.py -v --timeout=600
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

# Longer than the 12000-char inbox delivery cap, so delivery arrives
# truncated and only a working retrieval path can reach the tail.
_HANDOFF_LEN = 20000


def _tool_call(
    name: str,
    arguments: dict[str, object],
    call_id: str,
) -> dict[str, str]:
    """Build a mock LLM tool-call entry."""
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _session_items(
    http_client: httpx.Client,
    session_id: str,
) -> list[dict[str, Any]]:
    """Fetch a session's persisted conversation items (chronological)."""
    response = http_client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
    )
    response.raise_for_status()
    return response.json()["data"]


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


def _read_inbox_output(items: list[dict[str, Any]], call_id: str) -> str:
    """Extract the persisted output of one tool call by its call id."""
    for item in items:
        if item.get("type") == "function_call_output" and item.get("call_id") == call_id:
            output = item.get("output")
            return output if isinstance(output, str) else json.dumps(output)
    raise AssertionError(f"no function_call_output for call_id={call_id!r} in {items!r}")


def test_long_subagent_handoff_remains_fully_reachable_e2e(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """The complete text of a >12k sub-agent handoff is reachable by the parent."""
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-handoff-parent-{uid}"
    child_model = f"mock-handoff-child-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"

    begin_marker = f"HANDOFF_BEGIN|{uid}|"
    end_marker = f"|HANDOFF_END|{uid}"
    body_len = _HANDOFF_LEN - len(begin_marker) - len(end_marker)
    long_child_text = begin_marker + ("0123456789" * (body_len // 10 + 1))[:body_len] + end_marker
    assert len(long_child_text) == _HANDOFF_LEN

    parent_name = register_inline_agent(
        http_client,
        name=f"handoff-retrieval-{uid}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Follow the scripted mock tool calls exactly.",
        mock_llm_base_url=mock_base,
        extra_config={
            "tools": {
                "writer": {
                    "type": "agent",
                    "description": "Deterministic long-report writer.",
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
                        {
                            "agent": "writer",
                            "title": "long-report",
                            "args": "Write the complete report.",
                        },
                        "call_spawn",
                    )
                ]
            },
            {"text": "CHILD_DISPATCHED"},
            # Served to the auto-wake turn: drain the inbox, then finish.
            {"tool_calls": [_tool_call("sys_read_inbox", {}, "call_read_inbox")]},
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
        content="Delegate the full report to the writer sub-agent.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=parent_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", body.get("error")

    parent_items = _wait_for_session_text(http_client, parent_id, "AUTO_WAKE_COMPLETE")
    response = http_client.get(f"/v1/sessions/{parent_id}/child_sessions")
    response.raise_for_status()
    child_sessions = response.json()["data"]
    assert child_sessions, f"child session did not appear for parent {parent_id}"
    child_id = child_sessions[0]["id"]

    # Sanity: the child really produced and persisted the COMPLETE report,
    # so anything missing downstream was lost in delivery/retrieval.
    _wait_for_session_text(http_client, child_id, end_marker)

    # What the parent actually received in its inbox drain.
    inbox_output = _read_inbox_output(parent_items, "call_read_inbox")
    assert begin_marker in inbox_output, (
        f"sub-agent handoff never reached the parent inbox; got: {inbox_output[:500]!r}"
    )
    delivered_in_full = end_marker in inbox_output

    # The documented retrieval path: read the child's last item with the
    # maximum per-item content the tool accepts.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _tool_call(
                        "sys_session_get_history",
                        {
                            "conversation_id": child_id,
                            "tail_items": 1,
                            "content_max_chars": _HANDOFF_LEN,
                        },
                        "call_full_history",
                    )
                ]
            },
            {"text": "HISTORY_READ_COMPLETE"},
        ],
        key=parent_model,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=parent_id,
        content="Retrieve the writer's complete report.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=parent_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", body.get("error")

    outputs = {
        item["call_id"]: item["output"] for item in get_output_items(body, "function_call_output")
    }
    history_payload = json.loads(outputs["call_full_history"])
    assert "items" in history_payload, f"history read failed: {history_payload!r}"
    history_text = str(history_payload["items"][-1].get("text", ""))
    assert begin_marker in history_text, (
        f"history read did not return the child's report; got: {history_text[:500]!r}"
    )
    retrieved_in_full = end_marker in history_text

    print(f"handoff length: {_HANDOFF_LEN}")
    print(f"inbox delivery tail: ...{inbox_output[-160:]!r}")
    print(f"max history read length: {len(history_text)}")
    print(f"max history read tail: ...{history_text[-160:]!r}")

    assert delivered_in_full or retrieved_in_full, (
        "the sub-agent's long handoff is unreachable in full: inbox delivery "
        f"truncated it (tail: ...{inbox_output[-120:]!r}) and the maximum "
        "sys_session_get_history read returns the same truncated prefix "
        f"(tail: ...{history_text[-120:]!r}) — the parent has no direct way "
        "to retrieve the rest of the handoff"
    )
