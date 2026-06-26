"""Tests for the OpenCode native executor turn lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import opencode_http_transport as transport_mod
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.opencode_native_executor import OpenCodeNativeExecutor
from omnigent.opencode_native_bridge import (
    OPENCODE_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    OpenCodeNativeBridgeState,
    write_bridge_state,
)
from omnigent.opencode_native_client import OpenCodeClient

_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="  # noqa: E501
_PNG_DATA_URI = f"data:image/png;base64,{_PNG_B64}"


class _FakeServer:
    """Records the requests a fake OpenCode HTTP server receives."""

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
        if request.url.path.endswith("/abort"):
            return httpx.Response(200, json=True)
        return httpx.Response(200, json={})


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
) -> None:
    write_bridge_state(
        bridge_dir,
        OpenCodeNativeBridgeState(
            session_id=session_id,
            server_base_url="http://127.0.0.1:49231",
            opencode_session_id=opencode_session_id,
            auth_secret="pw",
            model_override=model_override,
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
    prompt_reqs = [r for r in fake_server.requests if r[1].endswith("/prompt_async")]
    assert len(prompt_reqs) == 1
    parts = prompt_reqs[0][2]["parts"]
    assert parts == [{"type": "text", "text": "hello"}]


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
    parts = fake_server.requests[0][2]["parts"]
    text_parts = [p for p in parts if p["type"] == "text"]
    file_parts = [p for p in parts if p["type"] == "file"]
    assert text_parts[0]["text"] == "what is this?"
    assert len(file_parts) == 1
    assert file_parts[0]["url"] == _PNG_DATA_URI
    assert file_parts[0]["mime"] == "image/png"
    # No inline base64 in any text part.
    assert all(_PNG_B64 not in p.get("text", "") for p in parts)


async def test_run_turn_pins_resolved_model_on_prompt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's resolved model reaches the prompt body from turn one.

    OpenCode's session-create body cannot carry a model, so the override is
    applied per prompt as ``model: {providerID, modelID}``. This pins the
    first injected turn (which OpenCode then persists as the session
    default), so the override governs the run from the start — not only a
    later web turn.
    """
    _seed_state(tmp_path, model_override="anthropic/claude-opus-4")
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "hello")
    assert [type(e) for e in events] == [TurnComplete]
    prompt_reqs = [r for r in fake_server.requests if r[1].endswith("/prompt_async")]
    assert len(prompt_reqs) == 1
    body = prompt_reqs[0][2]
    assert body["model"] == {"providerID": "anthropic", "modelID": "claude-opus-4"}


async def test_run_turn_omits_model_when_no_override(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no model_override the prompt carries no model (OpenCode default)."""
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    await _run(executor, "hello")
    prompt_reqs = [r for r in fake_server.requests if r[1].endswith("/prompt_async")]
    assert "model" not in prompt_reqs[0][2]


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


async def test_interrupt_calls_abort(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    assert await executor.interrupt_session("k") is True
    abort_reqs = [r for r in fake_server.requests if r[1].endswith("/abort")]
    assert len(abort_reqs) == 1


async def test_enqueue_message_injects_prompt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    assert await executor.enqueue_session_message("k", "steer me") is True
    prompt_reqs = [r for r in fake_server.requests if r[1].endswith("/prompt_async")]
    assert prompt_reqs[0][2]["parts"] == [{"type": "text", "text": "steer me"}]


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
    from omnigent.inner.opencode_native_harness import _build_opencode_native_executor
    from omnigent.opencode_native_bridge import OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR

    monkeypatch.setenv(OPENCODE_NATIVE_BRIDGE_DIR_ENV_VAR, str(tmp_path))
    assert isinstance(_build_opencode_native_executor(), OpenCodeNativeExecutor)
