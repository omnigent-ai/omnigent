"""Tests for LLM retry logic: classification, backoff, and retry loop."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from omnigent.llms.errors import LLMErrorDetail, PermanentLLMError, RetryableLLMError
from omnigent.runtime.llm_retry import (
    classify_llm_error,
    compute_backoff_delay,
    detail_to_dict,
    execute_with_retry,
)
from omnigent.spec.types import RetryPolicy


@pytest.fixture()
def retryable_status_codes() -> list[int]:
    """
    Default retryable HTTP status codes used across classification tests.

    :returns: List of retryable status codes, e.g. ``[429, 500, 502, 503]``.
    """
    return [429, 500, 502, 503]


@pytest.fixture()
def retry_config_fast() -> RetryPolicy:
    """
    Retry config with minimal backoff for fast tests.

    Uses tiny backoff values so ``time.sleep`` (patched out) durations
    are negligible even if the patch were removed.

    :returns: A :class:`RetryPolicy` with 3 attempts and near-zero backoff.
    """
    return RetryPolicy(
        max_retries=3,
        backoff_base_s=0.001,
        backoff_max_s=0.01,
        retryable_status_codes=[429, 500, 502, 503],
    )


def _make_http_status_error(status_code: int, body: str = "error") -> httpx.HTTPStatusError:
    """
    Build a minimal ``httpx.HTTPStatusError`` for testing.

    :param status_code: HTTP status code for the mock response, e.g. ``429``.
    :param body: Response body text, e.g. ``"rate limited"``.
    :returns: An ``httpx.HTTPStatusError`` with the given status and body.
    """
    request = httpx.Request("POST", "http://test")
    response = httpx.Response(status_code, text=body, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


# ── classify_llm_error ───────────────────────────────────────────────


def test_classify_timeout_is_retryable(
    retryable_status_codes: list[int],
) -> None:
    """
    Timeout exceptions must be classified as retryable with code='timeout'.
    """
    exc = httpx.TimeoutException("timeout")

    result = classify_llm_error(exc, retryable_status_codes)

    # Timeouts are always transient — must produce RetryableLLMError.
    # Failure would mean timeouts are treated as permanent, skipping retry.
    assert isinstance(result, RetryableLLMError)
    # Code must be "timeout" so SSE events can distinguish timeout retries.
    # Failure would mean downstream consumers misidentify the error type.
    assert result.code == "timeout"


def test_classify_retryable_http_status(
    retryable_status_codes: list[int],
) -> None:
    """
    HTTP 429 must be classified as retryable when 429 is in the retryable list.
    """
    exc = _make_http_status_error(429, body="rate limited")

    result = classify_llm_error(exc, retryable_status_codes)

    # 429 is in retryable_status_codes — must produce RetryableLLMError.
    # Failure would mean rate-limited requests are not retried.
    assert isinstance(result, RetryableLLMError)
    # Code must be the string form of the status code for SSE events.
    # Failure would mean the retry event carries the wrong error code.
    assert result.code == "429"


def test_classify_non_retryable_http_status(
    retryable_status_codes: list[int],
) -> None:
    """
    HTTP 401 must be classified as permanent when not in the retryable list.
    """
    exc = _make_http_status_error(401, body="unauthorized")

    result = classify_llm_error(exc, retryable_status_codes)

    # 401 is not in retryable_status_codes — must produce PermanentLLMError.
    # Failure would mean auth failures are retried, wasting time.
    assert isinstance(result, PermanentLLMError)
    # Code must be the string form of the status code.
    # Failure would mean the error event carries the wrong code.
    assert result.code == "401"


def test_classify_unknown_exception(
    retryable_status_codes: list[int],
) -> None:
    """
    Generic exceptions must be classified as permanent.
    """
    exc = Exception("something unexpected")

    result = classify_llm_error(exc, retryable_status_codes)

    # Generic exceptions are not retryable — must produce PermanentLLMError.
    # Failure would mean unknown errors are retried indefinitely.
    assert isinstance(result, PermanentLLMError)
    assert result.code == "unknown_error"


def test_classify_connection_error_is_retryable(
    retryable_status_codes: list[int],
) -> None:
    """
    ``ConnectionError`` (tunnel disconnect, socket reset) must be retryable.

    The tunnel registry raises bare ``ConnectionError`` when the
    runner WebSocket closes mid-request. This is transient — the
    runner reconnects with backoff — so the retry loop must fire.
    """
    exc = ConnectionError("tunnel closed before request completed")

    result = classify_llm_error(exc, retryable_status_codes)

    assert isinstance(result, RetryableLLMError)
    assert result.code == "connection_error"


# ── compute_backoff_delay ────────────────────────────────────────────


def test_compute_backoff_basic() -> None:
    """
    Backoff delay must be at most base * 2^index (before cap), with jitter.

    Formula: ``base * (2 ** attempt_index)``, then cap at ``backoff_max_s``,
    then jitter via uniform(0.5, 1.5).
    """
    base = 2.0
    max_delay = 30.0

    delay = compute_backoff_delay(attempt_index=2, backoff_base_s=base, backoff_max_s=max_delay)

    deterministic = min(base * (2**2), max_delay)
    # Delay must not exceed deterministic * 1.5 (max jitter multiplier).
    # Failure would mean the exponential formula, cap, or jitter is broken.
    assert delay <= deterministic * 1.5
    # Jitter multiplier is uniform(0.5, 1.5), so delay >= 50% of deterministic.
    # Failure would mean jitter range is wrong (too aggressive).
    assert delay >= deterministic * 0.5


def test_compute_backoff_capped() -> None:
    """
    Backoff delay must be capped at backoff_max_s even when base*2^index exceeds it.
    """
    # base=10, index=3 → 80, but max=5 should cap it.
    delay = compute_backoff_delay(attempt_index=3, backoff_base_s=10.0, backoff_max_s=5.0)

    # Delay must never exceed backoff_max_s * 1.5 (max jitter).
    # Failure would mean the cap is not applied, causing excessive waits.
    assert delay <= 5.0 * 1.5
    # Jitter floor is 50% of the cap.
    assert delay >= 5.0 * 0.5


# ── execute_with_retry ───────────────────────────────────────────────


def test_execute_with_retry_success_first_attempt(
    retry_config_fast: RetryPolicy,
) -> None:
    """
    When call_fn succeeds on the first attempt, no retry callback fires.
    """
    call_fn = MagicMock(return_value="ok")
    on_retry = MagicMock()

    result = execute_with_retry(call_fn, retry_config_fast, on_retry)

    # call_fn succeeds immediately — result must be the return value.
    # Failure would mean the retry loop doesn't propagate success.
    assert result == "ok"
    # call_fn must be called exactly once (no unnecessary retries).
    # Failure would mean the loop retries even on success.
    assert call_fn.call_count == 1
    # on_retry must never fire when the first attempt succeeds.
    # Failure would mean spurious retry events are emitted.
    assert on_retry.call_count == 0


def test_execute_with_retry_retries_on_timeout(
    retry_config_fast: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Timeout on first call must trigger one retry; second success is returned.
    """
    # Patch time.sleep to avoid real delays in tests.
    monkeypatch.setattr("omnigent.runtime.llm_retry.time.sleep", lambda _: None)

    call_fn = MagicMock(side_effect=[httpx.TimeoutException("timeout"), "recovered"])
    on_retry = MagicMock()

    result = execute_with_retry(call_fn, retry_config_fast, on_retry)

    # Second call succeeds — result must be "recovered".
    # Failure would mean the retry loop doesn't return the recovery value.
    assert result == "recovered"
    # call_fn must be called twice: initial failure + one retry.
    # Failure would mean too many or too few attempts.
    assert call_fn.call_count == 2
    # on_retry must fire exactly once (before the single retry).
    # Failure would mean retry events are missing or duplicated.
    assert on_retry.call_count == 1


