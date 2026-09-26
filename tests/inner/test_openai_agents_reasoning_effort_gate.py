"""Cover seeded skips and confirmed-rejection retries in the SDK proxy."""

from __future__ import annotations

from typing import Any

import httpx
import openai
import pytest

from omnigent.inner.openai_agents_sdk_executor import (
    _create_with_reasoning_effort_gate,
    _ReasoningBlockFilterStream,
    _wrap_client_for_reasoning_models,
)
from omnigent.llms.reasoning_effort_support import clear_learned_rejections


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    """Isolate the learned-rejection cache across tests."""
    clear_learned_rejections()


def _param_rejection() -> openai.BadRequestError:
    """Build the OpenAI client's xAI-style parameter rejection."""
    body = (
        b'{"error": {"message": "Argument not supported on this model: '
        b'reasoning_effort", "type": "invalid_request_error"}}'
    )
    response = httpx.Response(
        400,
        content=body,
        request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"),
    )
    return openai.BadRequestError("Error code: 400", response=response, body=None)


def _unrelated_400() -> openai.BadRequestError:
    """Build a 400 that must not trigger the fallback."""
    response = httpx.Response(
        400,
        content=b'{"error": {"message": "max_tokens is too large"}}',
        request=httpx.Request("POST", "https://api.x.ai/v1/chat/completions"),
    )
    return openai.BadRequestError("Error code: 400", response=response, body=None)


class _FakeCompletions:
    """Records ``create()`` kwargs; raises scripted errors first."""

    def __init__(self, errors: list[Exception] | None = None) -> None:
        """Queue per-call errors before succeeding."""
        self.calls: list[dict[str, Any]] = []
        self._errors = list(errors or [])

    async def create(self, **kwargs: Any) -> str:
        """Record the request and return a sentinel after queued errors."""
        self.calls.append(kwargs)
        if self._errors:
            raise self._errors.pop(0)
        return "ok"


async def test_seeded_model_never_sends_the_param() -> None:
    """A seeded (provider, model) rejection strips the param up front."""
    completions = _FakeCompletions()
    result = await _create_with_reasoning_effort_gate(
        completions,
        {"model": "xai/grok-4", "reasoning_effort": "medium", "messages": []},
        base_url="http://127.0.0.1:1/v1",
    )
    assert result == "ok"
    assert len(completions.calls) == 1
    assert "reasoning_effort" not in completions.calls[0]


async def test_bare_model_is_gated_via_base_url() -> None:
    """Without a ``provider/`` prefix, the client base URL names the provider."""
    completions = _FakeCompletions()
    await _create_with_reasoning_effort_gate(
        completions,
        {"model": "grok-4", "reasoning_effort": "low", "messages": []},
        base_url="https://api.x.ai/v1",
    )
    assert "reasoning_effort" not in completions.calls[0]


async def test_supported_model_keeps_the_param() -> None:
    """An accepting model gets the param, in a single call."""
    completions = _FakeCompletions()
    await _create_with_reasoning_effort_gate(
        completions,
        {"model": "xai/grok-3-mini", "reasoning_effort": "low", "messages": []},
        base_url="https://api.x.ai/v1",
    )
    assert len(completions.calls) == 1
    assert completions.calls[0]["reasoning_effort"] == "low"


async def test_unset_effort_passes_through_verbatim() -> None:
    """The SDK's omit sentinel is not a string and must not be touched."""
    completions = _FakeCompletions()
    sentinel = object()  # stands in for openai's NOT_GIVEN / omit
    await _create_with_reasoning_effort_gate(
        completions,
        {"model": "xai/grok-4", "reasoning_effort": sentinel, "messages": []},
        base_url="",
    )
    assert completions.calls[0]["reasoning_effort"] is sentinel


async def test_live_rejection_strips_retries_once_and_learns() -> None:
    """An unseeded model that rejects the param self-heals."""
    completions = _FakeCompletions(errors=[_param_rejection()])
    result = await _create_with_reasoning_effort_gate(
        completions,
        {"model": "acme/wombat-1", "reasoning_effort": "high", "messages": []},
        base_url="",
    )
    assert result == "ok"
    assert len(completions.calls) == 2
    assert completions.calls[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in completions.calls[1]

    # The rejection was learned: the next call skips the wasted round trip.
    completions_next = _FakeCompletions()
    await _create_with_reasoning_effort_gate(
        completions_next,
        {"model": "acme/wombat-1", "reasoning_effort": "high", "messages": []},
        base_url="",
    )
    assert len(completions_next.calls) == 1
    assert "reasoning_effort" not in completions_next.calls[0]


async def test_unrelated_400_reraises_without_retry() -> None:
    """A 400 about a different parameter is not a capability rejection."""
    completions = _FakeCompletions(errors=[_unrelated_400()])
    with pytest.raises(openai.BadRequestError):
        await _create_with_reasoning_effort_gate(
            completions,
            {"model": "acme/wombat-1", "reasoning_effort": "high", "messages": []},
            base_url="",
        )
    assert len(completions.calls) == 1


async def test_failed_stripped_retry_learns_nothing() -> None:
    """A retry that also fails must not durably disable the param."""
    completions = _FakeCompletions(errors=[_param_rejection(), _unrelated_400()])
    with pytest.raises(openai.BadRequestError):
        await _create_with_reasoning_effort_gate(
            completions,
            {"model": "acme/wombat-2", "reasoning_effort": "high", "messages": []},
            base_url="",
        )
    assert len(completions.calls) == 2

    # Nothing learned: the next call still sends the param optimistically.
    completions_next = _FakeCompletions()
    await _create_with_reasoning_effort_gate(
        completions_next,
        {"model": "acme/wombat-2", "reasoning_effort": "high", "messages": []},
        base_url="",
    )
    assert completions_next.calls[0]["reasoning_effort"] == "high"


class _FakeStream:
    """Minimal async iterator standing in for an ``AsyncStream``."""

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> None:
        raise StopAsyncIteration


class _FakeStreamingCompletions:
    """Returns a fake stream and records kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeStream:
        """Record the request and return a fresh fake stream."""
        self.calls.append(kwargs)
        return _FakeStream()


class _FakeChat:
    """Bare ``chat`` namespace exposing ``completions``."""

    def __init__(self, completions: Any) -> None:
        """Expose the supplied completions object."""
        self.completions = completions


class _FakeClient:
    """Bare client shape for ``_wrap_client_for_reasoning_models``."""

    def __init__(self, completions: Any, base_url: str) -> None:
        """Expose completions and a provider base URL."""
        self.chat = _FakeChat(completions)
        self.base_url = base_url


async def test_wrapped_client_gates_and_keeps_stream_filtering() -> None:
    """The installed proxy gates the param and still wraps streams."""
    completions = _FakeStreamingCompletions()
    client = _wrap_client_for_reasoning_models(
        _FakeClient(completions, base_url="https://api.x.ai/v1")  # type: ignore[arg-type]
    )
    result = await client.chat.completions.create(
        model="grok-4", reasoning_effort="medium", stream=True, messages=[]
    )
    assert isinstance(result, _ReasoningBlockFilterStream)
    assert len(completions.calls) == 1
    assert "reasoning_effort" not in completions.calls[0]
