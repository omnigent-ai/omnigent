"""E2E: ``sys_session_list`` carries a live ``status`` for a sub-agent whose
runner died mid-turn and was replaced inside the disconnect grace, so the
supervisor can tell the stopped child apart from one that is still working."""

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
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.helpers import POLL_INTERVAL_S

pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    # ``ChildSessionSummary.status`` ships with 0.18.0; older servers omit it.
    pytest.mark.min_server_version("0.18.0"),
]

_STATUS_TOKEN = "STATUSCHECK"
_SETTLED_STATUSES = {"idle", "failed"}


def _tool_call(name: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _child_sessions(client: httpx.Client, parent_id: str) -> list[dict[str, object]]:
    response = client.get(f"/v1/sessions/{parent_id}/child_sessions", params={"limit": 100})
    response.raise_for_status()
    return response.json()["data"]


def _gate_pending(mock_llm_server_url: str) -> bool:
    resp = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0)
    resp.raise_for_status()
    return bool(resp.json().get("pending"))


def _wait(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"timed out waiting for {what} after {timeout}s")


def _latest_session_list_subagents(
    items: list[dict[str, object]],
) -> list[dict[str, object]] | None:
    """Return ``sub_agents`` from the newest ``sys_session_list`` tool output."""
    for item in reversed(items):
        if item.get("type") != "function_call_output":
            continue
        raw = item.get("output")
        if not isinstance(raw, str) or "sub_agents" not in raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("sub_agents"), list):
            return parsed["sub_agents"]
    return None


def test_supervisor_sees_stopped_subagent_status_after_runner_restart(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    restart_live_runner: Callable[[], None],
) -> None:
    """The stopped child's ``sub_agents`` row carries a non-running ``status``."""
    suffix = uuid.uuid4().hex[:8]
    parent_model = f"supervisor-{suffix}"
    child_model = f"researcher-{suffix}"
    mock_base_url = f"{mock_llm_server_url}/v1"

    parent_name = register_inline_agent(
        http_client,
        name=f"supervisor-{suffix}",
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt=(
            "You are a supervisor. Dispatch the researcher when asked, then wait. "
            "When asked about the researcher's status, call sys_session_list first."
        ),
        mock_llm_base_url=mock_base_url,
        extra_config={
            "tools": {
                "researcher": {
                    "type": "agent",
                    "description": "Does a long research task.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": child_model,
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": mock_base_url,
                        },
                    },
                    "prompt": "Work on the long task.",
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
                        {"agent": "researcher", "title": "long-task", "args": "work on it"},
                        "dispatch-call",
                    )
                ]
            },
            {"text": "Dispatched the researcher; it is working."},
        ],
        key=parent_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, parent_model, "acknowledged")
    # Route the status turn by its message token so it survives the dispatch
    # queue being re-consumed when the interrupted turn recovers.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [_tool_call("sys_session_list", {}, "list-call")]},
            {"text": "Checked the researcher via sys_session_list."},
        ],
        key="status-check",
        match=_STATUS_TOKEN,
    )
    # The child turn blocks on the gate so it is genuinely mid-run at the kill.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "researching...", "block": True}],
        key=child_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, child_model, "finished the task")

    parent_id = create_runner_bound_session(
        http_client, agent_name=parent_name, runner_id=live_runner_id
    )

    send_user_message_to_session(
        http_client, session_id=parent_id, content="Dispatch the researcher on the long task."
    )

    child_id: str | None = None

    def child_running() -> bool:
        nonlocal child_id
        for child in _child_sessions(http_client, parent_id):
            if child.get("busy") or child.get("current_task_status") == "in_progress":
                child_id = str(child["id"])
                return True
        return False

    _wait(child_running, timeout=120, what="the sub-agent to start running")
    _wait(
        lambda: _gate_pending(mock_llm_server_url),
        timeout=60,
        what="the child turn to reach the gate",
    )
    assert child_id is not None

    # Connectivity interruption: the runner executing the child dies and a
    # replacement reconnects under the same id, inside the disconnect grace.
    restart_live_runner()

    def child_settled() -> bool:
        children = _child_sessions(http_client, parent_id)
        row = next((c for c in children if str(c["id"]) == child_id), None)
        assert row is not None, f"child {child_id} vanished from the parent's listing: {children}"
        return not row.get("busy") and row.get("current_task_status") != "in_progress"

    _wait(child_settled, timeout=120, what="the interrupted child to leave the running state")

    server_child_view = next(
        (c for c in _child_sessions(http_client, parent_id) if str(c["id"]) == child_id),
        None,
    )
    snapshot = http_client.get(f"/v1/sessions/{child_id}")
    snapshot.raise_for_status()
    server_snapshot_status = snapshot.json().get("status")

    send_user_message_to_session(
        http_client,
        session_id=parent_id,
        content=f"{_STATUS_TOKEN} Is the researcher still running? Check its status first.",
    )

    sub_agents: list[dict[str, object]] | None = None

    def saw_session_list() -> bool:
        nonlocal sub_agents
        response = http_client.get(
            f"/v1/sessions/{parent_id}/items", params={"limit": 1000, "order": "asc"}
        )
        response.raise_for_status()
        sub_agents = _latest_session_list_subagents(response.json()["data"])
        return sub_agents is not None

    _wait(saw_session_list, timeout=120, what="the supervisor to call sys_session_list")
    assert sub_agents is not None

    child_row = next(
        (row for row in sub_agents if row.get("conversation_id") == child_id),
        None,
    )
    assert child_row is not None, (
        f"the stopped child vanished from the supervisor's view; sub_agents={sub_agents}"
    )
    assert "status" in child_row, (
        "sys_session_list gave the supervisor no live status for the stopped child, so it "
        f"cannot tell the child stopped. child_row={child_row}; server child view="
        f"{server_child_view}; server snapshot status={server_snapshot_status!r}"
    )
    assert child_row["status"] in _SETTLED_STATUSES, (
        "sys_session_list did not report a settled, non-running status for the interrupted "
        f"child after its runner died. child_row={child_row}; server child view="
        f"{server_child_view}; server snapshot status={server_snapshot_status!r}"
    )
