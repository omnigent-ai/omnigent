"""Retry behavior for lazy ``Client.responses.create(stream=True)`` streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from omnigent.llms.client import Client
from omnigent.llms.errors import RetryableLLMError
from omnigent.llms.routing import RoutedModel
from omnigent.llms.types import ResponseStreamEvent, ResponseTextDeltaEvent
from omnigent.spec.types import RetryPolicy

_SUCCESS_SSE = (
    b'event: response.output_text.delta\ndata: {"delta":"ok"}\n\n'
    b"event: response.completed\n"
    b'data: {"response":{"model":"test-model","output":[]}}\n\n'
)
_DELTA_SSE = b'event: response.output_text.delta\ndata: {"delta":"partial"}\n\n'


class _FailingBody(httpx.AsyncByteStream):
    """HTTPX body that can fail before or after yielding bytes."""

    def __init__(
        self,
        chunks: list[bytes],
        error: Exception | None = None,
        *,
        entered: asyncio.Event | None = None,
        block: bool = False,
    ) -> None:
        self._chunks = chunks
        self._error = error
        self._entered = entered
        self._block = block
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._entered is not None:
            self._entered.set()
        if self._block:
            await asyncio.Future()
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error

    async def aclose(self) -> None:
        self.closed = True


def _install_openai_http(
    monkeypatch: pytest.MonkeyPatch,
    actions: list[object],
) -> list[httpx.Request]:
    """Route each OpenAI stream attempt through a real HTTPX mock transport."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        action = actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if isinstance(action, httpx.AsyncByteStream):
            return httpx.Response(200, request=request, stream=action)
        assert isinstance(action, tuple)
        status, body = action
        return httpx.Response(status, request=request, content=body)

    transport = httpx.MockTransport(handler)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return requests


def _patch_openai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the public client through the OpenAI Responses adapter."""
    from omnigent.llms.adapters.openai import OpenAIAdapter

    adapter = OpenAIAdapter(base_url="https://llm.test/v1")
    monkeypatch.setattr(
        "omnigent.llms.client.parse_model_string",
        lambda _model: RoutedModel(provider="openai", model="test-model"),
    )
    monkeypatch.setattr("omnigent.llms.client.get_adapter", lambda _provider: adapter)


def _retry_policy() -> RetryPolicy:
    """Use two retries with near-zero backoff for deterministic tests."""
    return RetryPolicy(max_retries=2, backoff_base_s=0.001, backoff_max_s=0.001)


async def _collect(result: object) -> list[ResponseStreamEvent]:
    """Collect one public streaming result."""
    assert hasattr(result, "__aiter__")
    return [event async for event in result]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_stream_retry_503_then_success_before_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream-open 503 is retried lazily and recovers before output."""
    _patch_openai_client(monkeypatch)
    requests = _install_openai_http(
        monkeypatch,
        [(503, b"busy"), (200, _SUCCESS_SSE)],
    )
    result = await Client().responses.create(
        input=[{"role": "user", "content": "hi"}],
        model="openai/test-model",
        stream=True,
        retry=_retry_policy(),
    )
    assert requests == []  # stream creation remains lazy until iteration
    events = await _collect(result)

    assert [event.delta for event in events if isinstance(event, ResponseTextDeltaEvent)] == ["ok"]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_stream_retry_transport_error_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-output transport failure is retried through a fresh HTTP stream."""
    _patch_openai_client(monkeypatch)
    requests = _install_openai_http(
        monkeypatch,
        [httpx.ReadError("socket reset"), (200, _SUCCESS_SSE)],
    )
    result = await Client().responses.create(
        input=[{"role": "user", "content": "hi"}],
        model="openai/test-model",
        stream=True,
        retry=_retry_policy(),
    )
    events = await _collect(result)

    assert [event.delta for event in events if isinstance(event, ResponseTextDeltaEvent)] == ["ok"]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_stream_retry_none_preserves_single_lazy_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a policy, streaming keeps its one-attempt lazy behavior."""
    _patch_openai_client(monkeypatch)
    requests = _install_openai_http(monkeypatch, [(503, b"busy")])
    result = await Client().responses.create(
        input=[{"role": "user", "content": "hi"}],
        model="openai/test-model",
        stream=True,
    )
    assert requests == []
    with pytest.raises(httpx.HTTPStatusError):
        await _collect(result)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_stream_failure_after_partial_output_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream error after a typed event propagates without replaying output."""
    _patch_openai_client(monkeypatch)
    body = _FailingBody(
        [_DELTA_SSE],
        httpx.ReadError("socket reset"),
    )
    requests = _install_openai_http(monkeypatch, [body])
    result = await Client().responses.create(
        input=[{"role": "user", "content": "hi"}],
        model="openai/test-model",
        stream=True,
        retry=_retry_policy(),
    )

    with pytest.raises(RetryableLLMError):
        await _collect(result)
    assert len(requests) == 1
    assert body.closed


@pytest.mark.asyncio
async def test_stream_close_error_does_not_mask_original_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``aclose`` cannot replace the original stream error."""
    from omnigent.llms.client import _ResponsesNamespace

    class FailingStream:
        closed = False

        def __aiter__(self) -> AsyncIterator[ResponseStreamEvent]:
            return self

        async def __anext__(self) -> ResponseStreamEvent:
            raise httpx.ReadError("original stream failure")

        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("close failure")

    stream = FailingStream()

    async def fake_do_create(self: object, **kwargs: Any) -> object:
        del self, kwargs
        return stream

    monkeypatch.setattr(_ResponsesNamespace, "_do_create", fake_do_create)
    policy = RetryPolicy(max_retries=0, backoff_base_s=0.001, backoff_max_s=0.001)
    result = await Client().responses.create(
        input=[{"role": "user", "content": "hi"}],
        model="fake/test-model",
        stream=True,
        retry=policy,
    )

    with pytest.raises(RetryableLLMError, match="original stream failure"):
        await _collect(result)
    assert stream.closed


