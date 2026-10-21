"""Directed work and addressed replies across real server, runner and harness processes.

The provider is scripted. Local header authentication does not validate the
deployment's human/runner credential boundary. Native rows require Codex and tmux.
"""

from __future__ import annotations

import json
import os
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
from tests.helpers.ui_configuration import prepared_repro_environment

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def linked_runtime(request, tmp_path: Path, harness: str) -> Iterator[tuple[str, str, str]]:
    if harness == "codex-native":
        for executable in ("codex", "tmux"):
            if shutil.which(executable) is None:
                pytest.skip(f"requires the real {executable} executable")
    prepared = prepared_repro_environment()
    delay_handoff = getattr(request, "param", None) == "delay-handoff"
    if base := prepared["OMNIGENT_REPRO_SERVER_URL"]:
        if delay_handoff:
            pytest.skip("Transport-delay test owns its server bootstrap; run outside verify-env")
        response = httpx.get(f"{base}/v1/info", trust_env=False)
        response.raise_for_status()
        assert response.json()["features"].get("session_links"), response.text
        yield base, prepared["OMNIGENT_REPRO_MODEL_URL"], prepared["OMNIGENT_REPRO_RUNNER_ID"]
        return
    mock = request.getfixturevalue("isolated_mock_llm_server_url")
    with _linked_stack(tmp_path, mock, delay_handoff=delay_handoff) as (stack, _):
        yield stack.base_url, mock, stack.runner_id


@contextmanager
def _linked_stack(
    tmp_path: Path, mock: str, *, delay_handoff: bool = False
) -> Iterator[tuple[ServerRunner, dict[str, str]]]:
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
            "OMNIGENT_FEATURES": "session_links",
            "OMNIGENT_CONFIG_HOME": str(config),
            "CODEX_HOME": str(tmp_path / "codex-config"),
            "OMNIGENT_CODEX_NATIVE_STATE_DIR": native_state,
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
        }
        bootstrap = None
        if delay_handoff:
            gate = tmp_path / "handoff"
            gate.mkdir()
            env["OMNIGENT_E2E_LINK_GATE"] = str(gate)
            bootstrap = "from tests.e2e._session_link_delay_server import main; main()"
        with server_runner(
            tmp_path / "stack",
            server_cwd=_REPO,
            server_env=env,
            base_env=base_env,
            server_bootstrap=bootstrap,
        ) as stack:
            stack.start_runner(env=env, cwd=_REPO)
            yield stack, env


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
        "prompt": "Use session links to coordinate independent workers.",
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o",
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock}/v1"},
        },
        "skills_filter": "none",
    }
    return bundle_files({"linked-agent.yaml": yaml.safe_dump(spec).encode()})


@pytest.mark.timeout(180)
@pytest.mark.parametrize("harness", ["openai-agents"])
def test_existing_child_cancellation_with_link_authority(linked_runtime) -> None:
    base, mock, runner_id = linked_runtime
    suffix = uuid4().hex[:8]
    parent_model, child_model = f"parent-cancel-{suffix}", f"child-cancel-{suffix}"
    with httpx.Client(base_url=base, trust_env=False, timeout=60) as client:
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


