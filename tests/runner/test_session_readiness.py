"""Runner readiness probes are independent of user messages and terminal existence."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from omnigent.runner import create_runner_app
from omnigent.runner.native import readiness
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _ProcessManager(_FakeProcessManager):
    def session_is_running(self, session_id: str) -> bool:
        return self.has_session(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-sdk", "codex-native", "goose-native"])
async def test_input_readiness_requires_init_and_live_harness(
    harness: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = AgentSpec(
        spec_version=1,
        name="test",
        executor=ExecutorSpec(type="omnigent", config={"harness": harness}),
    )
    pm = _ProcessManager(_ScriptedHarnessClient([]))
    probe = AsyncMock(return_value=False)
    monkeypatch.setattr(readiness, "codex", probe)
    monkeypatch.setattr(
        "omnigent.runner.app._launch_native_terminal", AsyncMock(return_value=True)
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=AsyncMock(return_value=spec),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    session_id = "63b87ffb024e4d02841012668662a167"
    path = f"/v1/sessions/{session_id}/readiness"
    async with _runner_client(app) as client:
        before = (await client.get(path)).json()
        assert before["initialized"] is False
        response = await client.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": "agent_readiness"}
        )
        assert response.status_code == 201, response.text
        pending = (await client.get(path)).json()
        assert pending["initialized"] is True
        assert pending["input_ready"] is (harness == "claude-sdk")
        assert pending["supported"] is (harness != "goose-native")
        if harness == "codex-native":
            probe.return_value = True
            assert (await client.get(path)).json()["input_ready"] is True
            probe.side_effect = RuntimeError("native startup failed")
            assert (await client.get(path)).json()["input_ready"] is False
        pm._sessions.clear()
        assert (await client.get(path)).json()["input_ready"] is False


@pytest.mark.asyncio
async def test_delete_during_probe_discards_late_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = AgentSpec(
        spec_version=1,
        name="test",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )
    pm = _ProcessManager(_ScriptedHarnessClient([]))
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(_env: object) -> bool:
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(readiness, "codex", probe)
    monkeypatch.setattr(
        "omnigent.runner.app._launch_native_terminal", AsyncMock(return_value=True)
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=AsyncMock(return_value=spec),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    session_id = "a209c42ca2d141da9dd381aac8756477"
    async with _runner_client(app) as client:
        assert (
            await client.post(
                "/v1/sessions", json={"session_id": session_id, "agent_id": "agent_readiness"}
            )
        ).status_code == 201
        pending = asyncio.create_task(client.get(f"/v1/sessions/{session_id}/readiness"))
        await asyncio.wait_for(entered.wait(), 1)
        assert (await client.delete(f"/v1/sessions/{session_id}")).status_code < 300
        release.set()
        assert (await pending).json()["input_ready"] is False