def test_execute_with_retry_permanent_error_no_retry(
    retry_config_fast: RetryPolicy,
) -> None:
    """
    A permanent error (401) must raise immediately without any retry.
    """
    exc = _make_http_status_error(401, body="unauthorized")
    call_fn = MagicMock(side_effect=exc)
    on_retry = MagicMock()

    with pytest.raises(PermanentLLMError) as exc_info:
        execute_with_retry(call_fn, retry_config_fast, on_retry)

    # PermanentLLMError must be raised, not RetryableLLMError.
    # Failure would mean non-retryable errors are silently retried.
    assert exc_info.value.code == "401"
    # call_fn must be called exactly once — no retry on permanent errors.
    # Failure would mean wasted attempts on auth failures.
    assert call_fn.call_count == 1
    # on_retry must never fire for permanent errors.
    # Failure would mean false retry events are emitted to clients.
    assert on_retry.call_count == 0


def test_execute_with_retry_exhausted_raises(
    retry_config_fast: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When all attempts fail with retryable errors, RetryableLLMError is raised.
    """
    # Patch time.sleep to avoid real delays in tests.
    monkeypatch.setattr("omnigent.runtime.llm_retry.time.sleep", lambda _: None)

    call_fn = MagicMock(
        side_effect=httpx.TimeoutException("timeout"),
    )
    on_retry = MagicMock()

    with pytest.raises(RetryableLLMError) as exc_info:
        execute_with_retry(call_fn, retry_config_fast, on_retry)

    # After exhausting all attempts, RetryableLLMError must be raised.
    # Failure would mean the loop silently returns None or hangs.
    assert exc_info.value.code == "timeout"
    # Total tries = max_retries + 1 (initial + retries). With
    # max_retries=3 → 4 tries.
    # Failure would mean the loop exits early or retries beyond the limit.
    assert call_fn.call_count == retry_config_fast.max_retries + 1
    # on_retry fires between tries, so max_retries times.
    # Failure would mean retry events don't match the actual retry count.
    assert on_retry.call_count == retry_config_fast.max_retries


# ── SSE retry event structure ────────────────────────────────────────


def test_retry_event_structure_on_timeout(
    retry_config_fast: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Timeout retry event must contain the exact SSE payload structure.

    Verifies every field of the ``response.retry`` event dict passed
    to ``on_retry`` when a timeout triggers a retry.
    """
    # Patch time.sleep to avoid real delays in tests.
    monkeypatch.setattr("omnigent.runtime.llm_retry.time.sleep", lambda _: None)

    call_fn = MagicMock(
        side_effect=[httpx.TimeoutException("timeout"), "ok"],
    )
    captured_events: list[dict[str, Any]] = []

    def on_retry(event: dict[str, Any]) -> None:
        """Capture retry events for inspection."""
        captured_events.append(event)

    execute_with_retry(call_fn, retry_config_fast, on_retry)

    # Exactly one retry event must be captured.
    # Failure would mean the retry loop emitted wrong number of events.
    assert len(captured_events) == 1
    event = captured_events[0]

    # "type" must be "response.retry" — the SSE event discriminator.
    # Failure would break client-side event routing.
    assert event["type"] == "response.retry"

    # "source" must be "llm" — distinguishes LLM retries from others.
    # Failure would cause clients to misattribute the retry source.
    assert event["source"] == "llm"

    # "attempt" must be 2 — next attempt number, 1-based.
    # First call is attempt 1, so the retry targets attempt 2.
    # Failure would mean attempt numbering is off-by-one.
    assert event["attempt"] == 2

    # "max_retries" must match the retry config value.
    # Failure would give clients wrong retry budget information.
    assert event["max_attempts"] == retry_config_fast.max_retries + 1

    # "delay_seconds" must be a positive float (backoff duration).
    # Failure would mean the backoff calculation produced invalid delay.
    assert isinstance(event["delay_seconds"], float)
    assert event["delay_seconds"] >= 0

    # "error.code" must be "timeout" for timeout-triggered retries.
    # Failure would mean the error classification is wrong.
    assert event["error"]["code"] == "timeout"

    # "error.message" must contain "timed out" from the classification.
    # Failure would mean the human-readable message is malformed.
    assert "timed out" in event["error"]["message"]

    # "error.detail" must be None because LLMErrorDetail() has all
    # None fields, which detail_to_dict converts to None.
    # Failure would mean empty details leak into the SSE payload.
    assert event["error"]["detail"] is None


def test_retry_event_structure_on_http_429(
    retry_config_fast: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    HTTP 429 retry event must carry status code and body in error detail.

    Verifies the ``error.code`` and ``error.detail`` fields when a
    rate-limit response triggers a retry.
    """
    # Patch time.sleep to avoid real delays in tests.
    monkeypatch.setattr("omnigent.runtime.llm_retry.time.sleep", lambda _: None)

    exc = _make_http_status_error(429, body="rate limited")
    call_fn = MagicMock(side_effect=[exc, "ok"])
    captured_events: list[dict[str, Any]] = []

    def on_retry(event: dict[str, Any]) -> None:
        """Capture retry events for inspection."""
        captured_events.append(event)

    execute_with_retry(call_fn, retry_config_fast, on_retry)

    # Exactly one retry event must be captured.
    # Failure would mean wrong number of retry events emitted.
    assert len(captured_events) == 1
    event = captured_events[0]

    # "error.code" must be "429" — the string form of the status code.
    # Failure would mean HTTP status is not propagated correctly.
    assert event["error"]["code"] == "429"

    # "error.detail" must be a dict with status_code and response_body.
    # Failure would mean provider diagnostics are lost in the SSE event.
    assert event["error"]["detail"] == {
        "status_code": 429,
        "response_body": "rate limited",
    }

    # Top-level fields must still follow the standard structure.
    # Failure would mean the event schema is inconsistent across errors.
    assert event["type"] == "response.retry"
    assert event["source"] == "llm"
    assert event["attempt"] == 2
    assert event["max_attempts"] == retry_config_fast.max_retries + 1
    assert isinstance(event["delay_seconds"], float)
    assert event["delay_seconds"] >= 0


# ── detail_to_dict ───────────────────────────────────────────────────


def test_detail_to_dict_with_all_fields() -> None:
    """
    detail_to_dict must include all non-None fields from LLMErrorDetail.
    """
    detail = LLMErrorDetail(
        provider="openai",
        status_code=429,
        response_body='{"error": "rate limit"}',
    )

    result = detail_to_dict(detail)

    # All three fields are set — dict must contain all of them.
    # Failure would mean some fields are silently dropped.
    assert result == {
        "provider": "openai",
        "status_code": 429,
        "response_body": '{"error": "rate limit"}',
    }


def test_detail_to_dict_with_none() -> None:
    """
    detail_to_dict(None) must return None — no detail to serialize.
    """
    result = detail_to_dict(None)

    # None input must produce None output, not an empty dict.
    # Failure would mean the SSE payload contains a spurious empty dict.
    assert result is None


@pytest.fixture
def _otel_in_memory_exporter():
    """
    Install a fresh OTel TracerProvider with an in-memory exporter,
    yield it, and restore the original provider / set-once state on
    teardown.

    OTel only allows the TracerProvider to be set once per process.
    Leaving the test-installed provider in place after the test
    silently misroutes or drops spans in any later test in the same
    process, producing order-dependent flakiness that does not
    reproduce in isolation. This fixture snapshots every global it
    mutates and restores them in a finally block.
    """
    import mlflow
    import mlflow.tracing
    from mlflow.tracing.provider import provider as mlflow_provider_wrapper
    from mlflow.tracing.trace_manager import InMemoryTraceManager
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from omnigent.runtime import telemetry as runtime_telemetry

    original_provider = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    original_set_once_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]
    original_mlflow_once_done = mlflow_provider_wrapper._global_provider_init_once._done  # type: ignore[attr-defined]
    original_telemetry_initialized = runtime_telemetry._initialized  # type: ignore[attr-defined]
    original_env_value = os.environ.get("MLFLOW_USE_DEFAULT_TRACER_PROVIDER")

    os.environ["MLFLOW_USE_DEFAULT_TRACER_PROVIDER"] = "false"
    runtime_telemetry._initialized = False  # type: ignore[attr-defined]

    trace_manager_instance = getattr(InMemoryTraceManager, "_instance", None)
    if trace_manager_instance is not None:
        trace_manager_instance._traces.clear()  # type: ignore[attr-defined]
        trace_manager_instance._otel_id_to_mlflow_trace_id.clear()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    mlflow_provider_wrapper._global_provider_init_once._done = False  # type: ignore[attr-defined]
    mlflow.tracing.enable()

    try:
        yield exporter
    finally:
        exporter.clear()
        otel_trace._TRACER_PROVIDER = original_provider  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = original_set_once_done  # type: ignore[attr-defined]
        mlflow_provider_wrapper._global_provider_init_once._done = original_mlflow_once_done  # type: ignore[attr-defined]
        runtime_telemetry._initialized = original_telemetry_initialized  # type: ignore[attr-defined]
        if original_env_value is None:
            os.environ.pop("MLFLOW_USE_DEFAULT_TRACER_PROVIDER", None)
        else:
            os.environ["MLFLOW_USE_DEFAULT_TRACER_PROVIDER"] = original_env_value
        if trace_manager_instance is not None:
            trace_manager_instance._traces.clear()  # type: ignore[attr-defined]
            trace_manager_instance._otel_id_to_mlflow_trace_id.clear()  # type: ignore[attr-defined]


def test_execute_with_retry_records_gen_ai_retry_events_on_span(
    retry_config_fast: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
    _otel_in_memory_exporter,
) -> None:
    # Patch time.sleep so the test does not actually sleep.
    monkeypatch.setattr("omnigent.runtime.llm_retry.time.sleep", lambda _: None)
    # _capture_content is a module-level cached value. Patch it
    # directly so record_llm_retry includes error.message.
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", True)

    import mlflow
    from mlflow.entities import SpanType

    from omnigent.runtime.llm_retry import execute_with_retry

    # Fail twice, succeed on the third attempt. With max_retries=3
    # the policy allows 4 total tries, so 2 failures result in 2
    # retry events recorded on the span.
    call_fn = MagicMock(
        side_effect=[
            httpx.TimeoutException("first timeout"),
            httpx.TimeoutException("second timeout"),
            "recovered",
        ]
    )
    on_retry = MagicMock()

    with mlflow.start_span("llm_call", span_type=SpanType.CHAT_MODEL) as span:
        result = execute_with_retry(
            call_fn,
            retry_config_fast,
            on_retry,
            llm_span=span,
        )

    assert result == "recovered"
    assert call_fn.call_count == 3
    assert on_retry.call_count == 2

    spans = _otel_in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    events = [e for e in spans[0].events if e.name == "gen_ai.retry"]
    assert len(events) == 2
    attrs_by_attempt = {int(e.attributes["attempt"]): dict(e.attributes or {}) for e in events}
    assert set(attrs_by_attempt.keys()) == {1, 2}
    for _attempt, attrs in attrs_by_attempt.items():
        assert attrs["max_attempts"] == retry_config_fast.max_retries + 1
        assert attrs["error.type"] == "RetryableLLMError"
        assert "timed out" in attrs["error.message"]
        assert attrs["backoff_seconds"] > 0


def test_execute_with_retry_error_message_gated_by_content_capture(
    retry_config_fast: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
    _otel_in_memory_exporter,
) -> None:
    # OMNIGENT_OTEL_CAPTURE_CONTENT is OFF (default). error.message
    # must not appear; error.type still does.
    monkeypatch.setattr("omnigent.runtime.llm_retry.time.sleep", lambda _: None)
    # Force cache OFF (default state) so error.message is NOT captured.
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", False)

    import mlflow
    from mlflow.entities import SpanType

    from omnigent.runtime.llm_retry import execute_with_retry

    call_fn = MagicMock(
        side_effect=[
            httpx.TimeoutException("with PII: user@example.com"),
            "recovered",
        ]
    )
    with mlflow.start_span("llm_call", span_type=SpanType.CHAT_MODEL) as span:
        execute_with_retry(call_fn, retry_config_fast, MagicMock(), llm_span=span)

    spans = _otel_in_memory_exporter.get_finished_spans()
    events = [e for e in spans[0].events if e.name == "gen_ai.retry"]
    assert len(events) == 1
    attrs = dict(events[0].attributes or {})
    assert attrs["error.type"] == "RetryableLLMError"
    assert "error.message" not in attrs
    # And the PII string must not leak via any other attribute key.
    for v in attrs.values():
        assert "user@example.com" not in str(v)


def test_execute_with_retry_without_span_is_safe(
    retry_config_fast: RetryPolicy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Omitting ``llm_span`` keeps the original behavior intact.
    Retries happen, on_retry fires, and no span events get recorded
    because no span exists.
    """
    monkeypatch.setattr("omnigent.runtime.llm_retry.time.sleep", lambda _: None)

    call_fn = MagicMock(side_effect=[httpx.TimeoutException("t"), "ok"])
    on_retry = MagicMock()

    result = execute_with_retry(call_fn, retry_config_fast, on_retry)

    assert result == "ok"
    assert call_fn.call_count == 2
    assert on_retry.call_count == 1


def test_detail_to_dict_with_empty_detail() -> None:
    """
    detail_to_dict with all-None fields must return None, not empty dict.

    An LLMErrorDetail with no fields set (e.g. timeout errors) produces
    an empty dict internally, which is collapsed to None to keep the
    SSE JSON payload clean.
    """
    detail = LLMErrorDetail()

    result = detail_to_dict(detail)

    # All fields are None → empty dict → collapsed to None.
    # Failure would mean empty dicts leak into the SSE event payload.
    assert result is None