@pytest.mark.timeout(360)
@pytest.mark.parametrize("harness", ["openai-agents", "codex-native"])
def test_delegate_addressed_reply_and_disconnect(
    linked_runtime: tuple[str, str, str], tmp_path: Path, harness: str
) -> None:
    base, mock, runner_id = linked_runtime
    suffix = uuid4().hex[:8]
    if harness == "codex-native":
        set_fallback_mock_llm(
            mock,
            key="codex-auto-review",
            text='{"risk_level":"low","user_authorization":"high","outcome":"allow",'
            '"rationale":"The user requested this isolated session-link test."}',
        )
    set_fallback_mock_llm(mock, key="_policy_llm_", text='{"action":"allow","reason":""}')

    def tool(name: str, arguments: dict, call_id: str) -> dict:
        result = {"name": name, "arguments": json.dumps(arguments), "call_id": call_id}
        if harness == "codex-native":
            result["namespace"] = "mcp__omnigent"
        return result

    def configure(marker: str, responses: list[dict]) -> None:
        configure_mock_llm(
            mock,
            responses,
            match=marker,
            required_tools=["mcp__omnigent" if harness == "codex-native" else "sys_session_links"],
        )

    with httpx.Client(base_url=base, trust_env=False, timeout=60) as client:
        sessions = []

        def create(title: str) -> str:
            workspace = tmp_path / title
            workspace.mkdir()
            metadata = {"title": title, "workspace": str(workspace)}
            if harness == "codex-native":
                created = create_native_session(
                    client, base, harness="codex", model="gpt-4o", metadata=metadata
                )
            else:
                response = post_session_bundle(
                    client.post,
                    "/v1/sessions",
                    _sdk_bundle(title, mock),
                    metadata=metadata,
                )
                response.raise_for_status()
                created = response.json()
            session_id = created["session_id"]
            sessions.append(session_id)
            bind_session_runner(client.patch, base, session_id, runner_id)
            return session_id

        def send(session_id: str, text: str) -> None:
            response = client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
                },
            )
            assert response.status_code == 202, response.text

        try:
            manager, worker = create(f"Manager {suffix}"), create(f"Worker {suffix}")
            manager_marker, worker_marker = f"MANAGER_{suffix}", f"WORKER_{suffix}"
            approved = client.post(f"/v1/sessions/{manager}/links", json={"target_id": worker})
            assert approved.status_code == 201, approved.text
            configure(worker_marker, [{"text": "WORKER_ORDINARY_OUTPUT"}])
            configure(
                manager_marker,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_delegate",
                                {"target_id": worker, "message": worker_marker},
                                f"delegate_{suffix}",
                            )
                        ]
                    },
                    {"text": "MANAGER_SENT"},
                ],
            )
            send(manager, manager_marker)
            observed = _wait_for_text(client, manager, "MANAGER_SENT")
            receipt = json.loads(_tool_output(mock, observed, f"delegate_{suffix}"))
            assert receipt["delivery_state"] == "accepted", receipt
            request_id = receipt["id"]
            _wait_for_text(client, worker, "WORKER_ORDINARY_OUTPUT")
            assert "WORKER_ORDINARY_OUTPUT" not in json.dumps(_items(client, manager))

            addressed_reply = f"ADDRESSED_REPLY_{suffix}"
            configure(
                worker_marker,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_reply",
                                {"request_id": request_id, "message": addressed_reply},
                                f"reply_{suffix}",
                            ),
                            tool(
                                "sys_session_delegate",
                                {"target_id": manager, "message": "NEW_REVERSE_WORK"},
                                f"reverse_{suffix}",
                            ),
                        ]
                    },
                    {"text": "WORKER_REPLIED"},
                ],
            )
            send(worker, worker_marker + " send the addressed reply")
            observed = _wait_for_text(client, worker, "WORKER_REPLIED")
            assert json.loads(_tool_output(mock, observed, f"reply_{suffix}"))["published"] is True
            assert json.loads(_tool_output(mock, observed, f"reverse_{suffix}"))["status"] == 403
            _wait_for_text(client, manager, addressed_reply)
            viewer = "later-viewer@example.test"
            client.put(
                f"/v1/sessions/{manager}/permissions", json={"user_id": viewer, "level": 1}
            ).raise_for_status()
            shared = client.get(
                f"/v1/sessions/{manager}/items", headers={"X-Forwarded-Email": viewer}
            )
            assert shared.status_code == 200, shared.text
            assert addressed_reply in shared.text
            assert client.get(
                f"/v1/sessions/{worker}", headers={"X-Forwarded-Email": viewer}
            ).status_code in (403, 404)
            configure(
                manager_marker,
                [
                    {
                        "tool_calls": [
                            tool("sys_session_task", {"request_id": request_id}, f"poll_{suffix}"),
                            tool("sys_read_inbox", {}, f"inbox_{suffix}"),
                        ]
                    },
                    {"text": "MANAGER_COLLECTED"},
                ],
            )
            send(manager, manager_marker + " collect results")
            observed = _wait_for_text(client, manager, "MANAGER_COLLECTED")
            assert addressed_reply in _tool_output(mock, observed, f"poll_{suffix}")
            assert addressed_reply in _tool_output(mock, observed, f"inbox_{suffix}")

            configure(
                worker_marker, [{"block": True, "text": "WORKER_CONTINUED_AFTER_DISCONNECT"}]
            )
            configure(
                manager_marker,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_delegate",
                                {
                                    "target_id": worker,
                                    "message": worker_marker + " continue this work",
                                },
                                f"continue_{suffix}",
                            )
                        ]
                    },
                    {"text": "MANAGER_SENT_CONTINUATION"},
                ],
            )
            send(manager, manager_marker + " continue the worker")
            observed = _wait_for_text(client, manager, "MANAGER_SENT_CONTINUATION")
            continuation = json.loads(_tool_output(mock, observed, f"continue_{suffix}"))
            assert continuation["delivery_state"] == "accepted", continuation
            deadline = time.monotonic() + 30
            while not httpx.get(f"{mock}/gate/pending", trust_env=False).json()["pending"]:
                assert time.monotonic() < deadline, "Worker never reached the blocked provider"
                time.sleep(0.1)
            client.delete(f"/v1/sessions/{manager}/links/{worker}").raise_for_status()
            release_mock_gate(mock)
            _wait_for_text(client, worker, "WORKER_CONTINUED_AFTER_DISCONNECT")
            configure(
                worker_marker,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_reply",
                                {
                                    "request_id": continuation["id"],
                                    "message": "LATE_REPLY_MUST_NOT_ARRIVE",
                                },
                                f"late_{suffix}",
                            )
                        ]
                    },
                    {"text": "WORKER_LATE_REPLY_REFUSED"},
                ],
            )
            send(worker, worker_marker + " try a late reply")
            observed = _wait_for_text(client, worker, "WORKER_LATE_REPLY_REFUSED")
            assert json.loads(_tool_output(mock, observed, f"late_{suffix}"))["status"] == 403
            assert "LATE_REPLY_MUST_NOT_ARRIVE" not in json.dumps(_items(client, manager))
            worker_items = _items(client, worker)
            configure(
                manager_marker,
                [
                    {
                        "tool_calls": [
                            tool(
                                "sys_session_delegate",
                                {"target_id": worker, "message": "MUST_NOT_ARRIVE"},
                                f"refused_{suffix}",
                            )
                        ]
                    },
                    {"text": "MANAGER_REFUSED"},
                ],
            )
            send(manager, manager_marker + " check disconnection")
            observed = _wait_for_text(client, manager, "MANAGER_REFUSED")
            assert json.loads(_tool_output(mock, observed, f"refused_{suffix}"))["status"] == 403
            assert _items(client, worker) == worker_items
            assert addressed_reply in json.dumps(observed)
            shared = client.get(
                f"/v1/sessions/{manager}/items", headers={"X-Forwarded-Email": viewer}
            )
            assert shared.status_code == 200 and addressed_reply in shared.text
        finally:
            release_mock_gate(mock)
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()


