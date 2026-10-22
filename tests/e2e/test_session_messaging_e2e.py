"""Directed work and addressed replies across real server, runner and harness processes.

The provider is scripted; accounts login, signed credentials and all application
processes are real. Native rows require Codex and tmux.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import yaml

from dev.repro_env.runtime import write_model_config
from tests._helpers.live_server import terminate_process
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    register_inline_agent,
    release_mock_gate,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.test_steering import _wait_for_mock_gate

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def messaging_processes(request, tmp_path: Path, harness: str):
    if harness == "codex-native":
        for executable in ("codex", "tmux"):
            if shutil.which(executable) is None:
                pytest.skip(f"requires the real {executable} executable")
    mock = request.getfixturevalue("isolated_mock_llm_server_url")
    with _messaging_stack(tmp_path, mock) as (stack, env):
        yield stack, env, mock


@pytest.fixture
def messaging_runtime(messaging_processes) -> tuple[str, str, str]:
    stack, _, mock = messaging_processes
    return stack.base_url, mock, stack.runner_id


@contextmanager
def _messaging_stack(tmp_path: Path, mock: str) -> Iterator[tuple[ServerRunner, dict[str, str]]]:
    config = tmp_path / "config"
    write_model_config(config, mock, "claude-sonnet-4-20250514", "gpt-4o")
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    # Native bridge sockets must fit macOS's 104-byte path limit.
    with tempfile.TemporaryDirectory(prefix="linked-native-", dir="/tmp") as native_state:
        env = {
            "OMNIGENT_FEATURES": "session_messaging,canvas",
            "OMNIGENT_CONFIG_HOME": str(config),
            "CODEX_HOME": str(tmp_path / "codex-config"),
            "OMNIGENT_CODEX_NATIVE_STATE_DIR": native_state,
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
        }
        with server_runner(
            tmp_path / "stack",
            server_cwd=_REPO,
            server_env={
                **env,
                "OMNIGENT_AUTH_PROVIDER": "accounts",
                "OMNIGENT_ACCOUNTS_COOKIE_SECRET": secrets.token_hex(32),
                "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": "alice",
                "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": "test-password-123",
                "OMNIGENT_ACCOUNTS_AUTO_OPEN": "0",
            },
            base_env=base_env,
        ) as stack:
            from omnigent.server.accounts_store import SqlAlchemyAccountStore
            from omnigent.server.passwords import hash_password

            accounts = SqlAlchemyAccountStore(stack.database_uri)
            for user in ("colleague", "viewer"):
                accounts.create_user_with_password(user, hash_password("test-password-123"))
            owner = _user_headers(stack.base_url, "alice")
            env["OMNIGENT_RUNNER_INITIAL_AUTH_TOKEN"] = owner["Authorization"].removeprefix(
                "Bearer "
            )
            stack.start_runner(env=env, cwd=_REPO, readiness_headers=owner)
            yield stack, env


def _user_headers(base: str, user: str) -> dict[str, str]:
    response = httpx.post(
        f"{base}/auth/login",
        json={"username": user, "password": "test-password-123", "issue_refresh": True},
        trust_env=False,
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['token']}"}


@contextmanager
def _owner_client(base: str) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=base, headers=_user_headers(base, "alice"), trust_env=False, timeout=60
    ) as client:
        yield client


def _set_communication(client: httpx.Client, enabled: bool, *, headers=None) -> dict:
    path = "/v1/session-communication/settings"
    current = client.get(path, headers=headers)
    current.raise_for_status()
    response = client.put(
        path,
        headers=headers,
        json={"enabled": enabled, "expected_version": current.json()["version"]},
    )
    response.raise_for_status()
    return response.json()


def _items(client: httpx.Client, session_id: str) -> list[dict]:
    response = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
    response.raise_for_status()
    return response.json()["data"]


def _wait_for_text(client: httpx.Client, session_id: str, text: str) -> list[dict]:
    deadline = time.monotonic() + 90
    observed = []
    while time.monotonic() < deadline:
        observed = _items(client, session_id)
        if text in json.dumps(observed):
            return observed
        time.sleep(0.1)
    raise AssertionError(f"Missing {text!r} in {session_id}: {observed}")


def _tool_output(mock: str, observed: list[dict], call_id: str) -> str:
    candidates = [*observed]
    # Codex returns MCP results to the provider without persisting them as chat items.
    for request in get_mock_requests(mock):
        candidates.extend(request.get("input", []))
    for item in candidates:
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == call_id
        ):
            output = item["output"]
            # Older Codex wraps the MCP content array in a timed text result.
            if isinstance(output, str) and output.startswith("Wall time:"):
                output = json.loads(output.split("\nOutput:\n", 1)[1])
            if isinstance(output, str):
                return output
            return "\n".join(
                block.get("text", "")
                for block in output
                if not block.get("text", "").startswith("Wall time:")
            )
    raise AssertionError(f"Missing tool result {call_id}")


def _sdk_bundle(name: str, mock: str) -> bytes:
    spec = {
        "name": name.lower().replace(" ", "-"),
        "prompt": "Use session messaging to coordinate independent workers.",
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o",
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock}/v1"},
        },
        "skills_filter": "none",
    }
    return bundle_files({"linked-agent.yaml": yaml.safe_dump(spec).encode()})


@pytest.mark.timeout(240)
@pytest.mark.parametrize("harness", ["openai-agents", "codex-native"])
def test_owner_message_reply_and_refusal(
    messaging_runtime, messaging_processes, tmp_path, harness
):
    base, mock, runner_id = messaging_runtime
    suffix = uuid4().hex[:8]
    set_fallback_mock_llm(
        mock,
        key="codex-auto-review",
        text='{"risk_level":"low","user_authorization":"high","outcome":"allow",'
        '"rationale":"Isolated requested test"}',
    )
    set_fallback_mock_llm(mock, key="_policy_llm_", text='{"action":"allow","reason":""}')

    def tool(target, message, call_id):
        result = {
            "name": "sys_session_message",
            "arguments": json.dumps({"session_id": target, "message": message}),
            "call_id": call_id,
        }
        if harness == "codex-native":
            result["namespace"] = "mcp__omnigent"
        return result

    def configure(marker, responses):
        configure_mock_llm(
            mock,
            responses,
            match=marker,
            required_tools=[
                "mcp__omnigent" if harness == "codex-native" else "sys_session_message"
            ],
        )

    with _owner_client(base) as client:
        sessions = []
        for name in ("Manager", "Worker"):
            workspace = tmp_path / name
            workspace.mkdir()
            metadata = {"title": name, "workspace": str(workspace)}
            if harness == "codex-native":
                created = create_native_session(
                    client, base, harness="codex", model="gpt-4o", metadata=metadata
                )
            else:
                response = post_session_bundle(
                    client.post, "/v1/sessions", _sdk_bundle(name, mock), metadata=metadata
                )
                response.raise_for_status()
                created = response.json()
            sessions.append(created["session_id"])
            bind_session_runner(client.patch, base, sessions[-1], runner_id)
        manager, worker = sessions

        def send(session_id, text, headers=None):
            response = client.post(
                f"/v1/sessions/{session_id}/events",
                headers=headers,
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
                },
            )
            assert response.status_code == 202, response.text

        try:
            manager_marker, worker_marker = f"MANAGER_{suffix}", f"WORKER_{suffix}"
            _set_communication(client, True)
            configure(worker_marker, [{"text": "WORKER_PRIVATE_OUTPUT"}])
            configure(
                manager_marker,
                [
                    {"tool_calls": [tool(worker, worker_marker, "send-work")]},
                    {"text": "MANAGER_SENT"},
                ],
            )
            send(manager, manager_marker)
            observed = _wait_for_text(client, manager, "MANAGER_SENT")
            receipt = json.loads(_tool_output(mock, observed, "send-work"))
            assert receipt.get("queued") is True, receipt
            _wait_for_text(client, worker, "WORKER_PRIVATE_OUTPUT")
            assert "WORKER_PRIVATE_OUTPUT" not in json.dumps(_items(client, manager))
            assert client.get(f"/v1/sessions/{worker}").json()["parent_session_id"] is None
            # Content routing prefers longer markers over earlier user messages.
            reply_marker = f"MANAGER_REPLY_{suffix}"
            configure(reply_marker, [{"text": "MANAGER_READ_REPLY"}])
            configure(
                worker_marker,
                [
                    {"tool_calls": [tool(manager, reply_marker, "send-reply")]},
                    {"text": "WORKER_REPLIED"},
                ],
            )
            send(worker, "Send the result back")
            _wait_for_text(client, worker, "WORKER_REPLIED")
            _wait_for_text(client, manager, "MANAGER_READ_REPLY")
            assert reply_marker in json.dumps(_items(client, manager))
            assert client.get(f"/v1/sessions/{manager}/communication-activity").json()["data"]
            if harness == "codex-native":
                stack, env, _ = messaging_processes
                terminate_process(stack.runner)
                poll_until(
                    lambda: not client.get(f"/v1/runners/{runner_id}/status").json()["online"],
                    timeout_s=30,
                )
                stack.runner = None
                stack.start_runner(env=env, cwd=_REPO, readiness_headers=client.headers)
                terminal = client.post(
                    f"/v1/sessions/{manager}/resources/terminals",
                    json={
                        "terminal": "codex",
                        "session_key": "main",
                        "ensure_native_terminal": True,
                    },
                )
                terminal.raise_for_status()
                terminal_marker = f"MANAGER_REPLY_TERMINAL_{suffix}"
                configure(
                    terminal_marker,
                    [
                        {"tool_calls": [tool(worker, "UNVERIFIED_MUST_NOT_RUN", "terminal-send")]},
                        {"text": "TERMINAL_CHECKED"},
                    ],
                )
                from omnigent.harnesses.codex_native.app_server import client_for_transport
                from omnigent.harnesses.codex_native.bridge import read_bridge_state

                states = [
                    state
                    for path in (stack.runner_home / ".omnigent/codex-native").glob("*/state.json")
                    if (state := read_bridge_state(path.parent)) and state.session_id == manager
                ]
                assert len(states) == 1

                async def start_unverified_turn():
                    # Direct native input has no server-issued clientUserMessageId.
                    native_client = client_for_transport(
                        states[0].socket_path, client_name="restart-check"
                    )
                    try:
                        await native_client.connect()
                        await native_client.request(
                            "turn/start",
                            {
                                "threadId": states[0].thread_id,
                                "input": [{"type": "text", "text": terminal_marker}],
                                "environments": [{"environmentId": "local", "cwd": states[0].cwd}],
                            },
                        )
                    finally:
                        await native_client.close()

                asyncio.run(start_unverified_turn())
                observed = _wait_for_text(client, manager, "TERMINAL_CHECKED")
                assert json.loads(_tool_output(mock, observed, "terminal-send"))["status"] == 403
                assert "UNVERIFIED_MUST_NOT_RUN" not in json.dumps(_items(client, worker))
                fresh_marker = f"MANAGER_REPLY_TERMINAL_FRESH_{suffix}"
                configure(f"WORKER_RESTART_{suffix}", [{"text": "WORKER_AFTER_RESTART"}])
                configure(
                    fresh_marker,
                    [
                        {"tool_calls": [tool(worker, f"WORKER_RESTART_{suffix}", "fresh-send")]},
                        {"text": "FRESH_REQUEST_SENT"},
                    ],
                )
                send(manager, fresh_marker)
                observed = _wait_for_text(client, manager, "FRESH_REQUEST_SENT")
                assert json.loads(_tool_output(mock, observed, "fresh-send"))["queued"] is True
                _wait_for_text(client, worker, "WORKER_AFTER_RESTART")
            _set_communication(client, False)
            off_marker = f"MANAGER_REPLY_TERMINAL_FRESH_OFF_{suffix}"
            configure(
                off_marker,
                [
                    {"tool_calls": [tool(worker, "MUST_NOT_RUN", "off-send")]},
                    {"text": "OFF_CHECKED"},
                ],
            )
            send(manager, off_marker)
            observed = _wait_for_text(client, manager, "OFF_CHECKED")
            assert json.loads(_tool_output(mock, observed, "off-send"))["status"] == 403
            assert "MUST_NOT_RUN" not in json.dumps(_items(client, worker))
        finally:
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}")


@pytest.mark.timeout(240)
@pytest.mark.parametrize("harness", ["openai-agents", "codex-native"])
@pytest.mark.parametrize("mixed_requesters", [False, True], ids=["same-person", "two-people"])
def test_busy_worker_preserves_consumed_requesters(
    messaging_runtime, tmp_path, harness, mixed_requesters
):
    base, mock, runner = messaging_runtime
    suffix = uuid4().hex[:8]
    owner, colleague = "alice", "colleague"
    sender = colleague if mixed_requesters else owner
    initial, steering, root = (f"{part}_{suffix}" for part in ("BUSY_WORKER", "STEER", "ROOT"))
    native = harness == "codex-native"
    if native:
        set_fallback_mock_llm(
            mock,
            key="codex-auto-review",
            text='{"risk_level":"low","user_authorization":"high","outcome":"allow",'
            '"rationale":"The user requested this isolated session-link test."}',
        )
    set_fallback_mock_llm(mock, key="_policy_llm_", text='{"action":"allow","reason":""}')

    def tool(name, arguments, call_id):
        return {
            "name": name,
            "arguments": json.dumps(arguments),
            "call_id": call_id,
            **({"namespace": "mcp__omnigent"} if native else {}),
        }

    def configure(marker, responses):
        configure_mock_llm(
            mock,
            responses,
            match=marker,
            required_tools=["mcp__omnigent" if native else "sys_session_message"],
        )

    with _owner_client(base) as client:
        sessions = []
        try:
            for name in ("Manager", "Worker", "Leaf"):
                workspace = tmp_path / name
                workspace.mkdir()
                metadata = {"title": f"{name} {suffix}", "workspace": str(workspace)}
                if native:
                    created = create_native_session(
                        client, base, harness="codex", model="gpt-4o", metadata=metadata
                    )
                else:
                    response = post_session_bundle(
                        client.post, "/v1/sessions", _sdk_bundle(name, mock), metadata=metadata
                    )
                    response.raise_for_status()
                    created = response.json()
                session_id = created["session_id"]
                sessions.append(session_id)
                bind_session_runner(client.patch, base, session_id, runner)
                if name != "Leaf":
                    client.put(
                        f"/v1/sessions/{session_id}/permissions",
                        json={"user_id": colleague, "level": 2},
                    ).raise_for_status()
            manager, worker, leaf = sessions
            _set_communication(client, True)
            worker_script = [
                {"block": True, "tool_calls": [tool("sys_read_inbox", {}, f"busy_{suffix}")]},
                {
                    "tool_calls": [
                        tool(
                            "sys_session_message",
                            {"session_id": leaf, "message": f"LEAF_INPUT_{suffix} current turn"},
                            f"hop_{suffix}",
                        )
                    ]
                },
                {"text": "BUSY_WORKER_FINISHED" if native else "CURRENT_TURN_FINISHED"},
            ]
            if not native:
                worker_script.extend(
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_message",
                                    {
                                        "session_id": leaf,
                                        "message": f"LEAF_INPUT_{suffix} next turn",
                                    },
                                    f"queued_{suffix}",
                                )
                            ]
                        },
                        {"text": "BUSY_WORKER_FINISHED"},
                    ]
                )
            configure(initial, worker_script)
            configure(
                f"LEAF_INPUT_{suffix}",
                [{"text": "LEAF_BUSY_REACHED"}, {"text": "LEAF_QUEUED_REACHED"}],
            )
            configure(
                root,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_message",
                                {"session_id": worker, "message": steering},
                                f"steer_{suffix}",
                            )
                        ]
                    },
                    {"text": "MANAGER_STEERED"},
                ],
            )

            def send(session_id, text, user):
                client.post(
                    f"/v1/sessions/{session_id}/events",
                    headers=_user_headers(base, user),
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    },
                ).raise_for_status()

            send(worker, initial, owner)
            _wait_for_mock_gate(mock)
            if mixed_requesters:
                send(worker, steering, colleague)
            else:
                send(manager, root, sender)
                manager_items = _wait_for_text(client, manager, "MANAGER_STEERED")
                receipt = json.loads(_tool_output(mock, manager_items, f"steer_{suffix}"))
                assert receipt["queued"] is True, receipt
            release_mock_gate(mock)
            worker_items = _wait_for_text(client, worker, "BUSY_WORKER_FINISHED")
            output = _tool_output(mock, worker_items, f"hop_{suffix}")
            requests = get_mock_requests(mock)
            assert any(
                initial in json.dumps(request.get("input", []))
                and steering in json.dumps(request.get("input", []))
                for request in requests
            ), "The worker's real provider never consumed the steering input"
            if native and mixed_requesters:
                assert "403" in output, output
                assert not any(item.get("role") == "user" for item in _items(client, leaf))
            else:
                result = json.loads(output)
                assert result["queued"] is True, result
                leaf_items = _wait_for_text(client, leaf, "LEAF_BUSY_REACHED")
                if not native:
                    queued = json.loads(_tool_output(mock, worker_items, f"queued_{suffix}"))
                    if mixed_requesters:
                        assert queued["status"] == 403, queued
                    else:
                        assert queued["queued"] is True, queued
                        leaf_items = _wait_for_text(client, leaf, "LEAF_QUEUED_REACHED")
                leaf_inputs = [item for item in leaf_items if item.get("role") == "user"]
                assert len(leaf_inputs) == (1 if native or mixed_requesters else 2)
                assert all(item["created_by"] == owner for item in leaf_inputs)
        finally:
            release_mock_gate(mock)
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()


@pytest.mark.timeout(180)
@pytest.mark.parametrize("harness", ["openai-agents"])
def test_existing_child_cancellation_with_message_authority(messaging_runtime) -> None:
    base, mock, runner_id = messaging_runtime
    suffix = uuid4().hex[:8]
    parent_model, child_model = f"parent-cancel-{suffix}", f"child-cancel-{suffix}"
    with _owner_client(base) as client:
        parent_name = register_inline_agent(
            client,
            name=parent_model,
            harness="openai-agents",
            model=parent_model,
            profile="",
            prompt="Use the worker for the requested work.",
            mock_llm_base_url=f"{mock}/v1",
            extra_config={
                "tools": {
                    "worker": {
                        "type": "agent",
                        "description": "A cancellable worker.",
                        "executor": {
                            "harness": "openai-agents",
                            "model": child_model,
                            "auth": {
                                "type": "api_key",
                                "api_key": "mock-key",
                                "base_url": f"{mock}/v1",
                            },
                        },
                        "prompt": "Handle the parent's request.",
                    }
                }
            },
        )
        parent = create_runner_bound_session(client, agent_name=parent_name, runner_id=runner_id)
        try:
            configure_mock_llm(mock, [{"text": "CHILD_FINISHED", "block": True}], key=child_model)
            configure_mock_llm(
                mock,
                [
                    {
                        "tool_calls": [
                            {
                                "name": "sys_session_send",
                                "arguments": json.dumps(
                                    {"agent": "worker", "title": "work", "args": "Start work"}
                                ),
                                "call_id": "child-start",
                            }
                        ]
                    },
                    {"text": "PARENT_DISPATCHED"},
                ],
                key=parent_model,
            )
            send_user_message_to_session(client, session_id=parent, content="Start the worker")
            _wait_for_mock_gate(mock)
            observed = _wait_for_text(client, parent, "PARENT_DISPATCHED")
            child = json.loads(_tool_output(mock, observed, "child-start"))["task_id"]
            assert client.get(f"/v1/sessions/{child}").json()["parent_session_id"] == parent
            configure_mock_llm(
                mock,
                [
                    {
                        "tool_calls": [
                            {
                                "name": "sys_cancel_task",
                                "arguments": json.dumps({"task_id": child}),
                                "call_id": "child-cancel",
                            }
                        ]
                    },
                    {"text": "PARENT_CANCEL_REQUESTED"},
                ],
                key=parent_model,
            )
            send_user_message_to_session(client, session_id=parent, content="Cancel the worker")
            observed = _wait_for_text(client, parent, "PARENT_CANCEL_REQUESTED")
            result = json.loads(_tool_output(mock, observed, "child-cancel"))
            assert result.get("cancelled") or result.get("cancel_requested"), result
            release_mock_gate(mock)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                snapshot = client.get(f"/v1/sessions/{child}").json()
                if snapshot["status"] == "idle":
                    break
                time.sleep(0.1)
            assert snapshot["status"] == "idle", snapshot
            assert "CHILD_FINISHED" not in json.dumps(_items(client, child))
        finally:
            release_mock_gate(mock)
            client.delete(f"/v1/sessions/{parent}")
