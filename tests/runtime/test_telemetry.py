"""
Unit tests for the ``omnigent.runtime.telemetry`` helpers.

Exercises pure helpers (no spans created) and the trace-context
wrapper with an in-memory OTel exporter so the tests stay fast
and deterministic. Integration with the full workflow lives in
``test_telemetry_integration.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi import FastAPI
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ReadableSpan,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    TraceFlags,
    TraceState,
    set_span_in_context,
)

from omnigent.runtime import telemetry

_RESP_HEX = "d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3"
_RESP_ID = f"resp_{_RESP_HEX}"


class _RecordingSpanExporter(SpanExporter):
    """
    Span exporter that records spans without network I/O.

    :param spans: Exported spans.
    """

    def __init__(self) -> None:
        """
        Initialize an empty exporter.

        :returns: ``None``.
        """
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        """
        Record a batch of spans.

        :param spans: Finished spans passed by the span processor.
        :returns: Successful export result.
        """
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        """
        Shut down the exporter.

        :returns: ``None``.
        """

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """
        Flush pending export work.

        :param timeout_millis: Flush timeout in milliseconds.
        :returns: ``True`` because this exporter has no buffered work.
        """
        return True


def _read_attr(span: Any, key: str) -> Any:
    """
    Read an attribute from an OTel span and decode MLflow's JSON
    wrapping.

    MLflow stores span attribute values via ``json.dumps`` so that
    complex types (dicts, lists, Pydantic models) round-trip
    through OTel's string-only attribute store. Strings are
    therefore wrapped in quotes when read back from the raw OTel
    attributes mapping. This helper decodes the JSON wrapping so
    tests can assert on the original value.

    :param span: An OTel ``ReadableSpan`` from the exporter.
    :param key: The attribute key to read.
    :returns: The decoded attribute value, or ``None`` if not set.
    """
    import json

    raw = span.attributes.get(key) if span.attributes else None
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _install_mlflow_otlp_test_provider(
    monkeypatch: pytest.MonkeyPatch,
    exporter: SpanExporter,
) -> TracerProvider:
    """
    Install a fresh OTel provider wired through MLflow's OTLP path.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param exporter: Span exporter MLflow should install on the
        provider, e.g. ``_RecordingSpanExporter()``.
    :returns: The installed OpenTelemetry ``TracerProvider``.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("MLFLOW_USE_DEFAULT_TRACER_PROVIDER", "false")
    monkeypatch.delenv("MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT", raising=False)
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)

    from mlflow.tracing.provider import provider as mlflow_provider_wrapper
    from mlflow.tracing.trace_manager import InMemoryTraceManager

    trace_manager_instance = getattr(InMemoryTraceManager, "_instance", None)
    if trace_manager_instance is not None:
        trace_manager_instance._traces.clear()  # type: ignore[attr-defined]
        trace_manager_instance._otel_id_to_mlflow_trace_id.clear()  # type: ignore[attr-defined]

    provider = TracerProvider()
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    mlflow_provider_wrapper._global_provider_init_once._done = False  # type: ignore[attr-defined]
    monkeypatch.setattr("mlflow.tracing.provider.get_otlp_exporter", lambda: exporter)
    monkeypatch.setattr("mlflow.tracing.provider.should_export_otlp_metrics", lambda: False)
    return provider


def _assert_otlp_encodable_span_attributes(span: ReadableSpan) -> None:
    """
    Assert span attributes can be encoded by the OTLP exporter.

    :param span: Finished OpenTelemetry span to validate.
    :raises Exception: If OpenTelemetry's OTLP encoder rejects an
        attribute value, e.g. ``None``.
    """
    from opentelemetry.exporter.otlp.proto.common._internal import _encode_attributes

    attributes = span.attributes or {}
    assert all(value is not None for value in attributes.values()), (
        f"span {span.name!r} has None-valued attributes: {attributes!r}"
    )
    _encode_attributes(attributes)


# ── Fixtures ────────────────────────────────────────────