@pytest.mark.timeout(120)
@pytest.mark.parametrize("harness", ["openai-agents"])
@pytest.mark.parametrize("linked_runtime", ["delay-handoff"], indirect=True)
def test_disconnect_expires_a_delayed_real_handoff(linked_runtime, tmp_path, harness):
    """Real elapsed lease, real runner HTTP: a late payload remains display-only."""
    from omnigent.session_authority import DELIVERY_LEASE_SECONDS

    base, mock, runner = linked_runtime
    gate = tmp_path / "handoff"
    suffix = uuid4().hex[:8]
    prompt = f"DELAYED_MANAGER_{suffix}"
    work = f"DELAYED_WORK_{suffix}"
    with httpx.Client(base_url=base, trust_env=False, timeout=60) as client:
        sessions = []
        try:
            for name in ("Manager", "Worker"):
                created = post_session_bundle(
                    client.post, "/v1/sessions", _sdk_bundle(f"{name} {suffix}", mock)
                )
                created.raise_for_status()
                sessions.append(created.json()["session_id"])
                bind_session_runner(client.patch, base, sessions[-1], runner)
            manager, worker = sessions
            client.post(
                f"/v1/sessions/{manager}/links", json={"target_id": worker}
            ).raise_for_status()
            configure_mock_llm(
                mock,
                [
                    {
                        "tool_calls": [
                            {
                                "name": "sys_session_delegate",
                                "call_id": f"delay_{suffix}",
                                "arguments": json.dumps({"target_id": worker, "message": work}),
                            }
                        ]
                    },
                    {"text": "DELAY_SETTLED"},
                ],
                match=prompt,
                required_tools=["sys_session_links"],
            )
            configure_mock_llm(
                mock,
                [{"text": "LATE_WORK_EXECUTED"}],
                match=work,
                required_tools=["sys_session_links"],
            )
            client.post(
                f"/v1/sessions/{manager}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                },
            ).raise_for_status()
            deadline = time.monotonic() + 20
            while not (gate / "claimed").exists():
                assert time.monotonic() < deadline, "The real delivery never reached the barrier"
                time.sleep(0.05)
            started = time.monotonic()
            client.delete(f"/v1/sessions/{manager}/links/{worker}").raise_for_status()
            assert time.monotonic() - started >= DELIVERY_LEASE_SECONDS - 2
            (gate / "release").touch()
            manager_items = _wait_for_text(client, manager, "DELAY_SETTLED")
            receipt = json.loads(_tool_output(mock, manager_items, f"delay_{suffix}"))
            assert receipt["delivery_state"] == "uncertain", receipt
            worker_items = _items(client, worker)
            linked = [
                item for item in worker_items if item.get("linked_request_id") == receipt["id"]
            ]
            assert len(linked) == 1 and linked[0]["linked_input_accepted"] is False
            assert not any(item.get("role") == "assistant" for item in worker_items)
            user_inputs = [
                json.dumps(item.get("content", []))
                for request in get_mock_requests(mock)
                for item in request.get("input", [])
                if isinstance(item, dict) and item.get("role") == "user"
            ]
            assert any(prompt in content for content in user_inputs), user_inputs
            assert not any(work in content for content in user_inputs), user_inputs
        finally:
            (gate / "release").touch()
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()


