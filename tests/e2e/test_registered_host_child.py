"""Registered host placement across owned runner/host processes, without provider credentials."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import isolated_host
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    lookup_agent_id,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.test_subagent_restart_recovery_e2e import _session_items, _tool_call, _wait_for_text


@pytest.mark.timeout(360)
@pytest.mark.parametrize("restart_while_running", [False, True])
def test_registered_child_survives_parent_restart_and_closes_only_its_runner(
    live_server: str,
    live_runner_id: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    restart_live_runner: Callable[[], None],
    tmp_path: Path,
    restart_while_running: bool,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    parent_model, child_model = f"remote-parent-{suffix}", f"remote-child-{suffix}"
    mock_url = f"{mock_llm_server_url}/v1"
    reset_mock_llm(mock_llm_server_url)
    parent_name = register_inline_agent(
        http_client,
        name=parent_model,
        harness="openai-agents",
        model=parent_model,
        profile="",
        prompt="Delegate work, then collect the inbox when asked.",
        mock_llm_base_url=mock_url,
        extra_config={"spawn": True},
    )
    child_name = register_inline_agent(
        http_client,
        name=child_model,
        harness="openai-agents",
        model=child_model,
        profile="",
        prompt="Return the requested marker.",
        mock_llm_base_url=mock_url,
    )
    parent_id = create_runner_bound_session(
        http_client,
        agent_name=parent_name,
        runner_id=live_runner_id,
    )
    workspace = tmp_path / "remote-workspace"
    workspace.mkdir()
    with isolated_host(
        tmp_path / "host",
        live_server,
        env={"OPENAI_BASE_URL": mock_url, "OPENAI_API_KEY": "mock-key"},
        ready=lambda host: host.wait_online(http_client),
    ) as host:
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "REMOTE_RESULT_A", "delay": 20 if restart_while_running else 0}],
            key=child_model,
        )
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        _tool_call(
                            "sys_session_create",
                            {
                                "agent_id": lookup_agent_id(http_client, child_name),
                                "host_id": host.host_id,
                                "workspace": str(workspace),
                                "title": "independent build",
                                "message": "run A",
                            },
                            "create-remote",
                        )
                    ]
                },
                {"text": "Remote work dispatched."},
                {"text": "RESULT_READY_NOT_DRAINED"},
            ],
            key=parent_model,
        )
        send_user_message_to_session(http_client, session_id=parent_id, content="Delegate A")
        _wait_for_text(
            http_client,
            parent_id,
            "Remote work dispatched." if restart_while_running else "RESULT_READY_NOT_DRAINED",
            timeout=120,
        )
        children = http_client.get(f"/v1/sessions/{parent_id}/child_sessions").json()["data"]
        assert len(children) == 1
        child_id = children[0]["id"]
        child = http_client.get(f"/v1/sessions/{child_id}").json()
        assert child["host_id"] == host.host_id
        assert child["workspace"] == str(workspace)
        assert child["runner_id"] != live_runner_id
        assert child["parent_session_id"] == parent_id

        # Arm recovery before restart so its automatic wake owns this response queue.
        configure_mock_llm(
            mock_llm_server_url,
            [
                {"tool_calls": [_tool_call("sys_read_inbox", {}, "read-after-restart")]},
                {"text": "Result A collected."},
            ],
            key=parent_model,
        )
        restart_live_runner()
        _wait_for_text(http_client, parent_id, "REMOTE_RESULT_A", timeout=90)
        _wait_for_text(http_client, parent_id, "Result A collected.", timeout=30)

        configure_mock_llm(mock_llm_server_url, [{"text": "REMOTE_RESULT_B"}], key=child_model)
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        _tool_call(
                            "sys_session_send",
                            {
                                "session_id": child_id,
                                "args": "run B",
                            },
                            "continue-remote",
                        )
                    ]
                },
                {"text": "Second remote turn dispatched."},
                {"tool_calls": [_tool_call("sys_read_inbox", {}, "read-second-result")]},
                {"text": "Result B collected."},
            ],
            key=parent_model,
        )
        send_user_message_to_session(http_client, session_id=parent_id, content="Continue B")
        _wait_for_text(http_client, parent_id, "REMOTE_RESULT_B", timeout=120)
        _wait_for_text(http_client, parent_id, "Result B collected.", timeout=30)
        assert len(http_client.get(f"/v1/sessions/{parent_id}/child_sessions").json()["data"]) == 1
        assert "REMOTE_RESULT_B" in json.dumps(_session_items(http_client, child_id))

        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        _tool_call(
                            "sys_session_close", {"conversation_id": child_id}, "close-remote"
                        )
                    ]
                },
                {"text": "Remote child closed."},
            ],
            key=parent_model,
        )
        send_user_message_to_session(http_client, session_id=parent_id, content="Close child")
        _wait_for_text(http_client, parent_id, "Remote child closed.", timeout=30)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            snapshot = http_client.get(f"/v1/sessions/{child_id}").json()
            if snapshot.get("archived"):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("child was not archived")
        assert host.proc.poll() is None, host.log_path.read_text()[-4000:]
        host.wait_online(http_client, timeout=10)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status = http_client.get(f"/v1/runners/{child['runner_id']}/status").json()
            if status.get("online") is False:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("archiving did not stop the child runner")
        assert host.proc.poll() is None, host.log_path.read_text()[-4000:]
        host.wait_online(http_client, timeout=10)
        configure_mock_llm(mock_llm_server_url, [{"text": "PARENT_STILL_ALIVE"}], key=parent_model)
        send_user_message_to_session(
            http_client, session_id=parent_id, content="Confirm parent lives"
        )
        _wait_for_text(http_client, parent_id, "PARENT_STILL_ALIVE", timeout=30)
