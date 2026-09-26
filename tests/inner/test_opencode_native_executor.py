"""Tests for the OpenCode native executor turn lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.opencode_native import http_transport as transport_mod
from omnigent.harnesses.opencode_native.bridge import (
    OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    OpenCodeNativeBridgeState,
    read_bridge_state,
    update_model_override,
    write_bridge_state,
)
from omnigent.harnesses.opencode_native.client import OpenCodeClient
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.opencode_native_executor import OpenCodeNativeExecutor

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="  # noqa: E501
_PNG_DATA_URI = f"data:image/png;base64,{_PNG_B64}"


class _FakeServer:
    """Records the requests a fake OpenCode v2 server receives."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = {}
        self.requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/interrupt"):
            return httpx.Response(200, json={"interrupted": True})
        if request.url.path.endswith("/model"):
            return httpx.Response(204)
        return httpx.Response(200, json={"data": {"id": "msg_1", "type": "user"}})


def _prompts(server: _FakeServer) -> list[dict[str, Any]]:
    return [body for _, path, body in server.requests if path == "/api/session/ses_1/prompt"]


@pytest.fixture
def fake_server(monkeypatch: pytest.MonkeyPatch) -> _FakeServer:
    """Patch the transport's client factory to talk to a fake server."""
    server = _FakeServer()

    def fake_client_for_state(
        *, base_url: str, auth_secret: str | None, directory: str | None = None
    ) -> OpenCodeClient:
        mock = httpx.AsyncClient(
            base_url="http://opencode.test",
            transport=httpx.MockTransport(server.handler),
        )
        return OpenCodeClient("http://opencode.test", client=mock)

    monkeypatch.setattr(transport_mod, "client_for_state", fake_client_for_state)
    return server


def _seed_state(
    bridge_dir: Path,
    *,
    session_id: str = "conv_1",
    opencode_session_id: str = "ses_1",
    model_override: str | None = None,
    last_applied_model: str | None = None,
) -> None:
    write_bridge_state(
        bridge_dir,
        OpenCodeNativeBridgeState(
            session_id=session_id,
            server_base_url="http://127.0.0.1:49231",
            opencode_session_id=opencode_session_id,
            auth_secret="pw",
            model_override=model_override,
            last_applied_model=last_applied_model,
        ),
    )


def _executor(
    bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, *, request_id: str = "conv_1"
) -> OpenCodeNativeExecutor:
    monkeypatch.setenv(OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR, request_id)
    executor = OpenCodeNativeExecutor(bridge_dir=bridge_dir)
    executor._boot_poll_attempts = 1
    executor._boot_poll_delay = 0.0
    return executor


async def _run(executor: OpenCodeNativeExecutor, content: Any) -> list[Any]:
    events: list[Any] = []
    async for event in executor.run_turn([{"role": "user", "content": content}], [], ""):
        events.append(event)
    return events


async def test_run_turn_injects_prompt_and_completes(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "hello")
    assert [type(e) for e in events] == [TurnComplete]
    assert _prompts(fake_server) == [{"text": "hello", "delivery": "steer"}]


