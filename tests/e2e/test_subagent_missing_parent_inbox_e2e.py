"""End-to-end reproduction: sub-agent terminal delivery must survive runner replacement.

A sub-agent's terminal ``external_session_status`` is rejected with HTTP 503
``missing_parent_inbox`` after the parent's runner is replaced. This strands the
parent forever: the child's completion can never be delivered to the parent's
process-local inbox because the replacement runner never recreated it.

This test uses the real server and runner subprocesses with the mock LLM. It
builds a genuine parent -> sub-agent conversation tree, restarts the runner
(durable server state survives; the process-local ``_session_inboxes`` map is
discarded), then delivers the child's terminal ``external_session_status: idle``
through the real server events endpoint -- exactly the signal the claude-native
forwarder posts at child turn end. The server forwards it to the parent's
replacement runner, which never re-ran the parent's ``_initialize_session`` (the
parent has taken no turn on it), so it has no parent inbox and the delivery is
rejected.

The child sub-agent is held open on the mock gate for the whole test so it never
delivers its result in-process before the restart: the terminal
``external_session_status`` POST is therefore the genuine first delivery attempt,
mirroring the native-forwarder edge under test.

Observed on the buggy build: the POST returns HTTP 503 with
``error.code == "runner_unavailable"`` and a message embedding the runner's
``{"error": "subagent_delivery_not_confirmed", "reason": "missing_parent_inbox"}``.
This test asserts the *fixed* contract -- the replacement runner must accept the
terminal delivery (seeding the cold parent inbox) -- so it fails on the buggy
build and passes once the fix lands.
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
    release_mock_gate,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.helpers import POLL_INTERVAL_S

_CHILD_RESULT = "COLD_PARENT_INBOX_CHILD_RESULT"

pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.min_server_version("0.3.0"),
]


def _tool_call(name: str, arguments: dict[str, str], call_id: str) -> dict[str, object]:
    """Build one mock Responses API tool-call entry.

    :param name: Tool name, e.g. ``"sys_session_send"``.
    :param arguments: JSON-serializable tool arguments.
    :param call_id: Stable mock call id.
    :returns: Mock LLM tool-call response entry.
    """
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _find_child_session_id(client: httpx.Client, parent_id: str, timeout: float) -> str:
    """Return the first sub-agent (child) session id under a parent.

    :param client: HTTP client connected to the live server.
    :param parent_id: Parent session/conversation id.
    :param timeout: Maximum seconds to wait for the child to appear.
    :returns: The child conversation id.
    :raises AssertionError: If no child appears before timeout.
    """
    deadline = time.monotonic() + timeout
    last_page: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        resp = client.get(
            f"/v1/sessions/{parent_id}/child_sessions",
            params={"limit": 100, "order": "asc"},
        )
        resp.raise_for_status()
        last_page = resp.json().get("data", [])
        for child in last_page:
            child_id = child.get("id")
            if child_id:
                return str(child_id)
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"no sub-agent child session appeared under parent {parent_id!r}; "
        f"last child_sessions page={last_page}"
    )


def test_subagent_terminal_status_accepted_after_runner_restart(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    restart_live_runner: Callable[[], None],
) -> None:
    """A sub-agent's terminal status must survive the parent's runner restart.

    Builds a real parent -> researcher sub-agent tree, restarts the runner while
    the child is still active (so the parent's process-local inbox is gone and
    is not recreated), then delivers the child's terminal
    ``external_session_status: idle`` through the real events endpoint. The
    replacement runner must accept the delivery instead of rejecting it with
    ``missing_parent_inbox`` -- otherwise the parent is stranded forever.

    :param http_client: Client connected to the real server subprocess.
    :param live_runner_id: Stable id shared by both runner generations.
    :param mock_llm_server_url: Mock Responses API base URL.
    :param restart_live_runner: Callback that kills and replaces the runner.
    """
    suffix = uuid.uuid4().hex[:8]
    parent_model = f"coldinbox-parent-{suffix}"
    child_model = f"coldinbox-child-{suffix}"
    mock_base_url = f"{mock_llm_server_url}/v1"

    parent_name = register_inline_agent(
        http_client,
        name=f"coldinbox-parent-{suffix}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Dispatch the researcher sub-agent when asked.",
        mock_llm_base_url=mock_base_url,
        extra_config={
            "tools": {
                "researcher": {
                    "type": "agent",
                    "description": "Returns a fixed marker.",
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
    # Parent: dispatch the researcher (async), then settle to idle. The dispatch
    # does not block on the child, so the parent turn completes while the child
    # is still running.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _tool_call(
                        "sys_session_send",
                        {"agent": "researcher", "title": "coldinbox", "args": "run"},
                        "dispatch-call",
                    )
                ]
            },
            {"text": "Dispatched the researcher."},
        ],
        key=parent_model,
    )
    # Any incidental parent turn (e.g. an auto-wake) must complete cleanly so the
    # pre-restart parent session cannot get stuck with an exhausted queue.
    set_fallback_mock_llm(mock_llm_server_url, parent_model, "Acknowledged.")
    # Child: block on the mock gate so it never completes / delivers in-process
    # before the restart. The terminal status is delivered explicitly below,
    # after the runner is replaced -- the native-forwarder edge under test.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _CHILD_RESULT, "block": True}],
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

    child_id = _find_child_session_id(http_client, parent_id, timeout=120)

    try:
        # Replace the runner process. Durable server state (the parent + child
        # conversations and their runner binding) survives, but the process-local
        # ``_session_inboxes`` map is gone. The replacement never ran the parent's
        # ``_initialize_session`` because the parent has taken no turn on it, so
        # the parent inbox is absent on the new runner.
        restart_live_runner()

        # Deliver the child's terminal status exactly as the claude-native
        # forwarder posts it at turn end. The server forwards it to the parent's
        # replacement runner, which must accept it (seeding the cold parent inbox)
        # rather than rejecting it with ``missing_parent_inbox``.
        resp = http_client.post(
            f"/v1/sessions/{child_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": _CHILD_RESULT},
            },
        )
    finally:
        # Best-effort: release the child gate so a leaked blocked request cannot
        # wedge the session-scoped mock server for later tests. The runner that
        # opened it is already dead, so this is a no-op cleanup.
        release_mock_gate(mock_llm_server_url)

    assert "missing_parent_inbox" not in resp.text, (
        "the parent's replacement runner rejected the sub-agent's "
        "terminal status with 'missing_parent_inbox', stranding the parent. The "
        "runner must seed the cold parent inbox and accept the terminal "
        f"delivery. status={resp.status_code} body={resp.text[:600]}"
    )
    assert resp.status_code in (200, 202, 204), (
        "expected the replacement runner to accept the sub-agent's "
        f"terminal status after restart, got HTTP {resp.status_code}: "
        f"{resp.text[:600]}"
    )
