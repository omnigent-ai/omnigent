"""
Tests for the standalone LLM client retry logic (llms/client.py).

Covers the public ``Client().responses.create(retry=...)`` interface,
verifying that transient failures are retried with backoff and permanent
failures surface immediately. All tests are async because the client
methods are async.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from omnigent.llms.client import Client
from omnigent.llms.errors import (
    ContextWindowExceededError,
    LLMErrorDetail,
    PermanentLLMError,
    RetryableLLMError,
)
from omnigent.llms.types import (
    MessageOutput,
    OutputText,
    Response,
)
from omnigent.spec.types import RetryPolicy

# ── Helpers ──────────────────────────────────────────────────


@dataclass
class _SleepTracker:
    """
    Tracks calls to ``asyncio.sleep`` during retry backoff.

    :param calls: List of sleep durations passed to each call.
    """

    calls: list[float]


def _make_response() -> Response:
    """
    Build a minimal ``Response`` for successful-call assertions.

    :returns: A ``Response`` with a single text output.
    """
    return Response(
        output=[MessageOutput(content=[OutputText(text="Hello")])],
        model="test-model",
    )


class _MockAdapter:
    """
    Fake adapter whose ``chat_completions`` is async and returns
    a preconfigured value or raises a preconfigured exception.

    Use ``return_value`` for a single fixed return, or
    ``side_effect`` for a list of values / exception to cycle
    through (list items are consumed in order; a bare exception is
    raised on every call).

    :param return_value: Value returned by every call when
        ``side_effect`` is ``None``.
    :param side_effect: A list of return values / exceptions, or
        a single exception raised on every call. When a list, each
        call pops the first item.
    """

    def __init__(
        self,
        *,
        return_value: Any = None,
        side_effect: list[Any] | Exception | None = None,
    ) -> None:
        """
        Initialize the mock adapter.

        :param return_value: Fixed return value for all calls.
        :param side_effect: List of return-values/exceptions or a
            single exception.
        """
        self.return_value = return_value
        self.side_effect = side_effect
        self.call_count = 0

    async def chat_completions(self, *args: Any, **kwargs: Any) -> Any:
        """
        Async mock of ``BaseAdapter.chat_completions()``.

        :param args: Positional arguments (ignored).
        :param kwargs: Keyword arguments (ignored).
        :returns: The configured return value.
        :raises: The configured side-effect exception.
        """
        self.call_count += 1
        if self.side_effect is not None:
            if isinstance(self.side_effect, list):
                item = self.side_effect.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item
            # Single exception — raise on every call
            raise self.side_effect
        return self.return_value


def _patch_client_deps(
    monkeypatch: pytest.MonkeyPatch,
    mock_adapter: _MockAdapter,
) -> _SleepTracker:
    """
    Patch all external dependencies of ``Client().responses.create()``
    so that calls route through ``mock_adapter.chat_completions``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param mock_adapter: A :class:`_MockAdapter` whose
        ``chat_completions`` controls call success/failure.
    :returns: A :class:`_SleepTracker` recording backoff sleep calls.
    """
    # Route model parsing to a fake routed model
    routed = MagicMock(provider="test", model="test-model")
    monkeypatch.setattr(
        "omnigent.llms.client.parse_model_string",
        lambda model: routed,
    )

    # Return the mock adapter (not OpenAIAdapter, so we hit
    # the chat_completions path instead of responses_create)
    monkeypatch.setattr(
        "omnigent.llms.client.get_adapter",
        lambda provider: mock_adapter,
    )

    # Stub the responses-to-chat conversion helpers
    monkeypatch.setattr(
        "omnigent.llms.client.responses_input_to_chat_messages",
        lambda input, instructions: [{"role": "user", "content": "test"}],
    )
    monkeypatch.setattr(
        "omnigent.llms.client.chat_response_to_response",
        lambda result: _make_response(),
    )

    # Capture retry-backoff sleep calls via the _sleep indirection
    # so the real asyncio.sleep is not patched globally.
    tracker = _SleepTracker(calls=[])

    async def _fake_sleep(duration: float) -> None:
        """
        Record the sleep duration without blocking.

        :param duration: The sleep duration in seconds.
        """
        tracker.calls.append(duration)

    monkeypatch.setattr(
        "omnigent.llms.client._sleep",
        _fake_sleep,
    )

    return tracker


def _default_create_kwargs() -> dict[str, Any]:
    """
    Minimal kwargs for ``Client().responses.create()``.

    :returns: Dict with required ``input`` and ``model`` keys.
    """
    return {
        "input": [{"role": "user", "content": "hi"}],
        "model": "test/test-model",
    }


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def retry_config() -> RetryPolicy:
    """
    A retry config with 3 total attempts (1 initial + 2 retries)
    and fast backoff for testing. ``max_retries`` is the
    "retries beyond the first attempt" — see RetryPolicy.
    """
    return RetryPolicy(
        max_retries=2,
        backoff_base_s=2.0,
        backoff_max_s=30.0,
    )


# ── Tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_without_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``retry=None``, the call succeeds normally without retry
    wrapping.
    """
    mock_adapter = _MockAdapter(return_value={"id": "test"})
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    result = await Client().responses.create(
        **_default_create_kwargs(),
    )

    # The response should contain the expected text from the mock
    # conversion; failure means the non-retry path is broken.
    assert isinstance(result, Response)
    assert result.output[0].content[0].text == "Hello"

    # No backoff sleeps should occur when retry is disabled.
    assert tracker.calls == []

    # Adapter should be called exactly once.
    assert mock_adapter.call_count == 1


