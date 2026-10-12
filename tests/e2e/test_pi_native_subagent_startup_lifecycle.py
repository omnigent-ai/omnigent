"""Real Pi startup, steering, and the sub-agent launch watchdog.

The test runs the installed Pi CLI in the runner's real tmux terminal. Its
first model response invokes Pi's built-in bash tool, which runs a live
heartbeat process until released. Only model responses are scripted. The
server delays the real startup metadata HTTP response to expose the race;
no Pi callbacks, busy flags, status events, or work entries are manufactured.

A startup idle would complete the initial dispatch while bash is still
running. A public MCP follow-up then becomes a new launching dispatch and
the watchdog fails it although the same Pi/tool processes remain alive.
The watchdog budget is shortened through its environment knob so one
reaper sweep fits the test; the production default only changes the wait.
Assertions run after that window and actual completion, so a failing run
captures the entire causal chain rather than stopping at the first idle.
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    find_free_port,
    get_mock_requests,
    register_inline_agent,
    send_user_message_to_session,
    set_fallback_mock_llm,
)

_ROOT = Path(__file__).resolve().parents[2]
_CHILD_MODEL = "pi-startup-child"
_PARENT_MODEL = "pi-startup-parent"
_FOLLOWUP = "PI_STARTUP_FOLLOWUP: report the heartbeat result when it finishes."
# Launch watchdog budget; the runner reaper sweeps every 30 s, so the observed
# window (budget + 35 s) always spans at least one full sweep.
_WATCHDOG_SECONDS = 10

pytestmark = pytest.mark.timeout(300, method="signal")

_OBSERVER = """\
const fs = require('fs');
const path = require('path');
module.exports = function(pi) {
  for (const event of ['session_start', 'agent_start', 'agent_end']) {
    pi.on(event, () => {
      fs.appendFileSync(path.join(process.cwd(), 'pi-events.jsonl'),
        JSON.stringify({event, pid: process.pid, time: Date.now() / 1000}) + '\\n');
    });
  }
};
"""

_WORK = """\
import json
import os
import time
from pathlib import Path

root = Path(__file__).parent
(root / 'tool-started.json').write_text(json.dumps({'pid': os.getpid()}))
while not (root / 'release-tool').exists():
    (root / 'heartbeat.tmp').write_text(str(time.time()))
    (root / 'heartbeat.tmp').replace(root / 'heartbeat')
    print(f'PI_HEARTBEAT {time.time()}', flush=True)
    time.sleep(0.2)