async def test_run_turn_with_blocks(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(
        executor,
        [
            {"type": "input_text", "text": "what is this?"},
            {"type": "input_image", "image_url": _PNG_DATA_URI},
        ],
    )
    assert [type(e) for e in events] == [TurnComplete]
    body = _prompts(fake_server)[0]
    assert body["text"] == "what is this?"
    assert body["files"] == [{"uri": _PNG_DATA_URI}]
    # No inline base64 in the text.
    assert _PNG_B64 not in body["text"]


async def test_run_turn_switches_model_before_prompt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override reaches OpenCode via POST /model before the first prompt."""
    _seed_state(tmp_path, model_override="anthropic/claude-opus-4")
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "hello")
    assert [type(e) for e in events] == [TurnComplete]
    assert [path for _, path, _ in fake_server.requests] == [
        "/api/session/ses_1/model",
        "/api/session/ses_1/prompt",
    ]
    assert fake_server.requests[0][2] == {
        "model": {"id": "claude-opus-4", "providerID": "anthropic"}
    }
    assert "model" not in _prompts(fake_server)[0]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.last_applied_model == "anthropic/claude-opus-4"


async def test_run_turn_skips_model_switch_when_already_applied(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(
        tmp_path,
        model_override="anthropic/claude-opus-4",
        last_applied_model="anthropic/claude-opus-4",
    )
    executor = _executor(tmp_path, monkeypatch)
    await _run(executor, "hello")
    assert [path for _, path, _ in fake_server.requests] == ["/api/session/ses_1/prompt"]


async def test_run_turn_switches_again_after_override_changes(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path, model_override="acme/one")
    executor = _executor(tmp_path, monkeypatch)
    await _run(executor, "first")
    await _run(executor, "again")
    assert update_model_override(tmp_path, "acme/two") is True
    await _run(executor, "second")
    model_bodies = [body for _, path, body in fake_server.requests if path.endswith("/model")]
    assert model_bodies == [
        {"model": {"id": "one", "providerID": "acme"}},
        {"model": {"id": "two", "providerID": "acme"}},
    ]


async def test_run_turn_without_override_never_switches_model(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    await _run(executor, "hello")
    assert [path for _, path, _ in fake_server.requests] == ["/api/session/ses_1/prompt"]


async def test_run_turn_model_switch_failure_errors_without_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeServer()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model"):
            server.requests.append(("POST", request.url.path, {}))
            return httpx.Response(400, json={"_tag": "InvalidRequestError", "message": "bad"})
        return server.handler(request)

    def fake_client_for_state(
        *, base_url: str, auth_secret: str | None, directory: str | None = None
    ) -> OpenCodeClient:
        mock = httpx.AsyncClient(
            base_url="http://opencode.test", transport=httpx.MockTransport(handler)
        )
        return OpenCodeClient("http://opencode.test", client=mock)

    monkeypatch.setattr(transport_mod, "client_for_state", fake_client_for_state)
    _seed_state(tmp_path, model_override="acme/missing")
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "hello")
    assert [type(e) for e in events] == [ExecutorError]
    assert _prompts(server) == []
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.last_applied_model is None


async def test_run_turn_no_user_content_errors(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "")
    assert [type(e) for e in events] == [ExecutorError]
    assert fake_server.requests == []


async def test_run_turn_missing_bridge_state_errors(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No state written; resolve never returns a session id.
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "hi")
    assert [type(e) for e in events] == [ExecutorError]


async def test_run_turn_session_mismatch_errors(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path, session_id="conv_OTHER")
    executor = _executor(tmp_path, monkeypatch, request_id="conv_1")
    events = await _run(executor, "hi")
    assert [type(e) for e in events] == [ExecutorError]


async def test_interrupt_calls_interrupt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    assert await executor.interrupt_session("k") is True
    assert [path for _, path, _ in fake_server.requests] == ["/api/session/ses_1/interrupt"]


async def test_enqueue_message_injects_prompt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    assert await executor.enqueue_session_message("k", "steer me") is True
    assert [body["text"] for body in _prompts(fake_server)] == ["steer me"]


async def _run_with_system_prompt(
    executor: OpenCodeNativeExecutor, content: Any, system_prompt: str
) -> list[Any]:
    events: list[Any] = []
    async for event in executor.run_turn(
        [{"role": "user", "content": content}], [], system_prompt
    ):
        events.append(event)
    return events


async def test_run_turn_never_sends_system_field(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2 has no per-prompt system field; instructions ship in the config."""
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    events = await _run_with_system_prompt(executor, "hello", "Be concise.")
    assert [type(e) for e in events] == [TurnComplete]
    assert "system" not in _prompts(fake_server)[0]
    assert await executor.enqueue_session_message("k", "later") is True
    assert "system" not in _prompts(fake_server)[1]


def test_capabilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    assert executor.supports_streaming() is False
    assert executor.handles_tools_internally() is True
    assert executor.supports_live_message_queue() is True


def test_harness_create_app_builds_fastapi() -> None:
    """The ``opencode-native`` harness module builds a FastAPI app (lazy executor)."""
    from fastapi import FastAPI

    from omnigent.inner.opencode_native_harness import create_app

    assert isinstance(create_app(), FastAPI)


def test_harness_executor_factory_builds_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The harness executor factory constructs an executor from the spawn env."""
    from omnigent.harnesses.opencode_native.bridge import OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR
    from omnigent.inner.opencode_native_harness import _build_opencode_native_executor

    monkeypatch.setenv(OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR, str(tmp_path))
    assert isinstance(_build_opencode_native_executor(), OpenCodeNativeExecutor)