@pytest.mark.asyncio
async def test_create_with_retry_success_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    With retry config, first-attempt success works and no backoff
    sleep occurs.
    """
    mock_adapter = _MockAdapter(return_value={"id": "test"})
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    result = await Client().responses.create(
        **_default_create_kwargs(),
        retry=retry_config,
    )

    # Successful first attempt returns the converted response.
    assert isinstance(result, Response)
    assert result.output[0].content[0].text == "Hello"

    # No backoff sleep when the first attempt succeeds.
    assert tracker.calls == []

    # Only one call to the adapter — no retries needed.
    assert mock_adapter.call_count == 1


@pytest.mark.asyncio
async def test_create_with_retry_timeout_then_success(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    Timeout on first attempt triggers a retry; second attempt
    succeeds.
    """
    # First call times out, second call succeeds
    mock_adapter = _MockAdapter(
        side_effect=[
            httpx.TimeoutException("timeout"),
            {"id": "test"},
        ],
    )
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    result = await Client().responses.create(
        **_default_create_kwargs(),
        retry=retry_config,
    )

    # The retry should recover and return a valid response.
    assert isinstance(result, Response)
    assert result.output[0].content[0].text == "Hello"

    # Exactly one backoff sleep between the failed first attempt
    # and the successful second attempt.
    assert len(tracker.calls) == 1

    # Two adapter calls total: one timeout, one success.
    assert mock_adapter.call_count == 2


