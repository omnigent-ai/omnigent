"""SDK completion and runner reconnect both publish locally captured PRs."""

from __future__ import annotations

import json

import httpx
from fastapi.routing import APIRoute

from omnigent.runner import create_runner_app
from omnigent.runner.session_init_protocol import (
    SESSION_INIT_PROTOCOL_VERSION,
    RunnerSessionInitEnvelope,
    RunnerSessionInitSnapshot,
)
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient, _sse

SESSION = "b17c0a4f9d2e4c6a8f1b3d5e7a9c0b2d"
URL = "https://github.com/example/repo/pull/42"


async def test_session_init_restores_server_prs_before_resolving_agent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    remote = SessionPrRegistry(SESSION, root=tmp_path / "remote")
    remote.record([PullRequestRef.from_url(URL)], relationship="created", source="sdk")
    saved = remote.snapshot()
    assert saved is not None
    revision, entries = saved
    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="0.0.0.dev0",
        session_id=SESSION,
        agent_id="agent",
        suppress_recovery_turn=True,
        snapshot=RunnerSessionInitSnapshot(
            created_at=0,
            updated_at=0,
            pull_requests=[entry.model_dump() for entry in entries],
            pull_requests_updated_at_ns=revision,
        ),
    )
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    async def resolve(*_args) -> AgentSpec:
        restored = SessionPrRegistry(SESSION).snapshot()
        assert restored is not None and restored[0] > revision
        assert restored[1] == entries
        return AgentSpec(spec_version=1, name="pr-probe")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as server:
        app = create_runner_app(
            process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[bad-argument-type]
            spec_resolver=resolve,
            server_client=server,
        )
        async with _runner_client(app) as client:
            response = await client.post(
                "/v1/sessions",
                json={
                    "session_id": SESSION,
                    "agent_id": "agent",
                    "session_init": envelope.model_dump(mode="json"),
                },
            )
            assert response.status_code == 201, response.text
    assert not any(
        request.method == "GET" and request.url.path == f"/v1/sessions/{SESSION}"
        for request in requests
    )


async def test_sdk_tool_output_publishes_pr_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    SessionPrRegistry(SESSION).record(
        [PullRequestRef.from_url(URL)], relationship="created", source="sdk"
    )
    reports: list[dict] = []

    def receive(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            reports.append(json.loads(request.content))
        return httpx.Response(200, json={})

    async def resolve(*_args) -> AgentSpec:
        return AgentSpec(spec_version=1, name="pr-probe")

    frames = [
        _sse({"type": "response.created", "response": {"id": "response"}}),
        _sse(
            {
                "type": "response.output_item.done",
                "item": {"type": "function_call_output", "call_id": "create", "output": URL},
            }
        ),
        _sse({"type": "response.completed", "response": {"id": "response"}}),
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as server:
        app = create_runner_app(
            process_manager=_FakeProcessManager(_ScriptedHarnessClient(frames)),  # type: ignore[bad-argument-type]
            spec_resolver=resolve,
            server_client=server,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://runner"
        ) as client:
            response = await client.post(
                f"/v1/sessions/{SESSION}/events?stream=true",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "965906f5d9fb596610dda599a80faaee",
                    "content": [{"type": "input_text", "text": "Create a PR"}],
                    "harness": "openai-agents",
                },
            )
            assert response.status_code == 200
            assert "function_call_output" in response.text
    assert len(reports) == 1
    assert reports[0]["type"] == "external_session_prs"
    assert reports[0]["data"]["prs"][0]["url"] == URL


async def test_stream_reconnect_replays_then_retries_at_heartbeat(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("omnigent.runner.app._SESSION_STREAM_HEARTBEAT_S", 0.01)
    SessionPrRegistry("reconnect").record(
        [PullRequestRef.from_url(URL)], relationship="created", source="native_hook"
    )
    reports: list[dict] = []

    def receive(request: httpx.Request) -> httpx.Response:
        reports.append(json.loads(request.content))
        return httpx.Response(503 if len(reports) == 1 else 200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive), base_url="http://server"
    ) as server:
        app = create_runner_app(server_client=server)
        route = next(
            r for r in app.routes if getattr(r, "path", None) == "/v1/sessions/{session_id}/stream"
        )
        assert isinstance(route, APIRoute)
        response = await route.endpoint("reconnect")
        stream = response.body_iterator
        assert b"session.heartbeat" in await anext(stream)
        assert reports == []
        assert b"session.heartbeat" in await anext(stream)
        await stream.aclose()
    assert len(reports) == 2
    assert reports[0] == reports[1]
    assert reports[1]["data"]["prs"][0]["url"] == URL
