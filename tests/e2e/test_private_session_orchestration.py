"""Real local host/server/SDK and Codex processes; scripted model, no cloud sandbox."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest
import yaml

from dev.repro_env.runtime import write_model_config
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.timeout(300)
@pytest.mark.parametrize("harness", ["openai-agents", "codex-native"])
def test_private_manager_launch_send_read_and_sharing(
    tmp_path, isolated_mock_llm_server_url, harness
):
    if harness == "codex-native" and any(shutil.which(cmd) is None for cmd in ("codex", "tmux")):
        pytest.skip("requires Codex and tmux")
    mock = isolated_mock_llm_server_url
    config = tmp_path / "config"
    write_model_config(config, mock, "claude-sonnet-4-20250514", "gpt-4o")
    base_env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TMPDIR"}}
    with tempfile.TemporaryDirectory(prefix="orch-native-", dir="/tmp") as native_state:
        env = {
            "OMNIGENT_FEATURES": "session_orchestration",
            "OMNIGENT_CONFIG_HOME": str(config),
            "OMNIGENT_CODEX_NATIVE_STATE_DIR": native_state,
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
        }
        with server_runner(
            tmp_path / "stack", server_cwd=_REPO, server_env=env, base_env=base_env
        ) as stack:
            stack.start_host(env=env, cwd=_REPO)
            with httpx.Client(base_url=stack.base_url, timeout=60, trust_env=False) as client:
                poll_until(
                    lambda: [
                        h
                        for h in client.get("/v1/hosts").json().get("hosts", [])
                        if h.get("status") == "online"
                    ],
                    timeout_s=45,
                )
                host_id = client.get("/v1/hosts").json()["hosts"][0]["host_id"]
                set_fallback_mock_llm(
                    mock, key="_policy_llm_", text='{"action":"allow","reason":""}'
                )
                set_fallback_mock_llm(
                    mock,
                    key="codex-auto-review",
                    text=(
                        '{"risk_level":"low","user_authorization":"high",'
                        '"outcome":"allow","rationale":"Isolated test"}'
                    ),
                )

                def configure(marker, responses):
                    configure_mock_llm(
                        mock,
                        responses,
                        match=marker,
                        required_tools=[
                            "mcp__omnigent" if harness == "codex-native" else "sys_session_message"
                        ],
                    )

                def tool(name, args, call_id):
                    result = {"name": name, "arguments": json.dumps(args), "call_id": call_id}
                    if harness == "codex-native":
                        result["namespace"] = "mcp__omnigent"
                    return result

                def items(session):
                    response = client.get(f"/v1/sessions/{session}/items", params={"limit": 100})
                    response.raise_for_status()
                    return response.json()["data"]

                def wait_text(session, text):
                    return poll_until(lambda: text in json.dumps(items(session)), timeout_s=90)

                def send(session, text):
                    response = client.post(
                        f"/v1/sessions/{session}/events",
                        json={
                            "type": "message",
                            "data": {
                                "role": "user",
                                "content": [{"type": "input_text", "text": text}],
                            },
                        },
                    )
                    assert response.status_code == 202, response.text

                sessions = []
                for title in ("Manager", "Worker"):
                    metadata = {
                        "title": title,
                        "host_id": host_id,
                        "workspace": str(stack.workspace),
                    }
                    if harness == "codex-native":
                        created = create_native_session(
                            client,
                            stack.base_url,
                            harness="codex",
                            model="gpt-4o",
                            metadata=metadata,
                        )
                    else:
                        spec = {
                            "name": title.lower(),
                            "prompt": "Coordinate session work.",
                            "spawn": True,
                            "executor": {
                                "harness": harness,
                                "model": "gpt-4o",
                                "auth": {
                                    "type": "api_key",
                                    "api_key": "mock-key",
                                    "base_url": f"{mock}/v1",
                                },
                            },
                            "skills_filter": "none",
                        }
                        response = post_session_bundle(
                            client.post,
                            "/v1/sessions",
                            bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()}),
                            metadata=metadata,
                        )
                        response.raise_for_status()
                        created = response.json()
                    sessions.append(created["session_id"])
                manager, worker = sessions
                shared = client.put(
                    f"/v1/sessions/{worker}/permissions",
                    json={"user_id": "__public__", "level": 1},
                )
                assert shared.status_code == 200, shared.text
                configure("WORKER_TASK", [{"text": "WORKER_RESULT"}])
                configure(
                    "MANAGER_SEND",
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_message",
                                    {"session_id": worker, "message": "WORKER_TASK"},
                                    "send",
                                )
                            ]
                        },
                        {"text": "MANAGER_SENT"},
                    ],
                )
                send(manager, "MANAGER_SEND")
                wait_text(manager, "MANAGER_SENT")
                wait_text(worker, "WORKER_RESULT")
                assert "WORKER_RESULT" not in json.dumps(items(manager)), (
                    "No automatic reply forwarding"
                )
                configure(
                    "MANAGER_SEND_READ",
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_get_history", {"conversation_id": worker}, "read"
                                )
                            ]
                        },
                        {"text": "MANAGER_READ"},
                    ],
                )
                send(manager, "MANAGER_SEND_READ")
                wait_text(manager, "MANAGER_READ")
                assert "WORKER_RESULT" in json.dumps(get_mock_requests(mock)), (
                    "History reaches the model"
                )

                agent = client.get(f"/v1/sessions/{worker}").json()["agent_id"]
                configure("NEW_WORKER_TASK", [{"text": "NEW_WORKER_RESULT"}])
                configure(
                    "MANAGER_SEND_READ_CREATE",
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_create",
                                    {
                                        "agent_id": agent,
                                        "placement": "same_host",
                                        "title": "Independent worker",
                                        "message": "NEW_WORKER_TASK",
                                    },
                                    "create",
                                )
                            ]
                        },
                        {"text": "MANAGER_CREATED"},
                    ],
                )
                send(manager, "MANAGER_SEND_READ_CREATE")
                wait_text(manager, "MANAGER_CREATED")
                rows = client.get("/v1/sessions", params={"visibility": "mine"}).json()["data"]
                created = next(row for row in rows if row["title"] == "Independent worker")
                created = client.get(f"/v1/sessions/{created['id']}").json()
                assert created["parent_session_id"] is None
                assert created["host_id"] == host_id
                wait_text(created["id"], "NEW_WORKER_RESULT")
                grants = client.get(f"/v1/sessions/{created['id']}/permissions").json()[
                    "permissions"
                ]
                assert [g["user_id"] for g in grants] == ["local"]

                before_managed = {
                    row["id"]
                    for row in client.get("/v1/sessions", params={"visibility": "mine"}).json()[
                        "data"
                    ]
                }
                configure(
                    "MANAGER_SEND_READ_CREATE_MANAGED",
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_create",
                                    {"agent_id": agent, "placement": "managed"},
                                    "managed",
                                )
                            ]
                        },
                        {"text": "MANAGED_UNAVAILABLE"},
                    ],
                )
                send(manager, "MANAGER_SEND_READ_CREATE_MANAGED")
                wait_text(manager, "MANAGED_UNAVAILABLE")
                assert "Managed sandboxes are unavailable" in json.dumps(get_mock_requests(mock))
                assert {
                    row["id"]
                    for row in client.get("/v1/sessions", params={"visibility": "mine"}).json()[
                        "data"
                    ]
                } == before_managed

                shared = client.put(
                    f"/v1/sessions/{manager}/permissions",
                    json={"user_id": "__public__", "level": 1},
                )
                assert shared.status_code == 200, shared.text
                configure(
                    "MANAGER_SEND_READ_CREATE_MANAGED_BLOCK",
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_message",
                                    {"session_id": worker, "message": "MUST_NOT_DELIVER"},
                                    "blocked",
                                )
                            ]
                        },
                        {"text": "MANAGER_BLOCKED"},
                    ],
                )
                send(manager, "MANAGER_SEND_READ_CREATE_MANAGED_BLOCK")
                wait_text(manager, "MANAGER_BLOCKED")
                assert "MUST_NOT_DELIVER" not in json.dumps(items(worker))
                assert "Shared sessions cannot" in json.dumps(get_mock_requests(mock))

                configure(
                    "SCHEDULED_MANAGER_TASK",
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_message",
                                    {
                                        "session_id": worker,
                                        "message": "SCHEDULED_MUST_NOT_DELIVER",
                                    },
                                    "scheduled-message",
                                )
                            ]
                        },
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_create",
                                    {"agent_id": agent, "placement": "same_host"},
                                    "scheduled-create",
                                )
                            ]
                        },
                        {"text": "SCHEDULED_MANAGER_BLOCKED"},
                    ],
                )
                response = client.post(
                    "/v1/scheduled-tasks",
                    json={
                        "name": "Scheduled worker",
                        "prompt": "SCHEDULED_MANAGER_TASK",
                        "rrule": "FREQ=DAILY",
                        "timezone": "UTC",
                        "agent_id": agent,
                        "host_id": host_id,
                        "workspace": str(stack.workspace),
                    },
                )
                assert response.status_code == 200, response.text
                task_id = response.json()["id"]
                response = client.post(f"/v1/scheduled-tasks/{task_id}/run")
                assert response.status_code == 202, response.text
                poll_until(
                    lambda: (
                        client.get(f"/v1/scheduled-tasks/{task_id}")
                        .json()
                        .get("last_run_conversation_id")
                    ),
                    timeout_s=60,
                )
                scheduled = client.get(f"/v1/scheduled-tasks/{task_id}").json()[
                    "last_run_conversation_id"
                ]
                wait_text(scheduled, "SCHEDULED_MANAGER_BLOCKED")
                assert "SCHEDULED_MUST_NOT_DELIVER" not in json.dumps(items(worker))
                assert "Scheduled sessions cannot" in json.dumps(get_mock_requests(mock))
                after_scheduled = {
                    row["id"]
                    for row in client.get("/v1/sessions", params={"visibility": "mine"}).json()[
                        "data"
                    ]
                }
                assert after_scheduled == before_managed | {scheduled}

                fork = client.post(
                    f"/v1/sessions/{manager}/fork",
                    json={"side_chat": True, "title": "Private side chat"},
                )
                assert fork.status_code == 201, fork.text
                side_chat = fork.json()["id"]
                grants = client.get(f"/v1/sessions/{side_chat}/permissions").json()["permissions"]
                assert [g["user_id"] for g in grants] == ["local"]
                bound = client.patch(
                    f"/v1/sessions/{side_chat}",
                    json={"runner_id": client.get(f"/v1/sessions/{manager}").json()["runner_id"]},
                )
                assert bound.status_code == 200, bound.text
                configure(
                    "MANAGER_SEND_READ_CREATE_MANAGED_BLOCK_SIDE_CHAT",
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_message",
                                    {
                                        "session_id": worker,
                                        "message": "SIDE_CHAT_MUST_NOT_DELIVER",
                                    },
                                    "side-chat-message",
                                )
                            ]
                        },
                        {"text": "SIDE_CHAT_BLOCKED"},
                    ],
                )
                send(side_chat, "MANAGER_SEND_READ_CREATE_MANAGED_BLOCK_SIDE_CHAT")
                wait_text(side_chat, "SIDE_CHAT_BLOCKED")
                assert "SIDE_CHAT_MUST_NOT_DELIVER" not in json.dumps(items(worker))
                # Native tool results reach the model without entering the UI transcript.
                side_chat_results = [
                    entry
                    for request in get_mock_requests(mock)
                    for entry in request.get("input", request.get("messages", []))
                    if isinstance(entry, dict)
                    and (
                        (
                            entry.get("type") == "function_call_output"
                            and entry.get("call_id") == "side-chat-message"
                        )
                        or (
                            entry.get("role") == "tool"
                            and entry.get("tool_call_id") == "side-chat-message"
                        )
                    )
                ]
                assert "Shared sessions cannot" in json.dumps(side_chat_results)
