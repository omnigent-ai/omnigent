"""Runner, HTTP scaffold, and stdio ACP session checkpoint round trips."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml

from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.inner.test_acp_native_session import _executor, _requests
from tests.runner.conftest import _FakeProcessManager, _runner_client


@contextlib.asynccontextmanager
async def _live_harness(adapter: ExecutorAdapter) -> AsyncIterator[httpx.AsyncClient]:
    app = adapter.build()
    app.state.conversation_id = "conversation-1"
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    server.capture_signals = contextlib.nullcontext
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            yield client
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
        await adapter.on_shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_ack", [False, True])
async def test_runner_persists_before_prompt_and_restores_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_before_ack: bool
) -> None:
    initial_executor, log = _executor(tmp_path)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {"acp": {"agents": [{"name": "Example", "command": initial_executor._config.command}]}}
        )
    )
    spec = AgentSpec(
        spec_version=1,
        name="example-agent",
        executor=ExecutorSpec(type="omnigent", config={"harness": "acp:example"}),
    )

    async def resolve(_agent_id: str, _session_id: str | None = None) -> AgentSpec:
        return spec

    stored_reference: str | None = None
    entered = asyncio.Event()
    acknowledged = asyncio.Event()
    checkpoint_writes = 0

    async def server(request: httpx.Request) -> httpx.Response:
        nonlocal stored_reference, checkpoint_writes
        if request.method == "PATCH":
            body = json.loads(request.content)
            if "external_session_id" in body:
                checkpoint_writes += 1
                stored_reference = body["external_session_id"]
                entered.set()
                await acknowledged.wait()
        return httpx.Response(
            200, json={"external_session_id": stored_reference, "labels": {}, "items": []}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="https://server.example.invalid"
    ) as server_client:
        for restart in (False, True):
            executor = _executor(tmp_path)[0] if restart else initial_executor
            adapter = ExecutorAdapter(lambda executor=executor: executor)
            async with _live_harness(adapter) as harness_client:
                manager = _FakeProcessManager(harness_client)
                app = create_runner_app(
                    process_manager=manager, spec_resolver=resolve, server_client=server_client
                )
                async with _runner_client(app) as client:
                    created = await client.post(
                        "/v1/sessions",
                        json={"session_id": "conversation-1", "agent_id": "example-agent"},
                    )
                    assert created.status_code == 201, created.text
                    turn = asyncio.create_task(
                        client.post(
                            "/v1/sessions/conversation-1/events?stream=true",
                            json={
                                "type": "message",
                                "role": "user",
                                "agent_id": "example-agent",
                                "model": "example-agent",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "follow up" if restart else "hello",
                                    }
                                ],
                            },
                        )
                    )
                    try:
                        if not restart:
                            await asyncio.wait_for(entered.wait(), 5)
                            assert stored_reference == "native-1"
                            assert [r["method"] for r in _requests(log)] == [
                                "initialize",
                                "session/new",
                            ]
                            wrong_ack = await harness_client.post(
                                "/v1/sessions/conversation-1/events",
                                json={
                                    "type": "native_session_checkpoint",
                                    "checkpoint_id": "unknown-checkpoint",
                                    "success": True,
                                },
                            )
                            assert wrong_ack.status_code == 204
                            bad_type = await harness_client.post(
                                "/v1/sessions/conversation-1/events",
                                json={"type": "unknown_checkpoint", "success": True},
                            )
                            assert bad_type.status_code == 422
                            assert not turn.done()
                            if cancel_before_ack:
                                cancelled = await harness_client.post(
                                    "/v1/sessions/conversation-1/events",
                                    json={"type": "interrupt"},
                                )
                                assert cancelled.status_code == 204
                            acknowledged.set()
                        response = await asyncio.wait_for(turn, 10)
                        assert response.status_code == 200, response.text
                        terminal = (
                            "response.cancelled"
                            if cancel_before_ack and not restart
                            else "response.completed"
                        )
                        assert terminal in response.text
                        assert "native_session.checkpoint_requested" not in response.text
                        assert "old transcript" not in response.text
                        if cancel_before_ack and not restart:
                            assert not any(
                                r.get("method") == "session/prompt" for r in _requests(log)
                            )
                        await client.delete("/v1/sessions/conversation-1")
                    finally:
                        acknowledged.set()
                        turn.cancel()
                        await asyncio.gather(turn, return_exceptions=True)
    requests = _requests(log)
    assert checkpoint_writes == 1
    assert [r.get("method") for r in requests].count("session/new") == 1
    assert [r.get("method") for r in requests].count("session/load") == 1
    assert [r for r in requests if r.get("method") == "session/prompt"][-1]["params"] == {
        "sessionId": "native-1",
        "prompt": [{"type": "text", "text": "follow up"}],
    }
