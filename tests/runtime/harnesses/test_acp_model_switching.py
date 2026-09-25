"""Real harness and ACP subprocesses preserve a session across model switches."""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from omnigent.runner.app import _build_spawn_env_from_spec
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth

_FAKE_ACP_AGENT = r"""
import json
import os
import sys

record_path, protocol, *options = sys.argv[1:]
failure = options[0] if options else ""
failed_once = False
model = "model-a"
session_id = "fake-" + str(os.getpid())
turn = 0

def send(value):
    print(json.dumps(value), flush=True)

def model_options():
    return [{"id": "model", "currentValue": model,
             "options": [{"value": value} for value in ("model-a", "model-b", "model-c")]}]

for line in sys.stdin:
    request = json.loads(line)
    with open(record_path, "a") as record:
        record.write(json.dumps({"pid": os.getpid(), **request}) + "\n")
    method, params = request.get("method"), request.get("params", {})
    if method in ("session/set_model", "session/set_config_option") and not failed_once:
        if failure == "reject-once":
            failed_once = True
            send({"jsonrpc": "2.0", "id": request["id"],
                  "error": {"code": -32000, "message": "model temporarily unavailable"}})
            continue
        if failure == "different-echo-once":
            failed_once = True
            send({"jsonrpc": "2.0", "id": request["id"],
                  "result": {"configOptions": model_options()}})
            continue
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}
    elif method == "session/new":
        model = params.get("model", model)
        result = {"sessionId": session_id}
        if protocol == "catalog":
            result["models"] = {
                "currentModelId": model,
                "availableModels": [
                    {"modelId": value} for value in ("model-a", "model-b", "model-c")
                ],
            }
        else:
            result["configOptions"] = model_options()
    elif method == "session/set_model" and protocol == "catalog":
        assert params["sessionId"] == session_id
        model = params["modelId"]
        result = {}
    elif method == "session/set_config_option" and protocol == "config":
        assert params["sessionId"] == session_id
        model = params["value"]
        result = {"configOptions": model_options()}
    elif method == "session/prompt":
        assert params["sessionId"] == session_id
        turn += 1
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id, "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": f"turn={turn};model={model}"}}}})
        result = {"stopReason": "end_turn"}
    else:
        send({"jsonrpc": "2.0", "id": request["id"],
              "error": {"code": -32601, "message": "unsupported method"}})
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("protocol", "failure"),
    [
        ("config", ""),
        ("catalog", ""),
        ("config", "reject-once"),
        ("catalog", "reject-once"),
        ("config", "different-echo-once"),
    ],
)
async def test_acp_picker_switch_and_reset_preserve_process_and_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, failure: str
) -> None:
    """Picks, retries, and reset preserve the session without wrong-model turns."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "test-gateway": {
                        "kind": "gateway",
                        "openai": {
                            "base_url": "https://gateway.invalid/v1",
                            "api_key_ref": "env:UNSET_FAKE_GATEWAY_KEY",
                            "models": {
                                "default": "model-a",
                                "fast": "model-b",
                                "large": "model-c",
                            },
                        },
                    }
                }
            }
        )
    )
    script = tmp_path / "fake_acp.py"
    script.write_text(_FAKE_ACP_AGENT)
    record_path = tmp_path / "requests.jsonl"
    spec = AgentSpec(
        spec_version=1,
        name="test-acp",
        instructions="Test agent",
        executor=ExecutorSpec(
            type="omnigent",
            auth=ProviderAuth(name="test-gateway"),
            config={
                "harness": "acp:fake",
                "acp_agent": {
                    "name": "Fake ACP",
                    "command": shlex.join(
                        [sys.executable, str(script), str(record_path), protocol, failure]
                    ),
                    "send_model": True,
                    "omnigent_mcp": False,
                },
            },
        ),
    )
    # macOS Unix sockets need a shorter path than pytest's default temp root.
    with tempfile.TemporaryDirectory(
        prefix="acp-pm-", dir="/tmp" if sys.platform != "win32" else None
    ) as socket_root:
        manager = HarnessProcessManager(tmp_parent=Path(socket_root))
        await manager.start(sweep_orphans=False)
        first_client = None
        first_pid = None
        try:
            turn = 0
            picks = (
                ("model-b", "model-c", "model-c", None)
                if failure
                else ("model-b", "model-c", None)
            )
            for attempt, override in enumerate(picks, start=1):
                env = _build_spawn_env_from_spec(spec, "acp", model_override=override)
                client = await manager.get_client("conv_acp_switch", "acp", env=env)
                pid = manager._entries["conv_acp_switch"].process.pid
                if first_client is None:
                    first_client, first_pid = client, pid
                assert client is first_client
                assert pid == first_pid
                response = await client.post(
                    "/v1/sessions/conv_acp_switch/events",
                    json={
                        "type": "message",
                        "role": "user",
                        "model": "test-acp",
                        "content": f"Attempt {attempt}",
                        "model_override": override,
                    },
                )
                response.raise_for_status()
                if failure and attempt == 2:
                    assert "event: response.failed" in response.text
                    assert "ACP model selection failed" in response.text
                    assert "No prompt was sent" in response.text
                    requests = [json.loads(line) for line in record_path.read_text().splitlines()]
                    assert sum(r.get("method") == "session/prompt" for r in requests) == 1
                    continue
                turn += 1
                assert "event: response.completed" in response.text
                assert f"turn={turn};model={override or 'model-a'}" in response.text
        finally:
            await manager.shutdown()

    requests = [json.loads(line) for line in record_path.read_text().splitlines()]
    assert len({request["pid"] for request in requests}) == 1
    new_sessions = [request for request in requests if request.get("method") == "session/new"]
    assert len(new_sessions) == 1
    assert new_sessions[0]["params"]["model"] == "model-b"
    switch_method = "session/set_model" if protocol == "catalog" else "session/set_config_option"
    switches = [
        request["params"] for request in requests if request.get("method") == switch_method
    ]
    model_field = "modelId" if protocol == "catalog" else "value"
    expected_switches = ["model-c", "model-c", "model-a"] if failure else ["model-c", "model-a"]
    assert [switch[model_field] for switch in switches] == expected_switches
    assert len({switch["sessionId"] for switch in switches}) == 1
    assert sum(r.get("method") == "session/prompt" for r in requests) == 3
