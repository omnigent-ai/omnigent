"""An agent's ``sys_scheduled_task_list`` reads a trigger automation's ``rrule`` as null.

The server decides when a trigger polls, so it withholds the trigger's saved
rule; an agent that read the rule would report a schedule the trigger doesn't
follow. The mock model calls the tool in a real session, so the list crosses the
server, the runner's tool dispatch and the session's tool output.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.test_tool_call_policy_e2e import _tool_outputs

pytestmark = [
    pytest.mark.min_server_version("0.24.0"),
    # The first runner with the scheduled-task tools.
    pytest.mark.min_runner_version("0.6.0"),
]

_TRIGGER_PROMPT = "This automation watches Slack and starts one Omnigent session per message."
# A weekday rule an agent might save for a trigger; the server polls it regardless.
_TRIGGER_RULE = "FREQ=MINUTELY;INTERVAL=10;BYDAY=MO,TU,WE,TH,FR"
_SCHEDULE_RULE = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0"


def _create_paused_task(client: httpx.Client, body: dict[str, object], task_ids: list[str]) -> str:
    created = client.post("/v1/scheduled-tasks", json=body)
    assert created.status_code == 200, created.text
    task_id = str(created.json()["id"])
    task_ids.append(task_id)  # cleaned up by the caller even if pausing fails
    # Paused, so neither automation fires while the agent lists them.
    client.patch(f"/v1/scheduled-tasks/{task_id}", json={"state": "paused"}).raise_for_status()
    return task_id


def test_scheduled_task_list_reads_a_trigger_rrule_as_null(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    model = f"mock-tasklist-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        http_client,
        name=f"tasklist-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You list the user's automations with sys_scheduled_task_list when asked.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    agent_id = lookup_agent_id(http_client, agent_name)
    task_ids: list[str] = []
    try:
        trigger_id = _create_paused_task(
            http_client,
            {
                "name": f"Slack trigger {model}",
                "prompt": _TRIGGER_PROMPT,
                "rrule": _TRIGGER_RULE,
                "agent_id": agent_id,
                "session_mode": "continue",
            },
            task_ids,
        )
        scheduled_id = _create_paused_task(
            http_client,
            {
                "name": f"Weekday digest {model}",
                "prompt": "Summarize yesterday's activity.",
                "rrule": _SCHEDULE_RULE,
                "agent_id": agent_id,
            },
            task_ids,
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "call_list",
                            "name": "sys_scheduled_task_list",
                            "arguments": "{}",
                        }
                    ]
                },
                {"text": "You have two automations."},
            ],
            key=model,
        )
        session_id = create_runner_bound_session(
            http_client, agent_name=agent_name, runner_id=live_runner_id
        )
        response_id = send_user_message_to_session(
            http_client, session_id=session_id, content="List my automations."
        )
        body = poll_session_until_terminal(
            http_client, session_id=session_id, response_id=response_id, timeout=60
        )
    finally:
        for task_id in task_ids:
            http_client.delete(f"/v1/scheduled-tasks/{task_id}")

    assert body.get("status") == "completed", (
        f"turn did not complete: status={body.get('status')!r} error={body.get('error')!r}"
    )
    [output] = _tool_outputs(body)
    listed = {task["id"]: task["rrule"] for task in json.loads(output)["scheduled_tasks"]}
    assert listed[trigger_id] is None, output
    assert listed[scheduled_id] == _SCHEDULE_RULE, output
