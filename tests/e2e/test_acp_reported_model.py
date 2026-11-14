"""An own-auth ACP session shows the model its agent reports, across a real server and runner."""

from __future__ import annotations

import contextlib
import os
import secrets
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
_PINNED_MODEL = "gpt-4o-mini"
_REPORTED_MODEL = "grok-4.6"

# Grok Build stand-in. It reports its model as a model-category option with its
# own id right after ``session/new``, the way a real agent does, so the report
# is queued before the first prompt.
_FAKE_GROK_AGENT = r"""#!/usr/bin/env python3
import json, sys
MODEL = "grok-4.6"
SESSION = "fake-grok-session"
OPTION = {
    "id": "llm",
    "name": "Model",
    "category": "model",
    "type": "select",
    "currentValue": MODEL,
    "options": [{"value": MODEL, "name": MODEL}],
}

def send(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()

def update(content):
    return {"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": SESSION, "update": content}}

for line in sys.stdin:
    message = json.loads(line)
    request_id, method = message.get("id"), message.get("method")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}
        send({"jsonrpc": "2.0", "id": request_id, "result": result})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": SESSION}})
        send(update({"sessionUpdate": "config_option_update", "configOptions": [OPTION]}))
    elif method == "session/prompt":
        reply = {"sessionUpdate": "agent_message_chunk",
                 "content": {"type": "text", "text": "Reply from " + MODEL}}
        send(update(reply))
        usage = {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8}
        send({"jsonrpc": "2.0", "id": request_id,
              "result": {"stopReason": "end_turn", "usage": usage}})
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
"""


@pytest.fixture(scope="module")
def acp_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[httpx.Client, str]]:
    """Run a real server and runner whose ``grok`` harness row launches the fake agent."""
    root = tmp_path_factory.mktemp("acp-reported-model")
    server_config = root / "server-config"
    runner_config = root / "runner-config"
    for config in (server_config, runner_config):
        config.mkdir()
    fake_grok = root / "fake-grok"
    fake_grok.write_text(_FAKE_GROK_AGENT)
    fake_grok.chmod(0o755)
    (runner_config / "config.yaml").write_text(
        yaml.safe_dump({"harness": {"grok": {"command": str(fake_grok)}}})
    )
    spec = root / "pinned.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "name": "pinned",
                "prompt": "Say hello",
                "executor": {"harness": "openai-agents", "model": _PINNED_MODEL},
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
                yield client, runner_id
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def _wait_for_reply(client: httpx.Client, session: str) -> dict:
    deadline = time.monotonic() + 45
    snapshot: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{session}")
        response.raise_for_status()
        snapshot = response.json()
        replies = [
            item
            for item in snapshot.get("items", [])
            if item["type"] == "message" and item["data"].get("role") == "assistant"
        ]
        if (snapshot["status"] == "failed" and snapshot.get("last_task_error")) or (
            replies and snapshot["status"] == "idle"
        ):
            return snapshot
        time.sleep(0.1)
    pytest.fail(f"No completed turn: {snapshot}")


def test_own_auth_acp_session_reports_the_agents_model(
    acp_server: tuple[httpx.Client, str],
) -> None:
    """Before a turn the snapshot claims no model; after the agent reports, it shows that model."""
    client, runner_id = acp_server
    agent = next(a for a in client.get("/v1/agents").json()["data"] if a["name"] == "pinned")
    created = client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "harness_override": "grok"}
    )
    assert created.status_code == 201, created.text
    session = created.json()["id"]
    attached = client.patch(f"/v1/sessions/{session}", json={"runner_id": runner_id})
    assert attached.status_code == 200, attached.text

    before = client.get(f"/v1/sessions/{session}").json()
    assert before["harness"] == "grok"
    assert before["llm_model"] is None, "the spec pin must not be shown for an own-auth agent"

    sent = client.post(
        f"/v1/sessions/{session}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "Say hello"}]},
        },
    )
    assert sent.status_code == 202, sent.text
    snapshot = _wait_for_reply(client, session)
    assert snapshot["status"] == "idle", snapshot.get("last_task_error")
    assert snapshot["llm_model"] == _REPORTED_MODEL
    assert snapshot["llm_model"] != _PINNED_MODEL

    # A fresh read, as a page reload would issue, still shows the reported model.
    reloaded = client.get(f"/v1/sessions/{session}").json()
    assert reloaded["llm_model"] == _REPORTED_MODEL