(root / 'tool-finished').write_text(str(time.time()))
print('PI_HEARTBEAT_TOOL_FINISHED', flush=True)
"""


def _wait(predicate: Callable[[], Any], description: str, *, timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # A concurrent writer may not have finished its last line.
    return records


def _mcp(client: httpx.Client, parent: str, name: str, arguments: dict[str, Any]) -> str:
    response = client.post(
        f"/v1/sessions/{parent}/mcp",
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        json={
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        timeout=90,
    )
    response.raise_for_status()
    body = response.json()
    assert "error" not in body, body
    result = body["result"]
    assert not result.get("isError"), result
    text = "\n".join(block["text"] for block in result["content"] if block["type"] == "text")
    assert not text.startswith("Error:"), text
    return text


@dataclass
class PiRig:
    client: httpx.Client
    runner_id: str
    workspace: Path
    evidence: Path
    version: str


@pytest.fixture
def pi_startup_rig(tmp_path: Path) -> Iterator[PiRig]:
    """Start isolated real server/runner processes and the installed Pi binary."""
    pi_binary = os.environ.get("OMNIGENT_PI_PATH") or shutil.which("pi")
    if not pi_binary:
        pytest.skip("The real Pi CLI is required")
    for binary in (pi_binary, "node"):
        if reason := cli_unavailable_reason(binary):
            pytest.skip(reason)
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real pi-native terminal")
    version = subprocess.check_output(
        [pi_binary, "--version"], text=True, stderr=subprocess.STDOUT, timeout=15
    ).strip()
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    isolated_home = tmp_path / "home"
    config = isolated_home / ".omnigent"
    extensions = isolated_home / ".pi" / "agent" / "extensions"
    for directory in (workspace, evidence, config, extensions):
        directory.mkdir(parents=True, exist_ok=True)
    (workspace / "work.py").write_text(_WORK)
    (extensions / "lifecycle-observer.js").write_text(_OBSERVER)
    (extensions.parent / "settings.json").write_text(
        json.dumps({"extensions": [str(extensions / "lifecycle-observer.js")]})
    )

    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(token)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("OMNIGENT_", "RUNNER_", "DATABRICKS_", "PI_", "OPENAI_", "ANTHROPIC_")
        )
        and key not in ("TMUX", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
    env.update(
        {
            "HOME": str(isolated_home),
            "OMNIGENT_CONFIG_HOME": str(config),
            "OMNIGENT_DATA_DIR": str(config),
            "OMNIGENT_PI_PATH": pi_binary,
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "OMNIGENT_SKIP_WEB_UI": "1",
            "OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S": str(_WATCHDOG_SECONDS),
            "PI_STARTUP_TEST_EVIDENCE_DIR": str(evidence),
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "TERM": "xterm-256color",
            "NO_PROXY": "localhost,127.0.0.1",
            "PYTHONPATH": os.pathsep.join(
                str(path) for path in (_ROOT, _ROOT / "sdks/python-client", _ROOT / "sdks/ui")
            ),
        }
    )
    # The test writes the provider before the child is launched.
    processes = []
    with (
        (evidence / "server.log").open("w") as server_log,
        (evidence / "runner.log").open("w") as runner_log,
        httpx.Client(base_url=base_url, trust_env=False, timeout=15) as client,
    ):
        try:
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "tests.e2e._pi_subagent_startup_server",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--database-uri",
                        f"sqlite:///{tmp_path}/server.db",
                        "--artifact-location",
                        str(tmp_path / "artifacts"),
                    ],
                    env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": token},
                    cwd=_ROOT,
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                )
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "omnigent.runner._entry"],
                    env={
                        **env,
                        "OMNIGENT_RUNNER_ID": runner_id,
                        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                        "RUNNER_SERVER_URL": base_url,
                    },
                    cwd=_ROOT,
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                )
            )

            def online() -> bool:
                assert all(process.poll() is None for process in processes), (
                    f"Test stack exited; inspect {evidence}"
                )
                try:
                    response = client.get(f"/v1/runners/{runner_id}/status", timeout=2)
                    return response.status_code == 200 and response.json().get("online") is True
                except httpx.HTTPError:
                    return False

            _wait(online, "server and runner to connect")
            yield PiRig(client, runner_id, workspace, evidence, version)
        finally:
            (evidence / "release-startup").touch()
            (workspace / "release-tool").touch()
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in reversed(processes):
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def test_real_pi_child_startup_cannot_complete_or_timeout_running_work(
    pi_startup_rig: PiRig, isolated_mock_llm_server_url: str
) -> None:
    rig = pi_startup_rig
    client, workspace, evidence = rig.client, rig.workspace, rig.evidence
    model_url = isolated_mock_llm_server_url
    config_path = workspace.parent / "home/.omnigent/config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "test": {
                        "kind": "gateway",
                        "default": ["pi"],
                        "openai": {
                            "base_url": f"{model_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "chat",
                            "models": {"default": _CHILD_MODEL},
                        },
                    }
                }
            }
        )
    )
    set_fallback_mock_llm(model_url, _PARENT_MODEL, "Ready; waiting for the worker.")
    configure_mock_llm(
        model_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "heartbeat_call",
                        "name": "bash",
                        "arguments": json.dumps(
                            {
                                "command": shlex.join(
                                    [sys.executable, str(workspace / "work.py")]
                                ),
                                "timeout": 360,
                            }
                        ),
                    }
                ]
            },
            {"text": "PI_HEARTBEAT_TOOL_FINISHED; PI_STARTUP_FOLLOWUP acknowledged."},
        ],
        key=_CHILD_MODEL,
    )
    parent_name = register_inline_agent(
        client,
        name="pi-startup-parent",
        harness="openai-agents",
        model=_PARENT_MODEL,
        profile="",
        prompt="Reply briefly; wait for the worker.",
        mock_llm_base_url=f"{model_url}/v1",
        extra_config={
            "os_env": {"type": "caller_process", "cwd": str(workspace)},
            "tools": {
                "worker": {
                    "type": "agent",
                    "description": "Pi lifecycle worker.",
                    "executor": {"harness": "pi-native", "model": _CHILD_MODEL},
                    "prompt": "Run the requested task.",
                    "os_env": {
                        "type": "caller_process",
                        "cwd": str(workspace),
                        "sandbox": {"type": "none"},
                    },
                }
            },
        },
    )
    parent = create_runner_bound_session(client, agent_name=parent_name, runner_id=rig.runner_id)
    send_user_message_to_session(client, session_id=parent, content="Initialize, then wait.")
    _wait(
        lambda: (
            "Ready; waiting for the worker."
            in json.dumps(client.get(f"/v1/sessions/{parent}").json().get("items"))
        ),
        "parent initialization",
    )
    child: str | None = None

    def session_items(session_id: str) -> list[dict[str, Any]]:
        response = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
        response.raise_for_status()
        return [{**item, **item.get("data", {})} for item in response.json()["data"]]

    def item_text(item: dict[str, Any]) -> str:
        if item.get("type") == "function_call_output":
            output = item.get("output")
            return output if isinstance(output, str) else ""
        return "\n".join(
            block["text"]
            for block in item.get("content", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )

    def terminal_notices(text: str) -> list[dict[str, str]]:
        notices = []
        for status in ("completed", "failed", "cancelled"):
            for source, prefix in (
                ("inbox_result", f"[System: sub-agent task {child} {status} — "),
                (
                    "wake_notice",
                    f"[System: sub-agent worker/heartbeat finished ({status}) — ",
                ),
            ):
                if prefix in text:
                    notices.append({"source": source, "status": status})
        return notices

    def item_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **{
                    key: item[key]
                    for key in ("id", "type", "role", "call_id", "name")
                    if key in item
                },
                "terminal_notices": terminal_notices(item_text(item)),
                "contains_tool_finished_marker": "PI_HEARTBEAT_TOOL_FINISHED" in item_text(item),
            }
            for item in items
        ]

    def has_status(texts: list[str], status: str) -> bool:
        return any(
            notice["status"] == status for text in texts for notice in terminal_notices(text)
        )

    try:
        initial = json.loads(
            _mcp(
                client,
                parent,
                "sys_session_send",
                {
                    "agent": "worker",
                    "title": "heartbeat",
                    "args": "Run the heartbeat task until released.",
                },
            )
        )
        child = initial["conversation_id"]
        ledger = evidence / "http-events.jsonl"
        _wait(
            lambda: any(
                row["event"] == "startup_patch_pending" and row["session_id"] == child
                for row in _records(ledger)
            ),
            "real Pi startup HTTP request",
        )
        _wait(lambda: (workspace / "tool-started.json").exists(), "Pi's real bash tool to start")
        pi_events = workspace / "pi-events.jsonl"
        started = _wait(
            lambda: [row for row in _records(pi_events) if row["event"] == "agent_start"],
            "actual Pi agent_start",
        )
        pi_process = psutil.Process(started[0]["pid"])
        tool_process = psutil.Process(
            json.loads((workspace / "tool-started.json").read_text())["pid"]
        )
        assert pi_process.pid in {process.pid for process in tool_process.parents()}, (
            "The heartbeat tool was not launched by the observed real Pi process"
        )
        running = [
            row
            for row in _records(ledger)
            if row["event"] == "external_session_status"
            and row.get("status") == "running"
            and row["session_id"] == child
        ]
        assert len(running) == 1, running
        assert not any(row["event"] == "agent_end" for row in _records(pi_events))

        (evidence / "release-startup").touch()
        _wait(
            lambda: any(
                row["event"] == "external_model_change" and row["session_id"] == child
                for row in _records(ledger)
            ),
            "startup model publication after HTTP release",
        )
        _wait(
            lambda: any(row["event"] == "session_start" for row in _records(pi_events)),
            "actual Pi startup handler completion",
        )

        def pane_is_running() -> bool:
            response = client.get(
                f"/v1/sessions/{child}",
                params={"include_items": "false", "include_liveness": "false"},
            )
            response.raise_for_status()
            return response.json().get("status") == "running"

        # Continuous output establishes the pane's running edge before steering;
        # a fresh redraw afterward would otherwise mask the lost work state.
        _wait(pane_is_running, "real streaming tool's pane to report running")
        before_followup = _mcp(client, parent, "sys_read_inbox", {})
        parent_before_followup = session_items(parent)
        followup = json.loads(
            _mcp(client, parent, "sys_session_send", {"session_id": child, "args": _FOLLOWUP})
        )
        sent_at = time.monotonic()
        while time.monotonic() - sent_at < _WATCHDOG_SECONDS + 35:
            assert pi_process.is_running() and tool_process.is_running(), (
                "The real Pi/tool process exited early"
            )
            assert time.time() - float((workspace / "heartbeat").read_text()) < 10, (
                "The actual tool stopped doing work"
            )
            time.sleep(1)
        during_work = _mcp(client, parent, "sys_read_inbox", {})
        before_tool_release = _records(ledger)
        parent_before_release = session_items(parent)
        parent_before_release_ids = {item["id"] for item in parent_before_release}
        watchdog_elapsed = time.monotonic() - sent_at
        assert not any(row["event"] == "agent_end" for row in _records(pi_events)), (
            "Pi ended before the tool release"
        )
        tool_released_at = time.time()
        (workspace / "release-tool").touch()
        _wait(
            lambda: any(row["event"] == "agent_end" for row in _records(pi_events)),
            "real Pi agent_end after its tool result",
        )
        _wait(
            lambda: any(
                row["event"] == "external_session_status"
                and row.get("status") == "idle"
                and row["session_id"] == child
                and row.get("response_id") == running[0]["response_id"]
                for row in _records(ledger)
            ),
            "matching native completion",
        )
        after_work = _mcp(client, parent, "sys_read_inbox", {})
        parent_after_work = session_items(parent)
        new_parent_items = [
            item for item in parent_after_work if item["id"] not in parent_before_release_ids
        ]
        child_items = session_items(child)
        child_requests = get_mock_requests(model_url, key=_CHILD_MODEL)
        heartbeat_call_ids = {
            item["call_id"]
            for item in child_items
            if item.get("type") == "function_call" and item.get("name") == "bash"
        }
        heartbeat_outputs = [
            item
            for item in child_items
            if item.get("type") == "function_call_output"
            and item.get("call_id") in heartbeat_call_ids
            and "PI_HEARTBEAT_TOOL_FINISHED" in item_text(item)
        ]
        assistant_outputs = [
            item
            for item in child_items
            if item.get("type") == "message"
            and item.get("role") == "assistant"
            and "PI_HEARTBEAT_TOOL_FINISHED" in item_text(item)
        ]
        followup_reached_model = (
            len(child_requests) == 2
            and _FOLLOWUP not in json.dumps(child_requests[0])
            and _FOLLOWUP in json.dumps(child_requests[1])
        )
        before_release_texts = [before_followup, during_work] + [
            item_text(item) for item in parent_before_release
        ]
        completion_delivered = has_status(
            [after_work, *(item_text(item) for item in new_parent_items)], "completed"
        )
        observed_pi_events = _records(pi_events)
        same_pi_run = [
            row["event"] for row in observed_pi_events if row["event"] != "session_start"
        ] == ["agent_start", "agent_end"] and all(
            row["pid"] == pi_process.pid for row in observed_pi_events
        )
        watchdog_failure_observed = any(
            f"produced no activity within {_WATCHDOG_SECONDS}s of dispatch" in text
            or "no start acknowledgment for sub-agent" in text
            for text in before_release_texts
        )
        summary = {
            "pi_version": rig.version,
            "parent_session_id": parent,
            "child_session_id": child,
            "pi_pid": pi_process.pid,
            "tool_pid": tool_process.pid,
            "handles": {
                label: {
                    key: handle[key]
                    for key in ("conversation_id", "kind", "agent", "title", "status")
                    if key in handle
                }
                for label, handle in (("initial", initial), ("followup", followup))
            },
            "inbox": {
                label: {
                    "empty": text == "Inbox is empty — no completed tasks.",
                    "terminal_notices": terminal_notices(text),
                }
                for label, text in (
                    ("before_followup", before_followup),
                    ("during_work", during_work),
                    ("after_work", after_work),
                )
            },
            "parent_before_followup": item_evidence(parent_before_followup),
            "parent_before_tool_release": item_evidence(parent_before_release),
            "parent_new_completion_items": item_evidence(new_parent_items),
            "child_items": item_evidence(child_items),
            "followup_reached_model": followup_reached_model,
            "same_pi_run": same_pi_run,
            "watchdog_failure_observed": watchdog_failure_observed,
            "child_model_request_count": len(child_requests),
            "actual_tool_result_ids": [item["id"] for item in heartbeat_outputs],
            "actual_assistant_result_ids": [item["id"] for item in assistant_outputs],
            "completion_delivered_after_release": completion_delivered,
            "watchdog_elapsed_seconds": watchdog_elapsed,
            "tool_released_at": tool_released_at,
            "tool_finished_at": (
                float((workspace / "tool-finished").read_text())
                if (workspace / "tool-finished").exists()
                else None
            ),
            "status_events": [
                row
                for row in _records(ledger)
                if row["event"] == "external_session_status" and row["session_id"] == child
            ],
            "pi_events": observed_pi_events,
        }
        failures = []
        if any(
            row["event"] == "external_session_status"
            and row.get("status") == "idle"
            and row["session_id"] == child
            for row in before_tool_release
        ):
            failures.append("startup published idle while real Pi and its bash tool were running")
        if followup.get("status") != "running":
            failures.append("follow-up was classified as a fresh launch instead of active work")
        if has_status(before_release_texts, "completed"):
            failures.append("parent received premature completion")
        if has_status(before_release_texts, "failed"):
            failures.append(
                "parent received watchdog failure while the same Pi/tool processes were alive"
            )
        if not followup_reached_model:
            failures.append("Pi did not deliver the follow-up to its real model request")
        if not same_pi_run:
            failures.append("The follow-up did not stay within the original Pi agent loop")
        if not heartbeat_outputs:
            failures.append("Pi did not persist its actual bash function_call_output")
        if not assistant_outputs:
            failures.append("Pi did not persist its final assistant answer")
        if not completion_delivered:
            failures.append("parent did not receive a new real completion after the tool finished")
        summary["failures"] = failures
        (evidence / "result.json").write_text(json.dumps(summary, indent=2))
        assert not failures, "\n".join(failures) + "\n" + json.dumps(summary, indent=2)
    finally:
        (evidence / "release-startup").touch()
        (workspace / "release-tool").touch()
        cleanup_errors = []
        for session_id in (child, parent):
            if session_id is None:
                continue
            try:
                response = client.delete(f"/v1/sessions/{session_id}", timeout=30)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                cleanup_errors.append({"session_id": session_id, "error_type": type(exc).__name__})
        if cleanup_errors:
            (evidence / "cleanup-errors.json").write_text(json.dumps(cleanup_errors, indent=2))
