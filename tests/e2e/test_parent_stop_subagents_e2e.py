"""Stop/Archive a real native parent while its native sub-agent is working.

The server, host daemon, host-launched runner, native CLIs, MCP dispatch and
WebSocket/SSE transports are real. Only the model endpoint is scripted: it
dispatches a child through the parent's tool and holds the child's response
in flight. No session status, stop marker, disconnect or failure is injected.

Run: uv run --no-sync pytest tests/e2e/test_parent_stop_subagents_e2e.py -v -s
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest
import yaml

from dev.repro_env.runtime import write_model_config
from omnigent.host.shutdown import ShutdownIntent
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.server.routes.sessions import RUNNER_DISCONNECT_GRACE_S
from omnigent.server.shutdown_attribution import SessionScope, SessionShutdown
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_MODELS = {"claude": "claude-sonnet-4-20250514", "codex": "gpt-4o"}
pytestmark = pytest.mark.timeout(360)


def _wait(check, description: str, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _read_shutdown_events(path: Path) -> list[dict]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    # A concurrent append may end inside a JSON value or UTF-8 character.
    return [json.loads(line) for line in data.split(b"\n")[:-1]]


def _send_message(client: httpx.Client, session_id: str, text: str) -> None:
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    assert response.status_code == 202, response.text


def _wait_for_native_tools(
    client: httpx.Client, session_id: str, mock_url: str, tool_name: str
) -> None:
    # Native CLIs may accept input before their MCP connection finishes discovery.
    probe = "NATIVE_TOOL_READY_PROBE: acknowledge readiness."
    _send_message(client, session_id, probe)
    seen_requests = 0

    def ready():
        nonlocal seen_requests
        snapshot = _get(client, f"/v1/sessions/{session_id}")
        assert not snapshot.get("last_task_error"), snapshot["last_task_error"]
        if snapshot["status"] != "idle" or snapshot.get("pending_inputs"):
            return False
        requests = [r for r in get_mock_requests(mock_url) if probe in json.dumps(r)]
        if any(t.get("name") == tool_name for r in requests for t in r.get("tools", [])):
            return True
        if len(requests) > seen_requests:
            seen_requests = len(requests)
            _send_message(client, session_id, probe)
        return False

    _wait(ready, "native CLI to advertise its real MCP tools")


class _Stream:
    def __init__(self, base_url: str, session_id: str) -> None:
        self.events: list[dict] = []
        self.ready = threading.Event()
        self.done = threading.Event()
        self.error: Exception | None = None
        self.url = f"{base_url}/v1/sessions/{session_id}/stream"
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self) -> None:
        try:
            with httpx.Client(trust_env=False, timeout=httpx.Timeout(30, read=30)) as client:
                with client.stream("GET", self.url) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if self.done.is_set():
                            return
                        if line.startswith("data:"):
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                return
                            event = json.loads(payload)
                            self.events.append(event)
                            self.ready.set()
        except httpx.ReadTimeout as exc:
            if not self.done.is_set():
                self.error = exc
        except Exception as exc:
            self.error = exc

    def check_error(self) -> None:
        if self.error is not None:
            raise AssertionError("Session SSE reader failed") from self.error

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(20):
            self.__exit__(None, None, None)
            raise AssertionError("Stream never became ready")
        return self

    def __exit__(self, exc_type, *_args):
        self.done.set()
        self.thread.join(timeout=35)
        if exc_type is None:
            assert not self.thread.is_alive(), "SSE reader did not exit within its read timeout"
            self.check_error()


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("action", ["stop", "archive", "crash"])
def test_native_parent_teardown_preserves_child_outcome(
    tmp_path: Path, isolated_mock_llm_server_url: str, harness: str, action: str
) -> None:
    for binary in (harness, "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")
    if harness == "claude" and any(p.is_file() for p in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip(
            "machine-managed Claude settings override mock auth; use an isolated container"
        )
    mock_url = isolated_mock_llm_server_url
    config = tmp_path / "config"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    write_model_config(config, mock_url, _MODELS["claude"], _MODELS["codex"])
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "CODEX_HOME": str(tmp_path / "codex-config"),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(tmp_path / "codex-state"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_SKIP_WEB_UI": "true",
    }
    dispatch_prompt = "PARENT_TEARDOWN_PROBE: delegate the research to the worker."
    child_prompt = "CHILD_TEARDOWN_PROBE: research the requested topic."
    tool_guard = "Bash" if harness == "claude" else "mcp__omnigent"
    dispatch_guard = "mcp__omnigent__sys_session_send" if harness == "claude" else tool_guard
    httpx.post(
        f"{mock_url}/mock/configure",
        trust_env=False,
        json={
            "required_tools": [dispatch_guard],
            "key": "parent",
            "match": dispatch_prompt,
            "responses": [
                {
                    "tool_calls": [
                        {
                            "call_id": "dispatch_worker",
                            "name": "mcp__omnigent__sys_session_send"
                            if harness == "claude"
                            else "sys_session_send",
                            **({"namespace": "mcp__omnigent"} if harness == "codex" else {}),
                            "arguments": json.dumps(
                                {"agent": "worker", "title": "research", "args": child_prompt}
                            ),
                        }
                    ]
                },
                {"text": "Worker dispatched."},
            ],
        },
    ).raise_for_status()
    httpx.post(
        f"{mock_url}/mock/configure",
        trust_env=False,
        json={
            "required_tools": [tool_guard],
            "key": "child",
            "match": child_prompt,
            "responses": [{"text": "CHILD_FINISHED", "block": True}] * 5,
        },
    ).raise_for_status()
    set_fallback_mock_llm(mock_url, key="default", text="Acknowledged.")
    set_fallback_mock_llm(
        mock_url,
        key="codex-auto-review",
        text='{"risk_level":"low","user_authorization":"high","outcome":"allow",'
        '"rationale":"The user requested this test worker in the isolated workspace."}',
    )
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')
    with ExitStack() as resources:
        stack = resources.enter_context(
            server_runner(
                tmp_path,
                server_bootstrap="from tests.e2e._shutdown_evidence_server import main; main()",
                server_cwd=_REPO,
                base_env=base_env,
                server_env={
                    **env,
                    "OMNIGENT_RUNNER_TUNNEL_TOKEN": None,
                    "OMNIGENT_E2E_SHUTDOWN_EVIDENCE": str(tmp_path / "shutdown-events.jsonl"),
                },
                workspace=workspace,
                poll_interval=0.2,
            )
        )
        # The host itself launches the dedicated runner via the production API.
        stack.start_host(env=env, cwd=_REPO)
        client = resources.enter_context(
            httpx.Client(
                base_url=stack.base_url,
                trust_env=False,
                timeout=60,
                headers={
                    "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                    "x-omnigent-background-session-titles": "off",
                },
            )
        )

        def online_host():
            assert stack.host is not None and stack.host.poll() is None, stack.log_tail()
            return next(
                (h for h in _get(client, "/v1/hosts")["hosts"] if h["status"] == "online"),
                None,
            )

        host = _wait(online_host, "real host registration")
        store = SqlAlchemyConversationStore(stack.database_uri)
        spec = {
            "name": "native-teardown-parent",
            "prompt": "Delegate research to the worker using sys_session_send.",
            "executor": {"harness": f"{harness}-native", "model": _MODELS[harness], "yolo": True},
            "spawn": True,
            "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
            "tools": {
                "worker": {
                    "type": "agent",
                    "description": "Research worker",
                    "executor": {
                        "harness": f"{harness}-native",
                        "model": _MODELS[harness],
                        "yolo": True,
                    },
                    "prompt": "Research the requested topic.",
                }
            },
        }
        response = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"parent.yaml": yaml.safe_dump(spec).encode()}),
            metadata={"host_id": host["host_id"], "workspace": str(workspace)},
        )
        response.raise_for_status()
        parent_id = response.json()["session_id"]
        _wait_for_native_tools(client, parent_id, mock_url, dispatch_guard)
        _send_message(client, parent_id, dispatch_prompt)

        def dispatched_child():
            snapshot = _get(client, f"/v1/sessions/{parent_id}")
            assert not snapshot.get("last_task_error"), snapshot["last_task_error"]
            for elicitation in snapshot.get("pending_elicitations", []):
                assert elicitation["params"].get("tool_name", "").endswith("sys_session_send"), (
                    elicitation
                )
                client.post(
                    f"/v1/sessions/{parent_id}/elicitations/{elicitation['elicitation_id']}/resolve",
                    json={"action": "accept"},
                ).raise_for_status()
            return next(
                iter(_get(client, f"/v1/sessions/{parent_id}/child_sessions")["data"]), None
            )

        child = _wait(dispatched_child, "native parent to dispatch its sub-agent")
        child_id = child["id"]
        _wait(
            lambda: (
                any(child_prompt in json.dumps(req) for req in get_mock_requests(mock_url))
                and httpx.get(f"{mock_url}/gate/pending", timeout=5, trust_env=False).json()[
                    "pending"
                ]
            ),
            "native child's real model request to block",
        )

        def child_running():
            snapshot = _get(client, f"/v1/sessions/{child_id}")
            assert not snapshot.get("last_task_error"), snapshot["last_task_error"]
            if (
                snapshot.get("external_session_id")
                and not snapshot.get("pending_inputs")
                and snapshot["status"] == "running"
            ):
                return snapshot
            return None

        before = _wait(
            child_running, "child's native turn to start beyond terminal initialization"
        )
        parent = _get(client, f"/v1/sessions/{parent_id}")
        assert before["runner_id"] == parent["runner_id"], (parent, before)
        assert before["parent_session_id"] == parent_id, before
        assert before["harness"] == f"{harness}-native", before
        assert before["external_session_id"] != parent["external_session_id"], (parent, before)
        with _Stream(stack.base_url, child_id) as stream:
            stopped_at = time.monotonic()
            requested_at_ms = time.time_ns() // 1_000_000
            if action == "crash":
                launched = re.search(
                    rf"Runner started: {re.escape(parent['runner_id'])} \(pid=(\d+)\)",
                    stack.log_path("host").read_text(),
                )
                assert launched is not None, "host must identify the runner this test owns"
                os.kill(int(launched.group(1)), signal.SIGKILL)
            elif action == "stop":
                response = client.post(
                    f"/v1/sessions/{parent_id}/events", json={"type": "stop_session"}
                )
                response.raise_for_status()
            else:
                response = client.patch(f"/v1/sessions/{parent_id}", json={"archived": True})
                response.raise_for_status()
            _wait(
                lambda: not _get(client, f"/v1/runners/{parent['runner_id']}/status")["online"],
                "host to terminate the parent's real runner",
                timeout=40,
            )
            disconnected_at = time.monotonic()
            decisions = {
                "intentional_stop",
                "failed_mid_turn",
                "idle_no_failure",
                "superseded_lifecycle",
            }

            def disconnect_decided():
                return any(
                    row["event_name"] == "runner_stream_disconnected"
                    and row["session_id"] == child_id
                    and row["attributes"]["runner_id"] == parent["runner_id"]
                    and row["attributes"]["decision"] in decisions
                    and row["at_ms"] >= requested_at_ms
                    for row in _read_shutdown_events(tmp_path / "shutdown-events.jsonl")
                )

            _wait(
                disconnect_decided,
                "child's disconnect decision after the production reconnect grace",
                timeout=RUNNER_DISCONNECT_GRACE_S + 20,
            )
            # Keep collecting through the full offline grace, including late failures.
            time.sleep(
                max(0, RUNNER_DISCONNECT_GRACE_S + 5 - (time.monotonic() - disconnected_at))
            )
        events = list(stream.events)
        shutdown_states = {
            session_id: store.get_shutdown_state(session_id)
            for session_id in (parent_id, child_id)
        }
        shutdown_events = _read_shutdown_events(tmp_path / "shutdown-events.jsonl")
        evidence = {
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=_REPO, text=True
            ).strip(),
            "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "harness_version": subprocess.check_output([harness, "--version"], text=True).strip(),
            "harness": harness,
            "action": action,
            "parent_id": parent_id,
            "child_id": child_id,
            "runner_id": parent["runner_id"],
            "before": before,
            "events": events,
            "shutdown_states": shutdown_states,
            "shutdown_events": shutdown_events,
            "labels_after": _get(client, f"/v1/sessions/{child_id}/labels"),
            "elapsed_s": time.monotonic() - stopped_at,
            "offline_elapsed_s": time.monotonic() - disconnected_at,
        }
        (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2))
        failures = [
            e for e in events if e.get("status") == "failed" or e.get("type") == "response.failed"
        ]
        print(
            json.dumps(
                {
                    "harness": harness,
                    "action": action,
                    "before_status": before["status"],
                    "native_session_id": before["external_session_id"],
                    "failures": failures,
                    "evidence": str(tmp_path / "evidence.json"),
                }
            ),
            flush=True,
        )
        if action == "crash":
            assert failures, "An unexpected runner death must still fail an active child"
        else:
            assert not failures, f"Intentional {action} failed the child: {failures}"
        after = _get(client, f"/v1/sessions/{child_id}")
        if action == "crash":
            assert after["status"] == "failed" and after["last_task_error"], after
            for state in shutdown_states.values():
                assert state is not None and not state.get("intent"), state
            assert shutdown_states[child_id]["live_status"] == "failed", shutdown_states
        else:
            assert after["status"] == "idle" and after["last_task_error"] is None, after
            shutdowns = []
            for session_id, state in shutdown_states.items():
                assert state is not None and state["live_status"] == "idle", state
                rows = [row for row in shutdown_events if row["session_id"] == session_id]
                applied = [
                    row
                    for row in rows
                    if row["event_name"] == "session_shutdown_applied"
                    and row["attributes"]["runner_id"] == parent["runner_id"]
                    and row["attributes"]["requested_at_ms"] >= requested_at_ms
                ]
                assert applied, f"No durable applied evidence for {session_id}: {rows}"
                attrs = applied[0]["attributes"]
                shutdown = SessionShutdown(
                    intent=ShutdownIntent.model_validate(
                        {key: attrs[key] for key in ShutdownIntent.model_fields}
                    ),
                    scope=SessionScope.model_validate(
                        {key: attrs[key] for key in SessionScope.model_fields}
                    ),
                    recorded_at_ms=attrs["shutdown_recorded_at_ms"],
                    preserve_failure=attrs["preserve_failure"],
                )
                assert any(
                    row["event_name"] == "session_shutdown_requested"
                    and row["attributes"]["eligible"] is True
                    and row["attributes"]["shutdown_id"] == shutdown.intent.shutdown_id
                    and row["attributes"]["lifecycle_id"] == shutdown.scope.lifecycle_id
                    and row["at_ms"] <= applied[0]["at_ms"]
                    for row in rows
                ), rows
                current = SessionScope.model_validate_json(state["scope"])
                if current == shutdown.scope:
                    assert state["settled"] is True, state
                    assert SessionShutdown.model_validate_json(state["intent"]) == shutdown
                else:
                    # A child's completion wake may start a real new parent runner.
                    # The original evidence survives; its active marker must not.
                    assert state["intent"] is None and not state["settled"], state
                    assert state["observed_shutdowns"][shutdown.intent.shutdown_id]["scope"] == (
                        hashlib.sha256(shutdown.scope.model_dump_json().encode()).hexdigest()
                    )
                    assert any(
                        row["event_name"] == "session_lifecycle_started"
                        and row["attributes"] == current.model_dump()
                        and row["at_ms"] >= applied[0]["at_ms"]
                        for row in rows
                    ), rows
                    if current.runner_id != shutdown.scope.runner_id:
                        assert session_id == parent_id
                        assert any(
                            row["event_name"] == "shutdown_test_message_received"
                            and row["at_ms"] >= requested_at_ms
                            and "[System: sub-agent" in json.dumps(row["attributes"]["payload"])
                            for row in rows
                        ), rows
                assert shutdown.scope.runner_id == parent["runner_id"]
                assert shutdown.intent.requested and not shutdown.preserve_failure, state
                assert shutdown.intent.reason == "user_stopped_session", state
                assert shutdown.intent.action == (
                    "stop_session" if action == "stop" else "archive"
                )
                assert shutdown.intent.host_id == host["host_id"], state
                assert (
                    requested_at_ms <= shutdown.intent.requested_at_ms <= shutdown.recorded_at_ms
                )
                shutdowns.append(shutdown)
            assert shutdowns[0].intent.shutdown_id == shutdowns[1].intent.shutdown_id
            assert (
                shutdowns[0].scope.runner_connection_id == shutdowns[1].scope.runner_connection_id
            )
            assert shutdowns[0].scope.lifecycle_id != shutdowns[1].scope.lifecycle_id
