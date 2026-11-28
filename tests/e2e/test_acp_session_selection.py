"""Resolve ACP session choices on a runner with its own configuration."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.runner.identity import token_bound_runner_id
from tests.e2e.conftest import find_free_port

_REPO = Path(__file__).resolve().parents[2]
_AGENT = r"""
import json, sys
from pathlib import Path
name, record = sys.argv[1:]
current_model = "unset"
with Path(record).open("a") as f:
    f.write(name + "\n")
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}
    elif method == "session/new":
        result = {"sessionId": "test-session"}
    elif method == "session/set_config_option":
        current_model = msg["params"]["value"]
        result = {"configOptions": [{"id": "model", "currentValue": current_model}]}
    elif method == "session/prompt":
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": msg["params"]["sessionId"], "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text",
                    "text": "Reply from " + name + "; model=" + current_model}}}})
        result = {"stopReason": "end_turn"}
    else:
        continue
    send({"jsonrpc": "2.0", "id": mid, "result": result})
"""


@pytest.fixture(scope="module", params=["empty-server", "conflicting-server-default"])
def acp_server(
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[httpx.Client, str, Path, bool]]:
    """Run real server/runner processes with different ACP configurations."""
    root = tmp_path_factory.mktemp("acp-selection")
    server_config = root / "server-config"
    runner_config = root / "runner-config"
    for config in (server_config, runner_config):
        config.mkdir()
    executable = root / "agent.py"
    executable.write_text(_AGENT)
    launches = root / "launches.txt"
    entries = [
        {
            "name": name,
            "command": shlex.join([sys.executable, str(executable), name, str(launches)]),
            "omnigent_mcp": False,
            "inject_system_prompt": False,
        }
        for name in ("Gemini", "Goose")
    ]
    server_settings: dict = {}
    runner_settings: dict = {"acp": {"agents": entries}}
    executor: dict = {"harness": "openai-agents", "model": "gpt-4o"}
    if request.param == "conflicting-server-default":
        provider = {
            "kind": "gateway",
            "openai": {
                "base_url": "https://unused.example.invalid/v1",
                "api_key": "unused-test-key",
                "models": {"default": "goose-model", "alternate": "gemini-model"},
            },
        }
        server_settings = {
            "providers": {"curated": provider},
            "acp": {
                "agents": [
                    {"name": name, "command": "must-not-run", "model": "unlisted-server-model"}
                    for name in ("Gemini", "Goose")
                ]
            },
        }
        runner_settings["providers"] = {"curated": provider}
        for entry in entries:
            entry["model"] = entry["name"].lower() + "-model"
        executor = {"harness": "openai-agents", "auth": {"type": "provider", "name": "curated"}}
    (server_config / "config.yaml").write_text(yaml.safe_dump(server_settings))
    (runner_config / "config.yaml").write_text(yaml.safe_dump(runner_settings))
    spec = root / "selection.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "name": "selection",
                "prompt": "Say hello",
                "executor": executor,
            }
        )
    )
    token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(token)
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            [str(_REPO), str(_REPO / "sdks/python-client"), str(_REPO / "sdks/ui")]
        ),
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_AUTH_ENABLED": "0",
        "OMNIGENT_DISABLE_KEYRING": "1",
        "OPENAI_API_KEY": "unused-test-key",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    processes: list[subprocess.Popen[bytes]] = []
    with contextlib.ExitStack() as stack:
        server_log = stack.enter_context((root / "server.log").open("wb"))
        runner_log = stack.enter_context((root / "runner.log").open("wb"))
        try:
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "omnigent.cli",
                        "server",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--database-uri",
                        f"sqlite:///{root / 'server.db'}",
                        "--artifact-location",
                        str(root / "artifacts"),
                        "--agent",
                        str(spec),
                    ],
                    env={
                        **env,
                        "OMNIGENT_CONFIG_HOME": str(server_config),
                        "OMNIGENT_RUNNER_TUNNEL_TOKEN": token,
                    },
                    cwd=_REPO,
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                )
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "omnigent.runner._entry"],
                    env={
                        **env,
                        "OMNIGENT_CONFIG_HOME": str(runner_config),
                        "OMNIGENT_RUNNER_ID": runner_id,
                        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                        "RUNNER_SERVER_URL": base_url,
                        "OMNIGENT_RUNNER_WORKSPACE": str(root),
                    },
                    cwd=_REPO,
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                )
            )
            with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        response = client.get(f"/v1/runners/{runner_id}/status")
                        if response.status_code == 200 and response.json().get("online"):
                            break
                    except httpx.TransportError:
                        pass  # The server is still starting.
                    time.sleep(0.1)
                else:
                    pytest.fail(
                        (root / "server.log").read_text() + (root / "runner.log").read_text()
                    )
                yield client, runner_id, launches, request.param == "conflicting-server-default"
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _message(text: str) -> dict:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def _wait_for_reply(client: httpx.Client, session: str, count: int) -> dict:
    deadline = time.monotonic() + 45
    snapshot: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{session}")
        response.raise_for_status()
        snapshot = response.json()
        items = snapshot.get("items", [])
        replies = [
            i for i in items if i["type"] == "message" and i["data"].get("role") == "assistant"
        ]
        if (snapshot["status"] == "failed" and snapshot.get("last_task_error")) or (
            len(replies) >= count and snapshot["status"] == "idle"
        ):
            return snapshot
        time.sleep(0.1)
    pytest.fail(f"No completed turn: {snapshot}")


@pytest.mark.parametrize(
    "override, expected", [("acp:goose", "Goose"), ("acp", "Gemini"), ("acp:missing", None)]
)
def test_acp_choice_is_resolved_on_runner(
    acp_server: tuple[httpx.Client, str, Path, bool], override: str, expected: str | None
) -> None:
    client, runner_id, launches, has_model_policy = acp_server
    agents = client.get("/v1/agents").json()["data"]
    agent = next(a for a in agents if a["name"] == "selection")
    before = launches.read_text() if launches.exists() else ""
    response = client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "harness_override": override,
            "initial_items": [_message("First turn")],
        },
    )
    assert response.status_code == 201, response.text
    session = response.json()["id"]
    try:
        attached = client.patch(f"/v1/sessions/{session}", json={"runner_id": runner_id})
        assert attached.status_code == 200, attached.text
        for turn in range(1, 3):
            if turn == 2:
                response = client.post(
                    f"/v1/sessions/{session}/events", json=_message("Second turn")
                )
                assert response.is_success, response.text
            snapshot = _wait_for_reply(client, session, turn)
            items = snapshot.get("items", [])
            if expected is None:
                assert snapshot["status"] == "failed", snapshot
                assert "not configured on this runner" in snapshot["last_task_error"]["message"], (
                    snapshot
                )
                assert (launches.read_text() if launches.exists() else "") == before
                break
            replies = [
                i for i in items if i["type"] == "message" and i["data"].get("role") == "assistant"
            ]
            assert len(replies) == turn, items
            assert all(f"Reply from {expected}" in json.dumps(i) for i in replies), items
            if has_model_policy:
                assert all(f"model={expected.lower()}-model" in json.dumps(i) for i in replies), (
                    items
                )
        assert client.get(f"/v1/sessions/{session}").json()["harness"] == override
    finally:
        client.delete(f"/v1/sessions/{session}").raise_for_status()
