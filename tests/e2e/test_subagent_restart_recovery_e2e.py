"""End-to-end coverage for sub-agent inbox recovery after runner restart.

This test uses the real server and runner subprocesses with the mock LLM. It
kills the runner after a child result reaches the process-local inbox, starts a
fresh runner against the unchanged server database, and drains the result from
the parent through ``sys_read_inbox``.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable

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

_CHILD_RESULT = "DURABLE_RESTART_RESULT"
_WAKE_WITHOUT_DRAIN = "WAKE_COMPLETED_WITHOUT_INBOX_DRAIN"

pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.min_server_version("0.3.0"),
]


def _tool_call(name: str, arguments: dict[str, str], call_id: str) -> dict[str, object]:
    """Build one mock Responses API tool-call entry.

    :param name: Tool name, e.g. ``"sys_read_inbox"``.
    :param arguments: JSON-serializable tool arguments.
    :param call_id: Stable mock call id.
    :returns: Mock LLM tool-call response entry.
    """
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _session_items(client: httpx.Client, session_id: str) -> list[dict[str, object]]:
    """Return all durable items for a session in chronological order.

    :param client: HTTP client connected to the live server.
    :param session_id: Session id to query.
    :returns: Durable session item dictionaries.
    """
    response = client.get(
        f"/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
    )
    response.raise_for_status()
    return response.json()["data"]


def _wait_for_text(client: httpx.Client, session_id: str, text: str, timeout: float) -> None:
    """Wait until text is durably visible in a session transcript.

    :param client: HTTP client connected to the live server.
    :param session_id: Session id to query.
    :param text: Exact marker expected anywhere in the serialized items.
    :param timeout: Maximum seconds to wait.
    :raises AssertionError: If the marker does not appear before timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if text in json.dumps(_session_items(client, session_id)):
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"{text!r} did not appear in session {session_id}")


def test_subagent_inbox_survives_real_runner_process_restart(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    restart_live_runner: Callable[[], None],
) -> None:
    """A fresh runner reconstructs an undrained terminal child result.

    The first runner dispatches a child and receives its completion wake, but
    the parent deliberately does not call ``sys_read_inbox``. The test then
    kills that process. On the next user turn, the replacement runner must boot
    the parent session, recover the child output from server state, and return
    it through the real inbox tool.

    :param http_client: Client connected to the real server subprocess.
    :param live_runner_id: Stable id shared by both runner generations.
    :param mock_llm_server_url: Mock Responses API base URL.
    :param restart_live_runner: Callback that kills and replaces the runner.
    """
    suffix = uuid.uuid4().hex[:8]
    parent_model = f"restart-parent-{suffix}"
    child_model = f"restart-child-{suffix}"
    mock_base_url = f"{mock_llm_server_url}/v1"
    parent_name = register_inline_agent(
        http_client,
        name=f"restart-parent-{suffix}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Dispatch the researcher and use sys_read_inbox when explicitly asked.",
        mock_llm_base_url=mock_base_url,
        extra_config={
            "tools": {
                "researcher": {
                    "type": "agent",
                    "description": "Returns a fixed restart recovery marker.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base_url,
                        },
                    },
                    "prompt": f"Return {_CHILD_RESULT} verbatim.",
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
                        {"agent": "researcher", "title": "restart", "args": "run"},
                        "dispatch-call",
                    )
                ]
            },
            {"text": "Child dispatched."},
            # Auto-wake proves runner one received completion, but deliberately
            # leaves its process-local inbox undrained before the crash.
            {"text": _WAKE_WITHOUT_DRAIN},
            {"tool_calls": [_tool_call("sys_read_inbox", {}, "inbox-call")]},
            {"text": "Recovered the child result after restart."},
        ],
        key=parent_model,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _CHILD_RESULT}],
        key=child_model,
    )

    parent_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )
    dispatch_id = send_user_message_to_session(
        http_client,
        session_id=parent_id,
        content="Dispatch the researcher.",
    )
    poll_session_until_terminal(
        http_client,
        session_id=parent_id,
        response_id=dispatch_id,
        timeout=180,
    )
    _wait_for_text(http_client, parent_id, _WAKE_WITHOUT_DRAIN, timeout=240)

    restart_live_runner()

    send_user_message_to_session(
        http_client,
        session_id=parent_id,
        content="Read the inbox now.",
    )
    # Session status can still reflect the preceding idle turn briefly after
    # enqueue, so wait for the durable recovered payload rather than sampling it.
    _wait_for_text(http_client, parent_id, _CHILD_RESULT, timeout=180)

    items = _session_items(http_client, parent_id)
    calls = {
        item.get("call_id")
        for item in items
        if item.get("type") == "function_call" and item.get("name") == "sys_read_inbox"
    }
    inbox_outputs = [
        item.get("output")
        for item in items
        if item.get("type") == "function_call_output" and item.get("call_id") in calls
    ]
    assert _CHILD_RESULT in json.dumps(inbox_outputs), json.dumps(items, indent=2)
