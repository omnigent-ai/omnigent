"""
Unit tests for the ``omnigent.runtime.telemetry`` helpers.

Exercises pure helpers (no spans created) and the trace-context
wrapper with an in-memory OTel exporter so the tests stay fast
and deterministic. Integration with the full workflow lives in
``test_telemetry_integration.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import (
    StatusCode,
)

from omnigent.runtime import telemetry

_RESP_HEX = "d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3"
_RESP_ID = f"resp_{_RESP_HEX}"


# ── Fixtures ────────────────────────────────────────────


@pytest.fixture
def in_memory_exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """
    Install a fresh SDK TracerProvider with an in-memory exporter
    for the duration of one test.
    """
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Set via private attribute to bypass OTel's set-once guard.
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]

    yield exporter

    exporter.clear()


# ── parse_provider_name ─────────────────────────────────


@pytest.mark.parametrize(
    "input_model,expected",
    [
        ("openai/gpt-5.4", ("openai", "gpt-5.4")),
        ("anthropic/claude-sonnet-4", ("anthropic", "claude-sonnet-4")),
        ("gpt-5.4", ("", "gpt-5.4")),
        ("", ("", "")),
        (
            "vertex/publishers/google/models/gemini-2.0",
            ("vertex", "publishers/google/models/gemini-2.0"),
        ),
    ],
)
def test_parse_provider_name(input_model: str, expected: tuple[str, str]) -> None:
    """
    :param input_model: Model string under test.
    :param expected: Expected ``(provider, model)`` tuple.
    """
    assert telemetry.parse_provider_name(input_model) == expected


# ── trace_id_from_response_id ───────────────────────────


def test_trace_id_from_response_id_valid() -> None:
    """
    A well-formed response ID decodes to its 32-char hex suffix.
    This proves operators can strip the ``resp_`` prefix and paste
    the hex into a trace backend's lookup UI.
    """
    assert telemetry.trace_id_from_response_id(_RESP_ID) == _RESP_HEX


def test_trace_id_from_response_id_wrong_prefix() -> None:
    """
    An ID without the ``resp_`` prefix raises ValueError. This is
    the first validation line — operators should not be able to
    confuse conversation IDs (``conv_...``) for response IDs.
    """
    with pytest.raises(ValueError, match="resp_"):
        telemetry.trace_id_from_response_id("conv_" + _RESP_HEX)


def test_trace_id_from_response_id_short_hex_zero_padded() -> None:
    """
    A short hex suffix (< 32 chars) is zero-padded to 32 chars.
    Harness-allocated response IDs use 24-char hex; the padding
    produces a valid 128-bit W3C trace ID.
    """
    result = telemetry.trace_id_from_response_id("resp_abcdef")
    assert result == "abcdef" + "0" * 26
    assert len(result) == 32


def test_trace_id_from_response_id_too_long() -> None:
    """
    A hex suffix longer than 32 chars raises ValueError.
    """
    with pytest.raises(ValueError, match="at most"):
        telemetry.trace_id_from_response_id("resp_" + "a" * 33)


def test_trace_id_from_response_id_invalid_hex() -> None:
    """
    An ID whose hex suffix contains non-hex characters raises
    ValueError. Non-hex input would produce an undefined int
    conversion, so we catch it explicitly.
    """
    bad_id = "resp_" + "Z" * 32
    with pytest.raises(ValueError, match="hex"):
        telemetry.trace_id_from_response_id(bad_id)


# ── _env_bool / should_capture_content ─────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_env_bool(
    raw: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    :param raw: The raw env var value.
    :param expected: Expected parsed boolean.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv("_AP_TEST_FLAG", raw)
    assert telemetry._env_bool("_AP_TEST_FLAG") is expected


def test_env_bool_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env vars read as ``False``."""
    monkeypatch.delenv("_AP_TEST_FLAG", raising=False)
    assert telemetry._env_bool("_AP_TEST_FLAG") is False


