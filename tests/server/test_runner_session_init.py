"""Tests for server-owned runner session initialization coordination."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.server.runner_session_init import RunnerSessionInitializer


class _Registry:
    def __init__(self) -> None:
        self.connection: object | None = object()

    def get(self, _runner_id: str) -> object | None:
        return self.connection


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.status_code = 201

    async def post(self, _path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(kwargs["json"])
        self.entered.set()
        await self.release.wait()
        return httpx.Response(self.status_code, json={"status": "initialized"})


def _conversation() -> Conversation:
    return Conversation(
        id="conv_init",
        created_at=10,
        updated_at=11,
        root_conversation_id="conv_init",
        agent_id="agent_init",
        runner_id="runner_init",
        workspace="/tmp/workspace",
        labels={"example": "value"},
    )


@pytest.mark.asyncio
async def test_initializer_shares_result_for_one_tunnel_generation() -> None:
    registry = _Registry()
    client = _Client()
    initializer = RunnerSessionInitializer(  # type: ignore[arg-type]
        registry,
        server_version="0.6.0.dev0",
    )
    conversation = _conversation()

    first = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await client.entered.wait()
    second = asyncio.create_task(initializer.initialize(conversation, client, timeout=10))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    client.release.set()
    first_response, second_response = await asyncio.gather(first, second)

    assert first_response is second_response
    assert len(client.calls) == 1
    assert client.calls[0]["session_init"]["snapshot"]["workspace"] == "/tmp/workspace"

    cached = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert cached is first_response
    assert len(client.calls) == 1

    initializer.invalidate_runner("runner_init")
    await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_initializer_evicts_rejected_result_for_retry() -> None:
    registry = _Registry()
    client = _Client()
    client.release.set()
    client.status_code = 503
    initializer = RunnerSessionInitializer(  # type: ignore[arg-type]
        registry,
        server_version="0.6.0.dev0",
    )
    conversation = _conversation()

    first = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]
    second = await initializer.initialize(conversation, client, timeout=10)  # type: ignore[arg-type]

    assert first.status_code == second.status_code == 503
    assert len(client.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_ready"),
    [
        ({"session_init_protocol_version": 2, "terminal_ready": True}, True),
        ({}, False),
    ],
    ids=["current-runner", "legacy-runner"],
)
async def test_session_init_readiness_is_explicit_and_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    expected_ready: bool,
) -> None:
    """Only a current runner response suppresses the terminal ensure."""
    from omnigent.server.routes import sessions as sessions_routes

    async def _noop_recovered(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_routes, "_publish_runner_recovered_status", _noop_recovered)

    class _Initializer:
        async def initialize(self, *_args: Any, **_kwargs: Any) -> httpx.Response:
            return httpx.Response(
                201,
                json=payload,
                request=httpx.Request("POST", "http://runner/v1/sessions"),
            )

    ready = await sessions_routes._ensure_runner_session_initialized(
        "conv_init",
        _conversation(),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        initializer=_Initializer(),  # type: ignore[arg-type]
    )

    assert ready is expected_ready
