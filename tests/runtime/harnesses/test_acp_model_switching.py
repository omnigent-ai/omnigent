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


_FAKE_ACP_EFFORT_AGENT = r"""
import json
import os
import sys

record_path = sys.argv[1]
model = "ag-pro"
effort = "MEDIUM"
session_id = "fake-eff-" + str(os.getpid())
turn = 0

def send(value):
    print(json.dumps(value), flush=True)

def config_options():
    return [
        {
            "id": "model",
            "category": "model",
            "currentValue": model,
            "options": [
                {"value": "ag-pro", "name": "AG Pro"},
                {"value": "ag-flash", "name": "AG Flash"},
            ],
        },
        {
            "id": "thinking",
            "category": "thought_level",
            "currentValue": effort,
            "options": [
                {"value": "LOW", "name": "Low"},
                {"value": "MEDIUM", "name": "Medium"},
                {"value": "HIGH", "name": "High"},
            ],
        },
    ]

for line in sys.stdin:
    request = json.loads(line)
    with open(record_path, "a") as record:
        record.write(json.dumps({"pid": os.getpid(), **request}) + "\n")
    method, params = request.get("method"), request.get("params", {})
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}}
    elif method == "session/new":
        result = {"sessionId": session_id, "configOptions": config_options()}
    elif method == "session/set_config_option":
        assert params["sessionId"] == session_id
        if params["configId"] == "model":
            model = params["value"]
        elif params["configId"] == "thinking":
            effort = params["value"]
        result = {"configOptions": config_options()}
    elif method == "session/prompt":
        assert params["sessionId"] == session_id
        turn += 1
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id, "update": {"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": f"turn={turn};model={model};effort={effort}"}}}})
        result = {"stopReason": "end_turn"}
    else:
        send({"jsonrpc": "2.0", "id": request["id"],
              "error": {"code": -32601, "message": "unsupported method"}})
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
"""


@pytest.mark.asyncio
async def test_acp_dynamic_discovery_and_live_effort_switching(tmp_path: Path) -> None:
    """Subprocess supports pre-turn /model-options, /session-settings, and no-respawn."""
    script = tmp_path / "fake_acp_efforts.py"
    script.write_text(_FAKE_ACP_EFFORT_AGENT)
    record_path = tmp_path / "effort_requests.jsonl"
    spec = AgentSpec(
        spec_version=1,
        name="test-acp-efforts",
        instructions="Test agent",
        executor=ExecutorSpec(
            type="omnigent",
            config={
                "harness": "acp:fake",
                "acp_agent": {
                    "name": "Fake Effort ACP",
                    "command": shlex.join([sys.executable, str(script), str(record_path)]),
                    "omnigent_mcp": False,
                },
            },
        ),
    )

    with tempfile.TemporaryDirectory(
        prefix="acp-eff-", dir="/tmp" if sys.platform != "win32" else None
    ) as socket_root:
        manager = HarnessProcessManager(tmp_parent=Path(socket_root))
        await manager.start(sweep_orphans=False)
        try:
            env1 = _build_spawn_env_from_spec(spec, "acp", model_override=None)
            client = await manager.get_client("conv_acp_efforts", "acp", env=env1)
            first_pid = manager._entries["conv_acp_efforts"].process.pid

            opts_resp = await client.get("/v1/sessions/conv_acp_efforts/model-options")
            opts_resp.raise_for_status()
            opts = opts_resp.json()
            assert [m["id"] for m in opts["models"]] == ["ag-pro", "ag-flash"]
            efforts = opts["models"][0]["supportedReasoningEfforts"]
            assert [e["reasoningEffort"] for e in efforts] == ["low", "medium", "high"]
            assert opts["active_model"] == "ag-pro"
            assert opts["active_effort"] == "medium"

            turn1 = await client.post(
                "/v1/sessions/conv_acp_efforts/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-acp-efforts",
                    "content": "First turn",
                    "reasoning": {"effort": "low"},
                },
            )
            turn1.raise_for_status()
            assert "turn=1;model=ag-pro;effort=LOW" in turn1.text

            # Live-switch model + effort via /session-settings.
            settings_resp = await client.post(
                "/v1/sessions/conv_acp_efforts/session-settings",
                json={"model": "ag-flash", "reasoning_effort": "high"},
            )
            settings_resp.raise_for_status()
            assert settings_resp.json() == {
                "active_model": "ag-flash",
                "active_effort": "high",
                "applied": True,
            }

            # Getting client with updated model_override must NOT respawn ACP.
            env2 = _build_spawn_env_from_spec(spec, "acp", model_override="ag-flash")
            client2 = await manager.get_client("conv_acp_efforts", "acp", env=env2)
            assert client2 is client
            assert manager._entries["conv_acp_efforts"].process.pid == first_pid

            turn2 = await client2.post(
                "/v1/sessions/conv_acp_efforts/events",
                json={
                    "type": "message",
                    "role": "user",
                    "model": "test-acp-efforts",
                    "content": "Second turn",
                    "model_override": "ag-flash",
                    "reasoning": {"effort": "high"},
                },
            )
            turn2.raise_for_status()
            assert "turn=2;model=ag-flash;effort=HIGH" in turn2.text
        finally:
            await manager.shutdown()

    requests = [json.loads(line) for line in record_path.read_text().splitlines()]
    assert len({r["pid"] for r in requests}) == 1
    assert sum(r.get("method") == "session/new" for r in requests) == 1


@pytest.mark.asyncio
async def test_scaffold_post_live_session_settings_returns_400_on_rejected_error() -> None:
    """LiveSettingsRejectedError surfaces as HTTP 400 session_settings_rejected."""
    from fastapi.testclient import TestClient

    from omnigent.inner.acp_executor import _AcpModelSwitchError
    from omnigent.runtime.harnesses._scaffold import HarnessApp

    class _DummyHarnessApp(HarnessApp):
        def supports_live_model_options(self) -> bool:
            return True

        async def apply_live_settings(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            raise _AcpModelSwitchError("model foo rejected by agent")

        async def run_turn(self, request: object, ctx: object) -> None:
            del request, ctx
            raise NotImplementedError

    app = _DummyHarnessApp().build()
    app.state.harness_auth_token = "tok"
    app.state.conversation_id = "conv_1"
    with TestClient(app) as client:
        resp = client.post(
            "/v1/sessions/conv_1/session-settings",
            headers={"Authorization": "Bearer tok"},
            json={"model": "foo"},
        )
    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "error": "session_settings_rejected",
        "detail": "model foo rejected by agent",
    }