@pytest.mark.asyncio
async def test_create_with_retry_http_429_then_success(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    Rate limit (429) on first attempt triggers retry; second
    attempt succeeds.
    """
    http_429 = httpx.HTTPStatusError(
        "rate limited",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(429),
    )
    mock_adapter = _MockAdapter(
        side_effect=[http_429, {"id": "test"}],
    )
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    result = await Client().responses.create(
        **_default_create_kwargs(),
        retry=retry_config,
    )

    # Recovery after 429 should produce a valid response.
    assert isinstance(result, Response)
    assert result.output[0].content[0].text == "Hello"

    # One backoff sleep between the 429 and the successful retry.
    assert len(tracker.calls) == 1

    # Two adapter calls: one 429, one success.
    assert mock_adapter.call_count == 2


@pytest.mark.asyncio
async def test_create_with_retry_permanent_error_no_retry(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    HTTP 401 raises PermanentLLMError immediately with no retry.
    """
    http_401 = httpx.HTTPStatusError(
        "unauthorized",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(401),
    )
    mock_adapter = _MockAdapter(side_effect=http_401)
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(PermanentLLMError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    # Error code should reflect the HTTP status; failure means
    # _classify_error mapped to the wrong category.
    assert exc_info.value.code == "401"

    # Detail should carry the status code for diagnostics.
    assert exc_info.value.detail is not None
    assert exc_info.value.detail.status_code == 401

    # No backoff sleeps — permanent errors abort immediately.
    assert tracker.calls == []

    # Only one adapter call — no retry attempted.
    assert mock_adapter.call_count == 1


@pytest.mark.asyncio
async def test_create_with_retry_exhausted_raises(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    All attempts timeout, raising RetryableLLMError after
    exhaustion.
    """
    # All 3 attempts time out
    mock_adapter = _MockAdapter(
        side_effect=httpx.TimeoutException("timeout"),
    )
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(RetryableLLMError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    # Code should be "timeout" since all failures were timeouts.
    assert exc_info.value.code == "timeout"

    # Two backoff sleeps (between attempt 1->2 and 2->3; no sleep
    # after the final failed attempt).
    assert len(tracker.calls) == 2

    # All 3 attempts should have been made before giving up.
    assert mock_adapter.call_count == 3


@pytest.mark.asyncio
async def test_create_with_retry_already_classified_reraise(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    If the adapter raises PermanentLLMError directly, it is
    re-raised without reclassification.
    """
    # Adapter raises an already-classified error
    original_error = PermanentLLMError(
        "auth failed",
        code="auth_error",
        detail=LLMErrorDetail(provider="test"),
    )
    mock_adapter = _MockAdapter(side_effect=original_error)
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(PermanentLLMError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    # The exact same error object should be re-raised, not
    # wrapped in a new PermanentLLMError. Failure means
    # _execute_with_retry reclassified an already-classified
    # error.
    assert exc_info.value is original_error
    assert exc_info.value.code == "auth_error"

    # No backoff sleeps — already-classified errors bypass retry.
    assert tracker.calls == []

    # Only one adapter call — no retry for pre-classified errors.
    assert mock_adapter.call_count == 1


@pytest.mark.asyncio
async def test_create_with_retry_already_classified_retryable_reraise(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    If the adapter raises RetryableLLMError directly, it is
    re-raised without reclassification or further retries.
    """
    original_error = RetryableLLMError(
        "rate limited upstream",
        code="429",
        detail=LLMErrorDetail(status_code=429),
    )
    mock_adapter = _MockAdapter(side_effect=original_error)
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(RetryableLLMError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    # Same error object re-raised, not reclassified or wrapped.
    assert exc_info.value is original_error

    # No backoff -- pre-classified RetryableLLMError is
    # immediately re-raised by the
    # ``except (PermanentLLMError, RetryableLLMError)`` clause
    # in _execute_with_retry.
    assert tracker.calls == []

    # Only one call -- no further retries for pre-classified
    # errors.
    assert mock_adapter.call_count == 1


@pytest.mark.asyncio
async def test_create_with_retry_connection_error_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    ``ConnectionError`` is a transient network failure (tunnel
    disconnect, socket reset) and must be retried.
    """
    mock_adapter = _MockAdapter(
        side_effect=ConnectionError("connection refused"),
    )
    tracker = _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(RetryableLLMError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    assert exc_info.value.code == "connection_error"
    assert "connection refused" in str(exc_info.value)

    # Retryable errors are retried — backoff sleeps must have fired.
    assert len(tracker.calls) == retry_config.max_retries

    # 1 initial + max_retries retries.
    assert mock_adapter.call_count == retry_config.max_retries + 1


@pytest.mark.parametrize(
    ("status_code", "expected_error_type"),
    [
        # 429 is in default retryable retryable_status_codes — should retry
        (429, RetryableLLMError),
        # 500 is in default retryable retryable_status_codes — should retry
        (500, RetryableLLMError),
        # 502 is in default retryable retryable_status_codes — should retry
        (502, RetryableLLMError),
        # 503 is in default retryable retryable_status_codes — should retry
        (503, RetryableLLMError),
        # 400 is NOT retryable — should be permanent
        (400, PermanentLLMError),
        # 401 is NOT retryable — should be permanent
        (401, PermanentLLMError),
        # 403 is NOT retryable — should be permanent
        (403, PermanentLLMError),
        # 404 is NOT retryable — should be permanent
        (404, PermanentLLMError),
    ],
    ids=[
        "429-retryable",
        "500-retryable",
        "502-retryable",
        "503-retryable",
        "400-permanent",
        "401-permanent",
        "403-permanent",
        "404-permanent",
    ],
)
@pytest.mark.asyncio
async def test_create_with_retry_http_status_classification(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
    status_code: int,
    expected_error_type: type,
) -> None:
    """
    HTTP status codes are classified correctly as retryable or
    permanent based on the retry config's ``retryable_status_codes`` list.
    """
    http_error = httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(status_code),
    )
    # Always fail so we can check classification
    mock_adapter = _MockAdapter(side_effect=http_error)
    _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(expected_error_type) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    # The error code should match the HTTP status string.
    assert exc_info.value.code == str(status_code)

    # Detail must carry the status code for downstream
    # diagnostics.
    assert exc_info.value.detail is not None
    assert exc_info.value.detail.status_code == status_code


# ── ContextWindowExceededError tests ──────────────────


@pytest.mark.asyncio
async def test_context_overflow_openai_error_body(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    OpenAI context overflow (HTTP 400 with
    context_length_exceeded code) raises
    ContextWindowExceededError with correct token counts.
    """
    openai_body = json.dumps(
        {
            "error": {
                "code": "context_length_exceeded",
                "message": (
                    "This model's maximum context length is"
                    " 128000 tokens. However, you requested"
                    " 142000 tokens (10000 in the messages,"
                    " 132000 in the completion). Please"
                    " reduce the length of the messages or"
                    " completion."
                ),
            }
        }
    )
    http_400 = httpx.HTTPStatusError(
        "context window exceeded",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(
            400,
            content=openai_body.encode(),
            headers={"content-type": "application/json"},
        ),
    )
    mock_adapter = _MockAdapter(side_effect=http_400)
    _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(ContextWindowExceededError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    # max_context_tokens must match the limit in the OpenAI
    # error message.
    assert exc_info.value.max_context_tokens == 128000, (
        "Expected max_context_tokens=128000, got"
        f" {exc_info.value.max_context_tokens}. Failure means"
        " the OpenAI error pattern regex did not match."
    )
    # actual_tokens must match the reported count from the
    # error message.
    assert exc_info.value.actual_tokens == 142000, (
        f"Expected actual_tokens=142000, got {exc_info.value.actual_tokens}."
    )
    # Code must be context_length_exceeded for downstream
    # detection.
    assert exc_info.value.code == "context_length_exceeded"


@pytest.mark.asyncio
async def test_context_overflow_anthropic_sum_pattern(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    Anthropic overflow error (N + M > limit) raises
    ContextWindowExceededError with correct token counts.
    """
    # Anthropic's "{input} + {max_tokens} > {limit}" format
    anthropic_body = "197202 + 21333 > 200000"
    http_400 = httpx.HTTPStatusError(
        "overflow",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(
            400,
            content=anthropic_body.encode(),
        ),
    )
    mock_adapter = _MockAdapter(side_effect=http_400)
    _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(ContextWindowExceededError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    # In Anthropic's "a + b > limit" pattern the full request size is
    # a + b (197202 + 21333 = 218535), not just a (the prompt alone).
    assert exc_info.value.max_context_tokens == 200000, (
        f"Expected max_context_tokens=200000, got {exc_info.value.max_context_tokens}."
    )
    assert exc_info.value.actual_tokens == 218535, (
        f"Expected actual_tokens=218535 (197202+21333), got {exc_info.value.actual_tokens}."
    )


@pytest.mark.asyncio
async def test_context_overflow_anthropic_long_pattern(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    Anthropic overflow (prompt is too long: N tokens > M maximum)
    raises ContextWindowExceededError with correct token counts.
    """
    anthropic_body = "prompt is too long: 210000 tokens > 200000 maximum"
    http_400 = httpx.HTTPStatusError(
        "overflow",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(
            400,
            content=anthropic_body.encode(),
        ),
    )
    mock_adapter = _MockAdapter(side_effect=http_400)
    _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(ContextWindowExceededError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    assert exc_info.value.max_context_tokens == 200000, (
        f"Expected max=200000, got {exc_info.value.max_context_tokens}."
    )
    assert exc_info.value.actual_tokens == 210000, (
        f"Expected actual=210000, got {exc_info.value.actual_tokens}."
    )


@pytest.mark.asyncio
async def test_context_overflow_gemini_pattern(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    Gemini overflow error raises ContextWindowExceededError with
    correct token counts extracted.
    """
    gemini_body = (
        "input token count (1100000) exceeds the maximum number of tokens allowed (1048576)"
    )
    http_400 = httpx.HTTPStatusError(
        "overflow",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(
            400,
            content=gemini_body.encode(),
        ),
    )
    mock_adapter = _MockAdapter(side_effect=http_400)
    _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(ContextWindowExceededError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    assert exc_info.value.max_context_tokens == 1048576, (
        f"Expected max=1048576, got {exc_info.value.max_context_tokens}."
    )
    assert exc_info.value.actual_tokens == 1100000, (
        f"Expected actual=1100000, got {exc_info.value.actual_tokens}."
    )


@pytest.mark.asyncio
async def test_unrecognized_400_not_context_overflow(
    monkeypatch: pytest.MonkeyPatch,
    retry_config: RetryPolicy,
) -> None:
    """
    A generic HTTP 400 that doesn't match any overflow pattern
    raises PermanentLLMError (not ContextWindowExceededError).
    """
    generic_body = "invalid request: missing required 'model' field"
    http_400 = httpx.HTTPStatusError(
        "bad request",
        request=httpx.Request("POST", "http://test"),
        response=httpx.Response(
            400,
            content=generic_body.encode(),
        ),
    )
    mock_adapter = _MockAdapter(side_effect=http_400)
    _patch_client_deps(monkeypatch, mock_adapter)

    with pytest.raises(PermanentLLMError) as exc_info:
        await Client().responses.create(
            **_default_create_kwargs(),
            retry=retry_config,
        )

    # Must be PermanentLLMError, NOT ContextWindowExceededError.
    # Failure means an unrelated 400 would enter the
    # compact-retry loop.
    assert not isinstance(exc_info.value, ContextWindowExceededError), (
        "Unrecognized 400 must not be classified as"
        " ContextWindowExceededError -- it would incorrectly"
        " trigger the compaction-retry loop."
    )
    assert exc_info.value.code == "400"


# ── Structured output translation (text → response_format) ──────────


class _CapturingAdapter:
    """Adapter stub that captures the ``extra`` dict passed to chat_completions.

    :param captured_extra: List to append the extra dict into on each call.
    """

    def __init__(self, captured_extra: list[dict[str, Any]]) -> None:
        self._captured = captured_extra

    async def chat_completions(
        self,
        messages: Any,
        model: str,
        tools: Any,
        stream: bool,
        extra: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record *extra* and return a minimal chat response.

        :param messages: Chat messages (ignored).
        :param model: Model id (ignored).
        :param tools: Tool schemas (ignored).
        :param stream: Streaming flag (ignored).
        :param extra: The extra kwargs dict — this is what we capture.
        :param kwargs: Additional kwargs (ignored).
        :returns: Minimal chat completion response.
        """
        self._captured.append(dict(extra))
        return {
            "choices": [{"message": {"content": "ok"}}],
            "model": model,
        }


@pytest.mark.asyncio
async def test_text_json_schema_translated_to_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Responses API ``text`` param with ``json_schema`` format is translated
    to Chat Completions ``response_format`` for non-OpenAI adapters.

    Without this translation, the ``text`` kwarg is sent as-is in the
    Chat Completions body and rejected with 400 by providers that don't
    recognise it (e.g. Databricks). A failure here means the structured
    output schema is lost or malformed in the Chat Completions path.
    """
    from omnigent.llms.routing import RoutedModel

    captured: list[dict[str, Any]] = []
    adapter = _CapturingAdapter(captured)
    routed = RoutedModel(provider="databricks", model="test-model")

    monkeypatch.setattr("omnigent.llms.client.parse_model_string", lambda model: routed)
    monkeypatch.setattr("omnigent.llms.client.get_adapter", lambda provider: adapter)
    monkeypatch.setattr(
        "omnigent.llms.client.responses_input_to_chat_messages",
        lambda input, instructions: [{"role": "user", "content": "test"}],
    )
    monkeypatch.setattr(
        "omnigent.llms.client.chat_response_to_response",
        lambda result: _make_response(),
    )

    client = Client()
    await client.responses.create(
        input=[{"role": "user", "content": "test"}],
        model="databricks/test-model",
        text={
            "format": {
                "type": "json_schema",
                "name": "my_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"tier": {"type": "string"}},
                    "required": ["tier"],
                },
            },
        },
    )

    # The adapter should have received response_format, not text.
    assert len(captured) == 1, f"Expected 1 call, got {len(captured)}"
    extra = captured[0]
    assert "text" not in extra, (
        f"'text' should have been removed from extra, but got {extra!r}. "
        "The raw Responses API param leaked into the Chat Completions body."
    )
    assert "response_format" in extra, (
        f"Expected 'response_format' in extra, got keys: {list(extra.keys())}. "
        "The text→response_format translation did not fire."
    )
    rf = extra["response_format"]
    assert rf["type"] == "json_schema", (
        f"response_format.type should be 'json_schema', got {rf['type']!r}"
    )
    assert rf["json_schema"]["name"] == "my_schema", (
        f"Schema name not preserved: {rf['json_schema']!r}"
    )
    assert rf["json_schema"]["strict"] is True
    assert "schema" in rf["json_schema"]


@pytest.mark.asyncio
async def test_text_without_json_schema_not_translated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``text`` param without ``json_schema`` format passes through unchanged.

    Only ``json_schema``-typed text should be translated. Other shapes
    (e.g. ``{"format": {"type": "text"}}``) must not be mangled.
    """
    from omnigent.llms.routing import RoutedModel

    captured: list[dict[str, Any]] = []
    adapter = _CapturingAdapter(captured)
    routed = RoutedModel(provider="databricks", model="test-model")

    monkeypatch.setattr("omnigent.llms.client.parse_model_string", lambda model: routed)
    monkeypatch.setattr("omnigent.llms.client.get_adapter", lambda provider: adapter)
    monkeypatch.setattr(
        "omnigent.llms.client.responses_input_to_chat_messages",
        lambda input, instructions: [{"role": "user", "content": "test"}],
    )
    monkeypatch.setattr(
        "omnigent.llms.client.chat_response_to_response",
        lambda result: _make_response(),
    )

    client = Client()
    await client.responses.create(
        input=[{"role": "user", "content": "test"}],
        model="databricks/test-model",
        text={"format": {"type": "text"}},
    )

    assert len(captured) == 1
    extra = captured[0]
    # Non-json_schema text should not be translated — it stays absent
    # (popped from extra but no response_format injected).
    assert "response_format" not in extra
    assert "text" not in extra