def test_should_capture_content_reflects_module_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``should_capture_content()`` reads the module-level flag that
    :func:`init` populates from the env var.
    """
    monkeypatch.setattr(telemetry, "_capture_content", False)
    assert telemetry.should_capture_content() is False
    monkeypatch.setattr(telemetry, "_capture_content", True)
    assert telemetry.should_capture_content() is True


# ── trace_context_for_response ──────────────────────────


def test_trace_context_for_response_root(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    A span opened inside ``trace_context_for_response(response_id)``
    has the trace_id derived from ``response_id`` — the full
    omnigent-to-trace-backend lookup chain works end-to-end.
    """
    tracer = otel_trace.get_tracer("test")
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("invoke_agent"):
            pass

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1, f"expected 1 span, got {len(spans)}"
    actual_hex = format(spans[0].context.trace_id, "032x")
    assert actual_hex == _RESP_HEX, (
        f"trace_id {actual_hex!r} does not match response ID hex {_RESP_HEX!r}"
    )


def test_trace_context_for_response_sub_agent(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    When ``root_response_id`` is set, the sub-agent span nests
    under the root's trace, not its own. This proves sub-agent
    spawn workflows share the root's trace.
    """
    tracer = otel_trace.get_tracer("test")
    sub_response_id = "resp_" + "a" * 32
    with telemetry.trace_context_for_response(
        response_id=sub_response_id,
        root_response_id=_RESP_ID,
    ):
        with tracer.start_as_current_span("invoke_agent sub"):
            pass

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    actual_hex = format(spans[0].context.trace_id, "032x")
    # Sub-agent span has the ROOT response's trace ID, not its own.
    assert actual_hex == _RESP_HEX, (
        f"sub-agent trace_id {actual_hex!r} should match root response hex {_RESP_HEX!r}"
    )


def test_trace_context_for_response_shared_across_children(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Multiple child spans created inside the same trace context all
    share one trace ID.
    """
    tracer = otel_trace.get_tracer("test")
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("invoke_agent"):
            with tracer.start_as_current_span("chat"):
                pass
            with tracer.start_as_current_span("raw-otel-child"):
                pass

    spans = in_memory_exporter.get_finished_spans()
    trace_ids = {format(s.context.trace_id, "032x") for s in spans}
    assert trace_ids == {_RESP_HEX}, (
        f"expected all spans to share trace_id {_RESP_HEX!r}, got {trace_ids!r}"
    )


# ── get_traceparent_env ─────────────────────────────────


def test_get_traceparent_env_no_span() -> None:
    """
    Outside of any span, ``get_traceparent_env`` returns an empty
    dict — we do not invent a trace context for subprocess
    inheritance when the parent has none.
    """
    env = telemetry.get_traceparent_env()
    assert env == {}, f"expected empty env dict outside of any span, got {env!r}"


def test_get_traceparent_env_inside_span(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Inside an active span, ``get_traceparent_env`` returns a
    ``TRACEPARENT`` env var whose trace_id matches the active
    span's trace_id.
    """
    tracer = otel_trace.get_tracer("test")
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("root"):
            env = telemetry.get_traceparent_env()
            assert "TRACEPARENT" in env, f"expected TRACEPARENT key, got {list(env.keys())!r}"
            parts = env["TRACEPARENT"].split("-")
            assert len(parts) == 4
            version, trace_id_hex, _span_id_hex, _flags = parts
            assert version == "00"
            assert trace_id_hex == _RESP_HEX, (
                f"traceparent trace_id {trace_id_hex!r} does not match response hex {_RESP_HEX!r}"
            )


# ── record_llm_usage ────────────────────────────────────


def test_record_llm_usage_basic(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    ``record_llm_usage`` sets OTel GenAI semantic convention attributes
    for input/output/total tokens.
    """
    tracer = otel_trace.get_tracer("test")
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("chat") as span:
            telemetry.record_llm_usage(
                span,
                {"input_tokens": 100, "output_tokens": 50},
            )

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs.get("gen_ai.usage.input_tokens") == 100
    assert attrs.get("gen_ai.usage.output_tokens") == 50
    # Total is derived (100 + 50 = 150) when not provided.
    assert attrs.get("gen_ai.usage.total_tokens") == 150


def test_record_llm_usage_with_cache(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Cache breakdown fields are recorded when present.
    """
    tracer = otel_trace.get_tracer("test")
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("chat") as span:
            telemetry.record_llm_usage(
                span,
                {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "total_tokens": 1200,
                    "cache_read_input_tokens": 800,
                    "cache_creation_input_tokens": 100,
                },
            )

    spans = in_memory_exporter.get_finished_spans()
    attrs = spans[0].attributes or {}
    assert attrs.get("gen_ai.usage.input_tokens") == 1000
    assert attrs.get("gen_ai.usage.output_tokens") == 200
    assert attrs.get("gen_ai.usage.total_tokens") == 1200
    assert attrs.get("gen_ai.usage.cache_read_input_tokens") == 800
    assert attrs.get("gen_ai.usage.cache_creation_input_tokens") == 100


def test_record_llm_usage_without_cache_omits_fields(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    When cache fields are absent from the input dict, they are NOT
    recorded — this prevents masking "caching not reported" as
    "zero tokens cached".
    """
    tracer = otel_trace.get_tracer("test")
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("chat") as span:
            telemetry.record_llm_usage(
                span,
                {"input_tokens": 10, "output_tokens": 5},
            )

    spans = in_memory_exporter.get_finished_spans()
    attrs = spans[0].attributes or {}
    assert "gen_ai.usage.cache_read_input_tokens" not in attrs
    assert "gen_ai.usage.cache_creation_input_tokens" not in attrs


# ── record_error / record_cancellation ─────────────────


def test_record_error_sets_error_type_and_status(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    ``record_error`` marks the span as ERROR and sets ``error.type``
    to the exception class name.
    """
    tracer = otel_trace.get_tracer("test")

    class CustomError(Exception):
        pass

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("test") as span:
            telemetry.record_error(span, CustomError("boom"))

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes or {}
    assert attrs.get("error.type") == "CustomError"
    assert spans[0].status.status_code == StatusCode.ERROR


def test_record_cancellation_sets_cancelled_error_type(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Cancellation uses ``error.type = "cancelled"`` as the
    distinguishing marker.
    """
    tracer = otel_trace.get_tracer("test")
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("test") as span:
            telemetry.record_cancellation(span)

    spans = in_memory_exporter.get_finished_spans()
    attrs = spans[0].attributes or {}
    assert attrs.get("error.type") == "cancelled"
    assert spans[0].status.status_code == StatusCode.ERROR


# ── init() ──────────────────────────────────────────────


def test_init_no_endpoint_does_not_install_otlp_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, ``init`` must
    NOT replace the global TracerProvider — callers may have already
    installed their own.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)

    before = otel_trace.get_tracer_provider()
    telemetry.init()
    # No new provider installed when no endpoint is configured.
    assert otel_trace.get_tracer_provider() is before


def test_init_with_endpoint_installs_sdk_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, ``init`` installs a
    ``TracerProvider`` backed by an OTLP span exporter.
    """
    from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)
    # Reset OTel set-once guard so init() can install a new provider.
    otel_trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]

    telemetry.init()

    assert isinstance(otel_trace.get_tracer_provider(), SdkTracerProvider)


def test_init_respects_capture_content_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``init`` reads ``OMNIGENT_OTEL_CAPTURE_CONTENT`` each call
    so operators can toggle it after restart.
    """
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)
    monkeypatch.setenv("OMNIGENT_OTEL_CAPTURE_CONTENT", "true")
    telemetry.init()
    assert telemetry.should_capture_content() is True

    monkeypatch.setenv("OMNIGENT_OTEL_CAPTURE_CONTENT", "false")
    telemetry.init()
    assert telemetry.should_capture_content() is False


def test_instrument_fastapi_app_is_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    FastAPI instrumentation stays disabled unless explicitly requested.
    """
    calls: list[FastAPI] = []

    def fake_instrument_app(app: FastAPI) -> None:
        calls.append(app)

    monkeypatch.delenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", raising=False)
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        fake_instrument_app,
    )

    telemetry.instrument_fastapi_app(FastAPI())

    assert calls == []


def test_instrument_fastapi_app_calls_instrumentor_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The opt-in flag installs OpenTelemetry FastAPI instrumentation.
    """
    app = FastAPI()
    calls: list[FastAPI] = []

    def fake_instrument_app(app_to_instrument: FastAPI) -> None:
        calls.append(app_to_instrument)

    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", "true")
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        fake_instrument_app,
    )

    telemetry.instrument_fastapi_app(app)

    assert calls == [app]
