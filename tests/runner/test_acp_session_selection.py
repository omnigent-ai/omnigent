"""Preserve a named ACP selection across session init and cached turns."""

from pathlib import Path

import pytest
import yaml

from omnigent.runner import create_runner_app
from omnigent.runner.app import _session_event_queues_ref
from omnigent.runner.session_init_protocol import (
    SESSION_INIT_PROTOCOL_VERSION,
    RunnerSessionInitEnvelope,
    RunnerSessionInitSnapshot,
)
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize("override", ["acp:goose", "acp:missing"])
async def test_acp_selection_survives_init_and_turn_without_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "acp": {
                    "agents": [
                        {"name": "Gemini", "command": "gemini --experimental-acp"},
                        {"name": "Goose", "command": "goose acp", "model": "goose-model"},
                    ]
                }
            }
        )
    )
    spec = AgentSpec(
        spec_version=1,
        name="selection",
        executor=ExecutorSpec(type="omnigent", config={"harness": "acp:gemini"}),
    )

    async def resolve(_agent_id: str, _session_id: str | None = None) -> AgentSpec:
        return spec

    harness = _ScriptedHarnessClient(
        [
            'event: response.completed\ndata: {"type":"response.completed",'
            '"response":{"id":"response","status":"completed"}}\n\n'
        ]
    )
    manager = _FakeProcessManager(harness)
    app = create_runner_app(
        process_manager=manager, spec_resolver=resolve, server_client=NullServerClient()
    )
    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="0.0.0.dev0",
        session_id="selection",
        agent_id="agent",
        sub_agent_name=None,
        snapshot=RunnerSessionInitSnapshot(created_at=0, updated_at=0, harness_override=override),
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions",
            json={
                "session_id": "selection",
                "agent_id": "agent",
                "session_init": envelope.model_dump(mode="json"),
            },
        )
        if override == "acp:missing":
            assert response.status_code == 400, response.text
            assert "not configured on this runner" in response.text
            assert not manager.get_client_calls
            queue = _session_event_queues_ref["selection"]
            statuses = []
            while not queue.empty():
                statuses.append(queue.get_nowait())
            assert any(
                event.get("status") == "failed"
                and "not configured on this runner" in event["error"]["message"]
                for event in statuses
            ), statuses
            for _ in range(2):
                snapshot = await client.get("/v1/sessions/selection")
                assert snapshot.status_code == 200, snapshot.text
                assert snapshot.json()["status"] == "failed"
            await client.delete("/v1/sessions/selection")
            return
        assert response.status_code == 201, response.text
        assert manager.get_client_calls[-1][2]["HARNESS_ACP_COMMAND"] == "goose acp"
        manager.get_client_calls.clear()
        response = await client.post(
            "/v1/sessions/selection/events?stream=true",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "agent",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        )
        assert response.status_code == 200, response.text
        assert "response.completed" in response.text
        assert manager.get_client_calls
        for _session, family, env in manager.get_client_calls:
            assert family == "acp"
            assert env is not None
            assert env["HARNESS_ACP_COMMAND"] == "goose acp"
            assert env["HARNESS_ACP_MODEL"] == "goose-model"
        await client.delete("/v1/sessions/selection")
    assert spec.executor.config["harness"] == "acp:gemini"