@pytest.mark.timeout(240)
@pytest.mark.parametrize("harness", ["openai-agents"])
def test_chained_work_uses_collaborators_own_permissions(
    linked_runtime: tuple[str, str, str], harness: str
) -> None:
    base, mock, runner_id = linked_runtime
    suffix = uuid4().hex[:8]
    colleague = "colleague@example.test"
    with httpx.Client(base_url=base, trust_env=False, timeout=60) as client:
        sessions = []
        try:
            for name in ("Manager", "Worker", "Leaf"):
                response = post_session_bundle(
                    client.post, "/v1/sessions", _sdk_bundle(f"{name} {suffix}", mock)
                )
                response.raise_for_status()
                sessions.append(response.json()["session_id"])
                bind_session_runner(client.patch, base, sessions[-1], runner_id)
            manager, worker, leaf = sessions
            for source, target in ((manager, worker), (worker, leaf)):
                client.post(
                    f"/v1/sessions/{source}/links", json={"target_id": target}
                ).raise_for_status()
                client.put(
                    f"/v1/sessions/{source}/permissions", json={"user_id": colleague, "level": 2}
                ).raise_for_status()
            for permitted in (False, True):
                if permitted:
                    client.put(
                        f"/v1/sessions/{leaf}/permissions", json={"user_id": colleague, "level": 2}
                    ).raise_for_status()
                chain_marker = f"CHAIN_{suffix}"
                marker = f"{chain_marker}_{permitted}"
                call_id = f"root_{marker}"
                hop_id = f"hop_{marker}"
                for session_marker, target, request_marker, tool_id, done in (
                    (
                        f"ROOT_INPUT_{chain_marker}",
                        worker,
                        f"WORKER_INPUT_{chain_marker}",
                        call_id,
                        f"ROOT_SENT_{marker}",
                    ),
                    (
                        f"WORKER_INPUT_{chain_marker}",
                        leaf,
                        f"LEAF_INPUT_{chain_marker}",
                        hop_id,
                        f"HOP_SENT_{marker}",
                    ),
                ):
                    configure_mock_llm(
                        mock,
                        [
                            {
                                "tool_calls": [
                                    {
                                        "name": "sys_session_delegate",
                                        "call_id": tool_id,
                                        "arguments": json.dumps(
                                            {"target_id": target, "message": request_marker}
                                        ),
                                    }
                                ]
                            },
                            {"text": done},
                        ],
                        match=session_marker,
                        required_tools=["sys_session_links"],
                    )
                configure_mock_llm(
                    mock,
                    [{"text": f"LEAF_FINISHED_{marker}"}],
                    match=f"LEAF_INPUT_{chain_marker}",
                    required_tools=["sys_session_links"],
                )
                sent = client.post(
                    f"/v1/sessions/{manager}/events",
                    headers={"X-Forwarded-Email": colleague},
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": f"ROOT_INPUT_{chain_marker} {permitted}",
                                }
                            ],
                        },
                    },
                )
                assert sent.status_code == 202, sent.text
                root = _wait_for_text(client, manager, f"ROOT_SENT_{marker}")
                receipt = json.loads(_tool_output(mock, root, call_id))
                assert receipt["delivery_state"] == "accepted", receipt
                worker_items = _wait_for_text(client, worker, f"HOP_SENT_{marker}")
                hop = json.loads(_tool_output(mock, worker_items, hop_id))
                assert f"on behalf of {colleague}" in json.dumps(worker_items)
                if permitted:
                    assert hop["delivery_state"] == "accepted", hop
                    leaf_items = _wait_for_text(client, leaf, f"LEAF_FINISHED_{marker}")
                    assert f"on behalf of {colleague}" in json.dumps(leaf_items)
                else:
                    assert hop["status"] == 403, hop
                    assert not any(item.get("type") == "message" for item in _items(client, leaf))
        finally:
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()