@pytest.fixture
def in_memory_exporter() -> Iterator[InMemorySpanExporter]:
    """
    Install a fresh SDK TracerProvider with an in-memory exporter
    for the duration of one test.

    Each test that needs to assert on emitted spans uses this
    fixture to get a clean exporter. The fixture also resets
    MLflow's ``InMemoryTraceManager`` singleton between tests so
    previous tests' ``is_remote_trace=True`` registrations don't
    leak into the new one (which would cause
    ``register_trace`` to silently no-op on a duplicate).
    """
    # Force unified mode so MLflow shares the global provider.
    os.environ["MLFLOW_USE_DEFAULT_TRACER_PROVIDER"] = "false"
    # Reset the telemetry module's one-shot init guard so each
    # test gets a fresh MLflow init if it needs one.
    telemetry._initialized = False  # type: ignore[attr-defined]

    # Reset MLflow's singleton trace manager. Without this, a
    # previous test's registered trace leaks in and the new
    # start_span path sees an unexpected pre-existing trace.
    from mlflow.tracing.trace_manager import InMemoryTraceManager

    trace_manager_instance = getattr(InMemoryTraceManager, "_instance", None)
    if trace_manager_instance is not None:
        # Clear the internal dicts so no stale trace state leaks.
        trace_manager_instance._traces.clear()  # type: ignore[attr-defined]
        trace_manager_instance._otel_id_to_mlflow_trace_id.clear()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Set via private attribute to bypass OTel's set-once guard
    # AND force MLflow to re-initialize its global-provider wiring
    # so its span processors get added to the new provider.
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]

    # Force MLflow to re-register its span processors on the new
    # provider. Calling ``mlflow.tracing.enable`` again without
    # clearing its once-flag is a no-op, so we reset the flag.
    from mlflow.tracing.provider import provider as mlflow_provider_wrapper

    mlflow_provider_wrapper._global_provider_init_once._done = False  # type: ignore[attr-defined]

    import mlflow.tracing

    mlflow.tracing.enable()

    yield exporter

    # Clear spans collected by the exporter after the test.
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
    :func:`init` populates from the env var. Init is gated so the
    flag updates even on a second call.
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
    import mlflow
    from mlflow.entities import SpanType

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("invoke_agent", span_type=SpanType.AGENT):
            pass

    spans = in_memory_exporter.get_finished_spans()
    # Exactly one span emitted — the invoke_agent span.
    assert len(spans) == 1, (
        f"expected 1 span, got {len(spans)} — extra spans indicate "
        "MLflow emitted something we didn't request, or spans from a "
        "previous test leaked into this exporter."
    )
    actual_hex = format(spans[0].context.trace_id, "032x")
    assert actual_hex == _RESP_HEX, (
        f"trace_id {actual_hex!r} does not match response ID hex "
        f"{_RESP_HEX!r} — trace_context_for_response did not "
        "propagate the custom trace ID into MLflow's span creation path."
    )


