"""E2E happy-path for work-items + scheduler tools (#3/#6/#12).

Drives a full turn through the live server + runner with a mock LLM: the agent
emits ``create_work_item`` then ``create_loop`` tool calls, the runner dispatches
them (proxying to the server's REST API), and we assert both rows landed by
reading them back over ``/v1/work-items`` and ``/v1/schedules``. The
work-management builtins are framework-auto-registered (#12), so the agent spec
declares no special tools.
"""

from __future__ import annotations

import json
import uuid

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


def test_agent_creates_work_item_and_loop(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    using_mock_llm: bool,
) -> None:
    """Agent tool calls persist a work item (#3) and a cron loop (#6).

    Requires a real LLM (``--llm-api-key``): the work-item/schedule builtins
    are *proxied* (runner → server REST), and the mock LLM + native-harness
    bridge replays canned tool-call JSON without actually driving the bridge's
    proxied-tool dispatch, so the side effect (persistence) never fires under
    the mock. The proxy mapping itself is covered unit-side by
    ``tests/runner/test_work_management_dispatch.py``; this asserts the full
    agent→runner→server→store path when a real model issues the calls.
    """
    if using_mock_llm:
        pytest.skip("needs a real LLM to drive native-bridge proxied-tool dispatch")
    reset_mock_llm(mock_llm_server_url)

    model = f"mock-wm-{uuid.uuid4().hex[:6]}"
    marker = uuid.uuid4().hex[:8]
    task_title = f"weekly-report-{marker}"
    loop_name = f"weekly-loop-{marker}"

    agent_name = register_inline_agent(
        http_client,
        name=f"wm-agent-{marker}",
        harness="claude-sdk",
        model=model,
        profile="",
        prompt="You manage work items and schedules using the provided tools.",
        mock_llm_base_url=mock_llm_server_url,
    )

    # Round 1: create a work item. Round 2: create a weekly loop. Round 3: done.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": "create_work_item",
                        "arguments": json.dumps({"title": task_title, "source": "manual"}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": "create_loop",
                        "arguments": json.dumps(
                            {
                                "name": loop_name,
                                "prompt": "Post the weekly report.",
                                "cron": "0 9 * * 1",
                            }
                        ),
                    }
                ]
            },
            {"text": f"Created work item and weekly loop ({marker})."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Create a work item for the weekly report and a weekly loop to post it.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=180,
    )
    assert body["status"] == "completed", f"turn failed: {body.get('error')}"

    # The work item landed in the store, readable over REST.
    work_items = http_client.get("/v1/work-items").json()["data"]
    assert any(w["title"] == task_title for w in work_items), (
        f"work item {task_title!r} not found in {[w['title'] for w in work_items]}"
    )

    # The cron loop landed too.
    schedules = http_client.get("/v1/schedules", params={"conversation_id": session_id}).json()[
        "data"
    ]
    assert any(s["name"] == loop_name and s["cron"] == "0 9 * * 1" for s in schedules), (
        f"loop {loop_name!r} not found in {[s.get('name') for s in schedules]}"
    )