@pytest.mark.timeout(240)
@pytest.mark.parametrize("harness", ["openai-agents", "codex-native"])
@pytest.mark.parametrize("mixed_requesters", [False, True], ids=["same-person", "two-people"])
def test_busy_worker_preserves_consumed_requesters(
    linked_runtime, tmp_path, harness, mixed_requesters
):
    base, mock, runner = linked_runtime
    suffix = uuid4().hex[:8]
    owner, colleague = "local", "colleague@example.test"
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
            required_tools=["mcp__omnigent" if native else "sys_session_links"],
        )

    with httpx.Client(base_url=base, trust_env=False, timeout=60) as client:
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
            for source, target in ((manager, worker), (worker, leaf)):
                client.post(
                    f"/v1/sessions/{source}/links", json={"target_id": target}
                ).raise_for_status()
            worker_script = [
                {"block": True, "tool_calls": [tool("sys_read_inbox", {}, f"busy_{suffix}")]},
                {
                    "tool_calls": [
                        tool(
                            "sys_session_delegate",
                            {"target_id": leaf, "message": f"LEAF_INPUT_{suffix} current turn"},
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
                                    "sys_session_delegate",
                                    {
                                        "target_id": leaf,
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
                                "sys_session_delegate",
                                {"target_id": worker, "message": steering},
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
                    headers={"X-Forwarded-Email": user} if user != owner else {},
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
            send(manager, root, sender)
            manager_items = _wait_for_text(client, manager, "MANAGER_STEERED")
            receipt = json.loads(_tool_output(mock, manager_items, f"steer_{suffix}"))
            assert receipt["delivery_state"] == "accepted", receipt
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
                assert result["delivery_state"] == "accepted", result
                leaf_items = _wait_for_text(client, leaf, "LEAF_BUSY_REACHED")
                if not native:
                    queued = json.loads(_tool_output(mock, worker_items, f"queued_{suffix}"))
                    if mixed_requesters:
                        assert queued["status"] == 403, queued
                    else:
                        assert queued["delivery_state"] == "accepted", queued
                        leaf_items = _wait_for_text(client, leaf, "LEAF_QUEUED_REACHED")
                leaf_inputs = [item for item in leaf_items if item.get("role") == "user"]
                assert len(leaf_inputs) == (1 if native or mixed_requesters else 2)
                assert all(item["created_by"] == owner for item in leaf_inputs)
        finally:
            release_mock_gate(mock)
            for session_id in sessions:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()


@pytest.mark.timeout(180)
def test_durable_poll_after_server_and_runner_restart(isolated_mock_llm_server_url, tmp_path):
    """Own both processes: polling survives restart and an offline worker is not launched."""
    mock = isolated_mock_llm_server_url
    suffix = uuid4().hex[:8]
    manager_marker, worker_marker = f"RESTART_MANAGER_{suffix}", f"RESTART_WORKER_{suffix}"
    with _linked_stack(tmp_path, mock) as (stack, env):
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=60) as client:
            sessions = []

            def send(session_id, text):
                client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    },
                ).raise_for_status()

            def tool(name, arguments, call_id):
                return {"name": name, "arguments": json.dumps(arguments), "call_id": call_id}

            def configure(marker, responses):
                configure_mock_llm(
                    mock, responses, match=marker, required_tools=["sys_session_links"]
                )

            try:
                for name in ("Manager", "Worker", "Offline"):
                    created = post_session_bundle(
                        client.post, "/v1/sessions", _sdk_bundle(f"{name} {suffix}", mock)
                    )
                    created.raise_for_status()
                    sessions.append(created.json()["session_id"])
                    if name != "Offline":
                        bind_session_runner(
                            client.patch, stack.base_url, sessions[-1], stack.runner_id
                        )
                manager, worker, offline = sessions
                for target in (worker, offline):
                    client.post(
                        f"/v1/sessions/{manager}/links", json={"target_id": target}
                    ).raise_for_status()
                configure(worker_marker, [{"text": "WORKER_RECEIVED"}])
                configure(
                    manager_marker,
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_delegate",
                                    {"target_id": worker, "message": worker_marker},
                                    "before_restart",
                                )
                            ]
                        },
                        {"text": "MANAGER_SENT"},
                    ],
                )
                send(manager, manager_marker)
                observed = _wait_for_text(client, manager, "MANAGER_SENT")
                receipt = json.loads(_tool_output(mock, observed, "before_restart"))
                assert receipt["delivery_state"] == "accepted", receipt
                _wait_for_text(client, worker, "WORKER_RECEIVED")
                copied_reply = f"DURABLE_REPLY_{suffix}"
                configure(
                    worker_marker,
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_reply",
                                    {"request_id": receipt["id"], "message": copied_reply},
                                    "reply_before_restart",
                                )
                            ]
                        },
                        {"text": "WORKER_REPLIED"},
                    ],
                )
                send(worker, worker_marker + " reply")
                _wait_for_text(client, worker, "WORKER_REPLIED")
                _wait_for_text(client, manager, copied_reply)
                worker_inputs = [
                    item for item in _items(client, worker) if item.get("role") == "user"
                ]
                old_runner = stack.runner
                terminate_process(old_runner)
                stack.runner = None
                stack.restart_server()
                stack.start_runner(env=env, cwd=_REPO)
                assert old_runner is not None and old_runner.poll() is not None
                assert stack.runner is not None and stack.runner.pid != old_runner.pid
                configure(
                    manager_marker,
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_task",
                                    {"request_id": receipt["id"]},
                                    "poll_after_restart",
                                ),
                                tool("sys_read_inbox", {}, "inbox_after_restart"),
                                tool(
                                    "sys_session_delegate",
                                    {"target_id": offline, "message": "OFFLINE_WORK"},
                                    "offline_request",
                                ),
                            ]
                        },
                        {"text": "MANAGER_RECOVERED"},
                    ],
                )
                send(manager, manager_marker + " poll after restart")
                observed = _wait_for_text(client, manager, "MANAGER_RECOVERED")
                poll = json.loads(_tool_output(mock, observed, "poll_after_restart"))
                assert poll["delivery_state"] == "accepted", poll
                assert [reply["message"] for reply in poll["replies"]] == [copied_reply]
                assert copied_reply not in _tool_output(mock, observed, "inbox_after_restart")
                offline_receipt = json.loads(_tool_output(mock, observed, "offline_request"))
                assert offline_receipt["delivery_state"] == "rejected", offline_receipt
                assert not any(item.get("role") == "user" for item in _items(client, offline))
                assert [
                    item for item in _items(client, worker) if item.get("role") == "user"
                ] == worker_inputs
                assert len(client.get(f"/v1/sessions/{manager}/links").json()["data"]) == 2
            finally:
                for session_id in sessions:
                    client.delete(f"/v1/sessions/{session_id}").raise_for_status()