def test_trace_context_for_response_sub_agent(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    When ``root_response_id`` is set, the sub-agent span nests
    under the root's trace, not its own. This proves sub-agent
    spawn workflows share the root's trace — the whole spawn tree
    lives in one trace.
    """
    import mlflow
    from mlflow.entities import SpanType

    sub_response_id = "resp_" + "a" * 32
    with telemetry.trace_context_for_response(
        response_id=sub_response_id,
        root_response_id=_RESP_ID,
    ):
        with mlflow.start_span("invoke_agent sub", span_type=SpanType.AGENT):
            pass

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    actual_hex = format(spans[0].context.trace_id, "032x")
    # Sub-agent span has the ROOT response's trace ID, not its own.
    assert actual_hex == _RESP_HEX, (
        f"sub-agent trace_id {actual_hex!r} should match root "
        f"response hex {_RESP_HEX!r}, not sub response hex — "
        "sub-agents must inherit the root's trace."
    )


def test_trace_context_for_response_shared_across_children(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Multiple child spans (MLflow + raw OTel) created inside the
    same trace context all share one trace ID. This is the
    hybrid-mode invariant — MLflow and raw OTel spans can mix
    freely in the same trace tree.
    """
    import mlflow
    from mlflow.entities import SpanType

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("invoke_agent", span_type=SpanType.AGENT):
            with mlflow.start_span("chat", span_type=SpanType.CHAT_MODEL):
                pass
            raw_tracer = otel_trace.get_tracer("test")
            with raw_tracer.start_as_current_span("raw-otel-child"):
                pass

    spans = in_memory_exporter.get_finished_spans()
    trace_ids = {format(s.context.trace_id, "032x") for s in spans}
    # All spans MUST share exactly one trace_id (the derived hex).
    assert trace_ids == {_RESP_HEX}, (
        f"expected all spans to share trace_id {_RESP_HEX!r}, "
        f"got {trace_ids!r}. Mixed trace IDs mean the parent context "
        "propagation broke between MLflow and raw OTel."
    )


# ── get_traceparent_env ─────────────────────────────────


def test_get_traceparent_env_no_span() -> None:
    """
    Outside of any span, ``get_traceparent_env`` returns an empty
    dict — we do not invent a trace context for subprocess
    inheritance when the parent has none.
    """
    env = telemetry.get_traceparent_env()
    assert env == {}, (
        f"expected empty env dict outside of any span, got {env!r}. "
        "A non-empty result means OTel's context API returned a span "
        "we don't expect."
    )


def test_get_traceparent_env_inside_span(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Inside an active span, ``get_traceparent_env`` returns a
    ``TRACEPARENT`` env var whose trace_id matches the active
    span's trace_id. This proves executor subprocess launchers can
    propagate the parent trace ID to a child process.
    """
    import mlflow
    from mlflow.entities import SpanType

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("root", span_type=SpanType.AGENT):
            env = telemetry.get_traceparent_env()
            assert "TRACEPARENT" in env, f"expected TRACEPARENT key, got {list(env.keys())!r}"
            # W3C traceparent format: 00-{trace_id}-{span_id}-{flags}
            parts = env["TRACEPARENT"].split("-")
            assert len(parts) == 4, (
                f"expected 4 traceparent parts, got {len(parts)}: {env['TRACEPARENT']!r}"
            )
            version, trace_id_hex, _span_id_hex, _flags = parts
            assert version == "00"
            # The trace_id in the env var must match our derived hex
            # so the child process nests under this trace.
            assert trace_id_hex == _RESP_HEX, (
                f"traceparent trace_id {trace_id_hex!r} does not match "
                f"response hex {_RESP_HEX!r} — executor subprocesses "
                "would end up in a different trace than the parent."
            )


# ── get_otel_subprocess_env ─────────────────────────────


def test_get_otel_subprocess_env_no_endpoint_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without ``OTEL_EXPORTER_OTLP_ENDPOINT``, the helper returns an
    empty dict — no half-configured OTel state leaks into executor
    subprocesses. This is the no-op baseline operators get when they
    haven't wired up a collector.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    assert telemetry.get_otel_subprocess_env() == {}
    assert telemetry.get_otel_subprocess_env(claude_sdk=True) == {}


def test_get_otel_subprocess_env_endpoint_no_active_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With an endpoint configured but no active span, the helper
    forwards the OTLP exporter knobs but emits no TRACEPARENT —
    the child has a collector to ship to but no parent span to
    nest under (e.g. a subprocess launched during server warmup
    before any agent turn started).
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "x-token=abc")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "omnigent-test")

    env = telemetry.get_otel_subprocess_env()

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "grpc"
    assert env["OTEL_EXPORTER_OTLP_HEADERS"] == "x-token=abc"
    assert env["OTEL_SERVICE_NAME"] == "omnigent-test"
    # No active span → propagator emits no carrier → no TRACEPARENT.
    assert "TRACEPARENT" not in env
    assert "TRACESTATE" not in env


def test_get_otel_subprocess_env_endpoint_with_active_span(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Inside an active span with an endpoint configured, the helper
    emits both the OTLP exporter knobs AND the TRACEPARENT — this
    is the normal in-flight executor case where a child process
    must nest under the omnigent agent span in the same trace.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

    import mlflow
    from mlflow.entities import SpanType

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("agent", span_type=SpanType.AGENT):
            env = telemetry.get_otel_subprocess_env()

    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert "TRACEPARENT" in env
    parts = env["TRACEPARENT"].split("-")
    assert len(parts) == 4
    # The TRACEPARENT trace_id must equal our derived hex so the
    # subprocess's spans land in the same trace as the parent.
    assert parts[1] == _RESP_HEX


def test_get_otel_subprocess_env_claude_sdk_sets_sdk_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``claude_sdk=True`` flips the SDK's built-in telemetry hooks on
    via ``CLAUDE_CODE_ENABLE_TELEMETRY=1`` and
    ``OTEL_TRACES_EXPORTER=otlp``. These are gated because they're
    no-ops in the other executor subprocesses (codex, pi, openai
    agents) and we don't want to spam unrecognized env vars.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    env = telemetry.get_otel_subprocess_env(claude_sdk=True)

    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
    # The default branch must NOT set these flags.
    plain = telemetry.get_otel_subprocess_env()
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in plain
    assert "OTEL_TRACES_EXPORTER" not in plain


def test_get_otel_subprocess_env_omits_unset_forwarded_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Only forwarded env vars that are actually set in the parent's
    environment are copied — we do not invent defaults. This keeps
    the subprocess env minimal and matches OTel's own behavior of
    falling back to baked-in defaults when a knob is unset.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_PROTOCOL", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)

    env = telemetry.get_otel_subprocess_env()

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in env
    assert "OTEL_EXPORTER_OTLP_PROTOCOL" not in env
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    assert "OTEL_SERVICE_NAME" not in env


# ── record_llm_usage ────────────────────────────────────


def test_record_llm_usage_basic(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    ``record_llm_usage`` sets the ``mlflow.chat.tokenUsage``
    attribute with input/output/total tokens. The attribute is
    what MLflow translates to ``gen_ai.usage.*`` on OTLP export,
    so getting this right means the token count makes it to the
    backend.
    """
    import mlflow
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("chat", span_type=SpanType.CHAT_MODEL) as span:
            telemetry.record_llm_usage(
                span,
                {"input_tokens": 100, "output_tokens": 50},
            )

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    payload = _read_attr(spans[0], SpanAttributeKey.CHAT_USAGE)
    # input_tokens/output_tokens must round-trip from the usage
    # dict verbatim — a wrong value here means record_llm_usage
    # mangled the input keys or type-coerced them incorrectly.
    assert payload["input_tokens"] == 100, (
        f"expected input_tokens=100, got {payload['input_tokens']!r}"
    )
    assert payload["output_tokens"] == 50, (
        f"expected output_tokens=50, got {payload['output_tokens']!r}"
    )
    # Total is DERIVED (input + output = 150) because the input
    # dict did not include a total_tokens field. A wrong value
    # means the total-derivation logic broke.
    assert payload["total_tokens"] == 150, (
        f"expected derived total_tokens = input + output = 150, "
        f"got {payload.get('total_tokens')!r}"
    )


def test_record_llm_usage_with_cache(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Cache breakdown fields are recorded when present. Their absence
    is meaningful — providers that do not report caching should not
    show zero-valued cache fields, so we only populate them when
    the input dict has the keys.
    """
    import mlflow
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("chat", span_type=SpanType.CHAT_MODEL) as span:
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
    payload = _read_attr(spans[0], SpanAttributeKey.CHAT_USAGE)
    assert payload["input_tokens"] == 1000
    assert payload["output_tokens"] == 200
    assert payload["total_tokens"] == 1200
    assert payload["cache_read_input_tokens"] == 800
    assert payload["cache_creation_input_tokens"] == 100


def test_record_llm_usage_without_cache_omits_fields(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    When cache fields are absent from the input dict, they are NOT
    recorded — this prevents masking "caching not reported" as
    "zero tokens cached", which would mislead cost analysis.
    """
    import mlflow
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("chat", span_type=SpanType.CHAT_MODEL) as span:
            telemetry.record_llm_usage(
                span,
                {"input_tokens": 10, "output_tokens": 5},
            )

    spans = in_memory_exporter.get_finished_spans()
    payload = _read_attr(spans[0], SpanAttributeKey.CHAT_USAGE)
    assert "cache_read_input_tokens" not in payload
    assert "cache_creation_input_tokens" not in payload


# ── record_error / record_cancellation ─────────────────


def test_record_error_sets_error_type_and_status(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    ``record_error`` marks the span as ERROR and sets an
    ``error.type`` attribute equal to the exception class name.
    Operators filter traces by ``error.type`` so this attribute
    must be populated.
    """
    import mlflow
    from mlflow.entities import SpanType

    class CustomError(Exception):
        pass

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("test", span_type=SpanType.AGENT) as span:
            telemetry.record_error(span, CustomError("boom"))

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert _read_attr(spans[0], "error.type") == "CustomError"
    # OTel status code — MLflow's SpanStatusCode.ERROR maps to
    # OTel StatusCode.ERROR (value = 2).
    from opentelemetry.trace import StatusCode

    assert spans[0].status.status_code == StatusCode.ERROR


def test_record_cancellation_sets_cancelled_error_type(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Cancellation uses ``error.type = "cancelled"`` as the
    distinguishing marker (neither OTel nor MLflow has a dedicated
    cancelled status). Operators filter by this attribute to find
    cancelled traces separately from failures.
    """
    import mlflow
    from mlflow.entities import SpanType

    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with mlflow.start_span("test", span_type=SpanType.AGENT) as span:
            telemetry.record_cancellation(span)

    spans = in_memory_exporter.get_finished_spans()
    assert _read_attr(spans[0], "error.type") == "cancelled"
    from opentelemetry.trace import StatusCode

    assert spans[0].status.status_code == StatusCode.ERROR


# ── init() ──────────────────────────────────────────────


def test_init_no_endpoint_does_not_enable_otlp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, ``init`` must
    NOT flip ``MLFLOW_ENABLE_OTLP_EXPORTER`` — otherwise operators
    who haven't configured an endpoint would get unexpected OTLP
    connection attempts.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_METRICS_EXPORTER", raising=False)
    monkeypatch.delenv("MLFLOW_ENABLE_OTLP_EXPORTER", raising=False)
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    telemetry.init()
    assert "MLFLOW_ENABLE_OTLP_EXPORTER" not in os.environ, (
        "init() set MLFLOW_ENABLE_OTLP_EXPORTER with no endpoint "
        "configured — OTLP export would fail at runtime."
    )


def test_init_with_endpoint_enables_otlp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, ``init`` flips
    ``MLFLOW_ENABLE_OTLP_EXPORTER=true`` so MLflow routes via OTLP
    instead of its default tracking-server path.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.delenv("MLFLOW_ENABLE_OTLP_EXPORTER", raising=False)
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    telemetry.init()
    assert os.environ.get("MLFLOW_ENABLE_OTLP_EXPORTER") == "true"


def test_init_allows_remote_parent_otel_server_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    MLflow's OTLP processor tolerates server spans with remote parents.

    FastAPI / ASGI auto-instrumentation creates exactly this shape
    when an incoming request carries a platform ``traceparent``:
    the first local server span has a parent, but no local MLflow
    trace has been registered yet. This test proves
    ``telemetry.init()`` patches MLflow before enablement so such
    spans are exported instead of crashing request handling.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    exporter = _RecordingSpanExporter()
    provider = _install_mlflow_otlp_test_provider(monkeypatch, exporter)

    telemetry.init()

    remote_parent = NonRecordingSpan(
        SpanContext(
            trace_id=int(_RESP_HEX, 16),
            span_id=0x1000000000000002,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
    )
    tracer = otel_trace.get_tracer("tests.runtime.telemetry")
    with tracer.start_as_current_span(
        "GET /health",
        context=set_span_in_context(remote_parent),
        kind=SpanKind.SERVER,
    ):
        pass

    assert provider.force_flush()
    assert [span.name for span in exporter.spans] == ["GET /health"]
    assert exporter.spans[0].kind is SpanKind.SERVER
    assert _read_attr(exporter.spans[0], SpanAttributeKey.SPAN_TYPE) == SpanType.UNKNOWN
    _assert_otlp_encodable_span_attributes(exporter.spans[0])


def test_init_defaults_raw_otel_root_span_type_for_otlp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Raw OTel root spans exported through MLflow get a valid span type.

    MLflow 3.11.1 registers raw OpenTelemetry spans by passing
    ``span_type=None`` into ``create_mlflow_span``. That stores
    ``mlflow.spanType=None`` and the OTLP exporter rejects the span.
    This test proves ``telemetry.init()`` patches that path so normal
    auto-instrumented spans are exportable.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    exporter = _RecordingSpanExporter()
    provider = _install_mlflow_otlp_test_provider(monkeypatch, exporter)

    telemetry.init()

    tracer = otel_trace.get_tracer("tests.runtime.telemetry")
    with tracer.start_as_current_span("GET /health", kind=SpanKind.SERVER):
        pass

    assert provider.force_flush()
    assert [span.name for span in exporter.spans] == ["GET /health"]
    assert _read_attr(exporter.spans[0], SpanAttributeKey.SPAN_TYPE) == SpanType.UNKNOWN
    _assert_otlp_encodable_span_attributes(exporter.spans[0])


def test_init_respects_capture_content_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``init`` reads ``OMNIGENT_OTEL_CAPTURE_CONTENT`` each call
    so operators can toggle it after restart. Idempotent re-init
    refreshes the flag.
    """
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
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
        """
        Record the app passed to the fake instrumentor.

        :param app: FastAPI app passed by telemetry code.
        """
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
        """
        Record the app passed to the fake instrumentor.

        :param app_to_instrument: FastAPI app passed by telemetry code.
        """
        calls.append(app_to_instrument)

    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", "true")
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        fake_instrument_app,
    )

    telemetry.instrument_fastapi_app(app)

    assert calls == [app]


def test_init_forces_unified_provider_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``init`` defaults ``MLFLOW_USE_DEFAULT_TRACER_PROVIDER`` to
    ``"false"`` (unified mode) so MLflow shares the global
    ``TracerProvider`` with raw OTel instrumentation. Without this,
    FastAPI auto-instrumentation spans and MLflow spans would live
    in separate trace trees.
    """
    monkeypatch.delenv("MLFLOW_USE_DEFAULT_TRACER_PROVIDER", raising=False)
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    telemetry.init()
    assert os.environ.get("MLFLOW_USE_DEFAULT_TRACER_PROVIDER") == "false"
