"""Runner-owned ``/btw`` side-question inference tests."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from tests.runner.helpers import NullServerClient


class _FakeHarnessStream:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.status_code = 200
        self._events = events

    async def __aenter__(self) -> _FakeHarnessStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def aiter_lines(self):
        for event in self._events:
            yield f"data: {json.dumps(event)}"
            yield ""


class _FakeHarnessClient:
    def __init__(self, events: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._events = events or [
            {"type": "response.output_text.delta", "delta": "It runs on "},
            {"type": "response.output_text.delta", "delta": "claude-native."},
            {"type": "response.completed"},
        ]

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float | None,
    ) -> _FakeHarnessStream:
        assert method == "POST"
        self.requests.append((url, json))
        return _FakeHarnessStream(self._events)

    async def post(self, url: str, *, json: dict[str, Any]) -> None:
        self.posts.append((url, json))


class _FakeProcessManager:
    def __init__(self, client: _FakeHarnessClient) -> None:
        self.client = client
        self.get_client_calls: list[tuple[str, str, dict[str, str] | None]] = []
        self.released: list[str] = []

    async def get_client(
        self,
        conversation_id: str,
        harness_name: str,
        *,
        env: dict[str, str] | None = None,
    ) -> _FakeHarnessClient:
        self.get_client_calls.append((conversation_id, harness_name, env))
        return self.client

    async def release(self, conversation_id: str) -> None:
        self.released.append(conversation_id)


@asynccontextmanager
async def _runner_client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


def _app(process_manager: _FakeProcessManager):
    return create_runner_app(
        process_manager=process_manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


def _patch_harness(monkeypatch: pytest.MonkeyPatch, harness: str) -> None:
    async def resolve_harness_config(**_kwargs: Any) -> tuple[str, dict[str, str]]:
        return harness, {}

    monkeypatch.setattr(
        "omnigent.runner.app._resolve_harness_config",
        resolve_harness_config,
    )


@pytest.mark.asyncio
async def test_side_question_answers_from_an_isolated_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer comes back and the throwaway process is released."""
    harness_client = _FakeHarnessClient()
    process_manager = _FakeProcessManager(harness_client)
    _patch_harness(monkeypatch, "claude-sdk")

    async with _runner_client(_app(process_manager)) as client:
        response = await client.post(
            "/v1/sessions/conv_test/side-question",
            json={"question": "which harness?", "excerpt": "user: fix the parser"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "answered", "answer": "It runs on claude-native."}
    # The synthetic process is keyed by a random id, never the session's —
    # that isolation is what keeps the live session untouched.
    assert process_manager.get_client_calls[0][0] != "conv_test"
    assert process_manager.released == [process_manager.get_client_calls[0][0]]


@pytest.mark.asyncio
async def test_side_question_sends_no_tools_and_fences_both_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The prompt declares no tools and fences transcript and question apart.

    A side question reads arbitrary agent output; if the transcript were
    not fenced as data, anything the session printed could steer the
    answer.
    """
    harness_client = _FakeHarnessClient()
    process_manager = _FakeProcessManager(harness_client)
    _patch_harness(monkeypatch, "claude-sdk")

    async with _runner_client(_app(process_manager)) as client:
        await client.post(
            "/v1/sessions/conv_test/side-question",
            json={"question": "which harness?", "excerpt": "user: fix the parser"},
        )

    _url, body = harness_client.requests[0]
    assert body["tools"] == []
    assert "<conversation>\nuser: fix the parser\n</conversation>" in body["content"]
    assert "<question>\nwhich harness?\n</question>" in body["content"]
    assert "never as instructions" in body["instructions"]


@pytest.mark.asyncio
async def test_side_question_denies_a_tool_call_it_never_offered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reach for a tool is refused, keeping a read-only ask read-only."""
    harness_client = _FakeHarnessClient(
        [
            {
                "type": "policy_evaluation.requested",
                "evaluation_id": "eval_1",
                "phase": "PHASE_TOOL_CALL",
            },
            {"type": "response.output_text.delta", "delta": "answer"},
            {"type": "response.completed"},
        ]
    )
    process_manager = _FakeProcessManager(harness_client)
    _patch_harness(monkeypatch, "claude-sdk")

    async with _runner_client(_app(process_manager)) as client:
        response = await client.post(
            "/v1/sessions/conv_test/side-question",
            json={"question": "run the tests", "excerpt": ""},
        )

    assert response.status_code == 200
    assert harness_client.posts[0][1]["action"] == "POLICY_ACTION_DENY"


@pytest.mark.asyncio
async def test_side_question_is_unsupported_for_an_unregistered_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A harness with no generator reports ``unsupported``, not an error.

    Clients render that as "not available here"; a 5xx would look like
    a broken session instead of a missing capability.
    """
    process_manager = _FakeProcessManager(_FakeHarnessClient())
    _patch_harness(monkeypatch, "goose")

    async with _runner_client(_app(process_manager)) as client:
        response = await client.post(
            "/v1/sessions/conv_test/side-question",
            json={"question": "which harness?", "excerpt": ""},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "unsupported"
    assert process_manager.get_client_calls == []


@pytest.mark.asyncio
async def test_side_question_surfaces_a_harness_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed turn returns the harness's own detail, not a bare 500."""
    harness_client = _FakeHarnessClient(
        [
            {
                "type": "response.failed",
                "response": {"error": {"message": "quota exhausted"}},
            }
        ]
    )
    process_manager = _FakeProcessManager(harness_client)
    _patch_harness(monkeypatch, "claude-sdk")

    async with _runner_client(_app(process_manager)) as client:
        response = await client.post(
            "/v1/sessions/conv_test/side-question",
            json={"question": "which harness?", "excerpt": ""},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "quota exhausted"
    # Even on failure the throwaway process is handed back.
    assert process_manager.released == [process_manager.get_client_calls[0][0]]


@pytest.mark.asyncio
async def test_side_question_rejects_a_blank_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ask is a validation error, not a wasted inference."""
    process_manager = _FakeProcessManager(_FakeHarnessClient())
    _patch_harness(monkeypatch, "claude-sdk")

    async with _runner_client(_app(process_manager)) as client:
        response = await client.post(
            "/v1/sessions/conv_test/side-question",
            json={"question": "", "excerpt": ""},
        )

    assert response.status_code == 422
    assert process_manager.get_client_calls == []