@pytest.mark.asyncio
async def test_stream_retry_exhaustion_uses_one_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three pre-output 503s exhaust the configured total attempt budget."""
    _patch_openai_client(monkeypatch)
    requests = _install_openai_http(
        monkeypatch,
        [(503, b"busy"), (503, b"busy"), (503, b"busy")],
    )
    result = await Client().responses.create(
        input=[{"role": "user", "content": "hi"}],
        model="openai/test-model",
        stream=True,
        retry=_retry_policy(),
    )

    with pytest.raises(RetryableLLMError):
        await _collect(result)
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_stream_retry_cancellation_closes_failed_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation while opening/reading a stream is not swallowed."""
    _patch_openai_client(monkeypatch)
    entered = asyncio.Event()
    body = _FailingBody([], entered=entered, block=True)
    _install_openai_http(monkeypatch, [body])
    result = await Client().responses.create(
        input=[{"role": "user", "content": "hi"}],
        model="openai/test-model",
        stream=True,
        retry=_retry_policy(),
    )

    task = asyncio.create_task(_collect(result))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed


@pytest.mark.asyncio
async def test_factory_and_stream_failures_share_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Factory and pre-output iterator failures consume one total budget."""
    calls = 0

    async def chunks() -> AsyncIterator[dict[str, Any]]:
        yield {"choices": [{"delta": {"content": "ok"}}]}

    async def failing_chunks() -> AsyncIterator[dict[str, Any]]:
        raise httpx.ReadError("stream reset")
        yield {}  # pragma: no cover

    class Adapter:
        async def chat_completions(self, *args: Any, **kwargs: Any) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise httpx.TimeoutException("factory timeout")
            if calls == 2:
                return failing_chunks()
            return chunks()

    adapter = Adapter()
    routed = RoutedModel(provider="fake", model="test-model")
    monkeypatch.setattr("omnigent.llms.client.parse_model_string", lambda _model: routed)
    monkeypatch.setattr("omnigent.llms.client.get_adapter", lambda _provider: adapter)
    result = await Client().responses.create(
        input=[{"role": "user", "content": "hi"}],
        model="fake/test-model",
        stream=True,
        retry=_retry_policy(),
    )
    events = await _collect(result)

    assert [event.delta for event in events if isinstance(event, ResponseTextDeltaEvent)] == ["ok"]
    assert calls == 3
