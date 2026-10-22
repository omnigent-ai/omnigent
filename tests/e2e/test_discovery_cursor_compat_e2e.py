"""E2E compatibility coverage for discovery-tool cursor continuations."""

from __future__ import annotations

import json
import os
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


@pytest.mark.compat_smoke
def test_session_list_cursor_round_trips_through_runner_and_old_server(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A returned cursor drives a distinct second page through the live runner.

    The fast compatibility gate runs this against a new runner and the latest
    released server. It is intentionally skipped with a pinned old runner:
    cursor pagination is a new runner-owned tool contract, so that direction
    is covered by the old runner's pre-existing schema instead.
    """
    if os.environ.get("OMNIGENT_COMPAT_RUNNER_VERSION"):
        pytest.skip("cursor pagination requires the checked-out runner")

    suffix = uuid.uuid4().hex[:8]
    model = f"discovery-cursor-{suffix}"
    agent_name = register_inline_agent(
        http_client,
        name=f"discovery-cursor-{suffix}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Follow the scripted tool calls exactly.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    # Ensure the permission-bounded global list has another entry to continue to.
    create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    def tool_call(arguments: dict[str, object]) -> dict[str, object]:
        return {
            "tool_calls": [
                {
                    "call_id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": "sys_session_list",
                    "arguments": json.dumps(arguments),
                }
            ]
        }

    configure_mock_llm(
        mock_llm_server_url,
        [tool_call({"limit": 1}), {"text": "first page complete"}],
        key=model,
    )
    first_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="List one session.",
    )
    first_turn = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=first_response_id,
        timeout=120,
    )
    assert first_turn["status"] == "completed", first_turn

    def list_outputs() -> list[str]:
        response = http_client.get(f"/v1/sessions/{session_id}/items", params={"limit": 1000})
        response.raise_for_status()
        flattened = [
            {**item, **item["data"]} if isinstance(item.get("data"), dict) else item
            for item in response.json()["data"]
        ]
        call_ids = {
            item.get("call_id")
            for item in flattened
            if item.get("type") == "function_call" and item.get("name") == "sys_session_list"
        }
        return [
            str(item.get("output") or "")
            for item in flattened
            if item.get("type") == "function_call_output" and item.get("call_id") in call_ids
        ]

    first_page = json.loads(list_outputs()[-1])
    cursor = first_page["page"].get("next_cursor")
    assert isinstance(cursor, str) and cursor
    assert first_page["page"]["has_more"] == {"sessions": True}

    configure_mock_llm(
        mock_llm_server_url,
        [tool_call({"limit": 1, "cursor": cursor}), {"text": "second page complete"}],
        key=model,
    )
    second_response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Continue the list with the cursor.",
    )
    second_turn = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=second_response_id,
        timeout=120,
    )
    assert second_turn["status"] == "completed", second_turn

    second_page = json.loads(list_outputs()[-1])
    assert second_page["sessions"][0]["session_id"] != first_page["sessions"][0]["session_id"]
