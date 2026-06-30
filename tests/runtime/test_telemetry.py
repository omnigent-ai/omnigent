"""
Unit tests for the ``omnigent.runtime.telemetry`` helpers.

Exercises pure helpers (no spans created) and the trace-context
wrapper with an in-memory OTel exporter so the tests stay fast
and deterministic. Integration with the full workflow lives in
``test_telemetry_integration.py``.
"""

from __future__ import annotations

import os
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


@pytest.fixture(autouse=True)
def _opt_in_telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Telemetry is opt-in (``OMNIGENT_TELEMETRY_ENABLED``, off by default).
    This module exercises telemetry behavior, so opt in for every test; the
    opt-out test clears it explicitly. Also resets the session-id contextvar
    around each test so a ``set_session_id`` / hook call in one test (which
    deliberately does not reset — prod requests are isolated async tasks)
    cannot leak into the next.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")
    token = telemetry._session_id_var.set(None)
    try:
        yield
    finally:
        telemetry._session_id_var.reset(token)
        # init()/enable_tracing() in a telemetry test flips global tracing on
        # and never resets it; clear it so it can't leak "tracing on" into
        # other suites (e.g. the executor-adapter tests).
        from omnigent.inner.tracing import disable_tracing

        disable_tracing()
        telemetry._initialized = False


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


def test_init_disabled_by_default_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Telemetry is opt-in: with ``OMNIGENT_TELEMETRY_ENABLED`` unset,
    ``init`` is a no-op even when an OTLP endpoint is configured — no
    provider is installed, so a default install pays nothing.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.delenv("OMNIGENT_TELEMETRY_ENABLED", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setattr(telemetry, "_initialized", False)

    assert telemetry.telemetry_enabled() is False
    before = otel_trace.get_tracer_provider()
    telemetry.init()
    assert otel_trace.get_tracer_provider() is before


def test_fastapi_session_id_hook_stamps_session_id(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    The FastAPI server-request hook tags the server span with the
    Omnigent session id parsed from a ``/sessions/<conv_…>/`` path, so
    every session-scoped request span is findable by session.

    :param in_memory_exporter: In-memory span exporter fixture.
    """
    tracer = otel_trace.get_tracer("test")
    span = tracer.start_span("POST /v1/sessions/{id}/events")
    telemetry._fastapi_session_id_hook(span, {"path": "/v1/sessions/conv_deadbeef/events"})
    span.end()

    exported = in_memory_exporter.get_finished_spans()
    assert exported[-1].attributes is not None
    assert exported[-1].attributes.get("session.id") == "conv_deadbeef"


def test_fastapi_session_id_hook_ignores_non_session_paths(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    The hook leaves spans for non-session routes untouched.

    :param in_memory_exporter: In-memory span exporter fixture.
    """
    tracer = otel_trace.get_tracer("test")
    span = tracer.start_span("GET /v1/info")
    telemetry._fastapi_session_id_hook(span, {"path": "/v1/info"})
    span.end()

    exported = in_memory_exporter.get_finished_spans()
    assert "session.id" not in (exported[-1].attributes or {})


def test_tracing_context_stamps_session_id_on_agent_span(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    Spans created by a session-scoped ``TracingContext`` carry the
    ``session.id`` attribute, so agent-turn spans — which can root their
    own trace — stay groupable by session in the backend.

    :param in_memory_exporter: In-memory span exporter fixture.
    """
    from omnigent.inner.tracing import TracingContext

    tctx = TracingContext(session_id="conv_abc123")
    agent_span = tctx.start_agent_span("my-agent", "hello")
    tctx.end_agent_span(agent_span, response="hi")

    exported = in_memory_exporter.get_finished_spans()
    agent_spans = [s for s in exported if s.name == "agent:my-agent"]
    assert agent_spans, "expected an agent span to be exported"
    assert agent_spans[-1].attributes.get("session.id") == "conv_abc123"


def test_set_session_id_stamps_current_span(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    ``set_session_id`` tags the active span — used by session-creating
    routes (``POST /v1/sessions``) where the conv id is minted in the body
    and so is absent from the request path the FastAPI hook reads. A falsy
    id is a no-op.

    :param in_memory_exporter: In-memory span exporter fixture.
    """
    tracer = otel_trace.get_tracer("test")
    with tracer.start_as_current_span("POST /v1/sessions"):
        telemetry.set_session_id("conv_cafef00d")
        telemetry.set_session_id(None)  # no-op, must not raise or clear

    exported = in_memory_exporter.get_finished_spans()
    assert exported[-1].attributes.get("session.id") == "conv_cafef00d"


def test_session_scope_processor_stamps_every_span() -> None:
    """
    The generic mechanism: every span created inside ``session_scope`` is
    tagged with ``session.id`` by ``_SessionIdSpanProcessor`` — no per-span
    code — and spans outside the scope are left untouched.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(telemetry._make_session_id_processor())
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    with telemetry.session_scope("conv_generic01"):
        with tracer.start_as_current_span("server.request"):
            with tracer.start_as_current_span("db.query"):  # child span, never stamped by hand
                pass
    with tracer.start_as_current_span("outside.scope"):
        pass

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["server.request"].attributes.get("session.id") == "conv_generic01"
    assert spans["db.query"].attributes.get("session.id") == "conv_generic01"
    assert "session.id" not in (spans["outside.scope"].attributes or {})


def test_init_sets_service_name_from_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A passed ``service_name`` becomes ``OTEL_SERVICE_NAME``, overriding
    any inherited value — so a child process (e.g. the runner spawned by
    the server) is attributable as its own component rather than
    inheriting the parent's name.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    # Simulate an inherited value from a parent process.
    monkeypatch.setenv("OTEL_SERVICE_NAME", "omni-server")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)

    telemetry.init("omni-runner")

    assert os.environ["OTEL_SERVICE_NAME"] == "omni-runner"


def test_init_defaults_service_name_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no argument and no operator-set ``OTEL_SERVICE_NAME``, the
    service name defaults to ``omnigent`` so spans are never anonymous.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)

    telemetry.init()

    assert os.environ["OTEL_SERVICE_NAME"] == "omnigent"


def test_init_honors_operator_service_name_without_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no argument but an operator-set ``OTEL_SERVICE_NAME``, the
    operator's value is preserved.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "my-deployment")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("OTEL_LOGS_EXPORTER", "none")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)

    telemetry.init()

    assert os.environ["OTEL_SERVICE_NAME"] == "my-deployment"


def _stub_fastapi_instrumentor(monkeypatch: pytest.MonkeyPatch) -> list[FastAPI]:
    """
    Replace ``FastAPIInstrumentor.instrument_app`` with a recorder.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: A list that accumulates each app passed to the
        instrumentor — empty means instrumentation was skipped.
    """
    calls: list[FastAPI] = []
    monkeypatch.setattr(
        "opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app",
        lambda app, **kwargs: calls.append(app),
    )
    return calls


def test_instrument_fastapi_app_disabled_without_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With no flag and no tracing backend configured, FastAPI
    instrumentation is skipped — bare installs pay no span overhead.
    """
    monkeypatch.delenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    calls = _stub_fastapi_instrumentor(monkeypatch)

    telemetry.instrument_fastapi_app(FastAPI())

    assert calls == []


def test_instrument_fastapi_app_default_on_with_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With the flag unset, instrumentation defaults ON when an OTLP
    endpoint is configured — that is when HTTP server spans have
    somewhere to go and when cross-app trace propagation matters.
    """
    monkeypatch.delenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    app = FastAPI()
    calls = _stub_fastapi_instrumentor(monkeypatch)

    telemetry.instrument_fastapi_app(app)

    assert calls == [app]


def test_instrument_fastapi_app_explicit_false_overrides_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An explicit ``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=false`` wins
    even when a backend is configured — operators can force it off.
    """
    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", "false")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    calls = _stub_fastapi_instrumentor(monkeypatch)

    telemetry.instrument_fastapi_app(FastAPI())

    assert calls == []


def test_instrument_fastapi_app_calls_instrumentor_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The explicit flag installs OpenTelemetry FastAPI instrumentation
    even with no backend configured (the in-memory-exporter test path).
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", "true")
    app = FastAPI()
    calls = _stub_fastapi_instrumentor(monkeypatch)

    telemetry.instrument_fastapi_app(app)

    assert calls == [app]


def test_instrument_httpx_wires_global_client() -> None:
    """
    ``_instrument_httpx`` installs the global HTTPX instrumentation so
    outbound httpx requests inject ``traceparent``. Idempotent — a
    second call must not raise. Uninstruments afterward to avoid
    leaking global state into other tests.
    """
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    instrumentor = HTTPXClientInstrumentor()
    was_instrumented = instrumentor.is_instrumented_by_opentelemetry
    try:
        telemetry._instrument_httpx()
        assert instrumentor.is_instrumented_by_opentelemetry is True
        # Idempotent: calling again is a no-op, not an error.
        telemetry._instrument_httpx()
        assert instrumentor.is_instrumented_by_opentelemetry is True
    finally:
        if not was_instrumented:
            instrumentor.uninstrument()


def test_instrument_sqlalchemy_engine_instruments() -> None:
    """
    ``instrument_sqlalchemy_engine`` instruments a real engine so its
    statements emit spans. Engine-scoped instrumentation does not leak
    globally, so no teardown is needed.
    """
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    # Must not raise; the call is the contract exercised at engine
    # creation in ``db.utils.get_or_create_engine``.
    telemetry.instrument_sqlalchemy_engine(engine)


def test_instrument_sqlalchemy_engine_missing_package_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When the optional SQLAlchemy instrumentation package is absent,
    the helper degrades to a no-op rather than raising — bare installs
    without the tracing extras must still create engines.
    """
    import sys

    from sqlalchemy import create_engine

    # Force the import inside the helper to fail with ImportError.
    monkeypatch.setitem(sys.modules, "opentelemetry.instrumentation.sqlalchemy", None)
    engine = create_engine("sqlite://")
    # Should not raise despite the missing package.
    telemetry.instrument_sqlalchemy_engine(engine)


def test_inject_extract_frame_round_trip(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    A frame injected under a span and consumed via ``consume_frame_span``
    nests under the same trace — the JSON-frame websocket propagation
    invariant (host tunnel, session-updates) holds end to end.
    """
    tracer = otel_trace.get_tracer("test")
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with tracer.start_as_current_span("producer"):
            frame = telemetry.inject_trace_context({"kind": "host.launch_runner"})
    assert "traceparent" in frame

    with telemetry.consume_frame_span("host.launch_runner", frame) as span:
        consumed_hex = format(span.get_span_context().trace_id, "032x")

    assert consumed_hex == _RESP_HEX, (
        f"consumer trace {consumed_hex!r} should match producer trace "
        f"{_RESP_HEX!r} — frame trace-context propagation is broken."
    )


def test_inject_trace_context_noop_without_active_span() -> None:
    """
    Outside any span, ``inject_trace_context`` leaves the carrier
    unchanged so frames stay byte-for-byte wire-compatible.
    """
    carrier = {"kind": "host.stat", "request_id": "req_1"}
    result = telemetry.inject_trace_context(carrier)
    assert result is carrier
    assert "traceparent" not in carrier


def test_consume_frame_span_roots_new_trace_without_carrier(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    A carrier with no trace headers roots a fresh trace rather than
    raising — a frame from a peer that never injected context is still
    handled, just without an upstream parent.
    """
    with telemetry.consume_frame_span("host.hello", {"kind": "host.hello"}) as span:
        assert span.get_span_context().trace_id != 0


def test_consume_frame_span_omits_payload_when_capture_off(
    in_memory_exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With content capture off (the default), the frame body is NOT
    attached to the span — only its structure/metadata is traced.
    """
    monkeypatch.setattr(telemetry, "_capture_content", False)
    with telemetry.consume_frame_span(
        "host.launch_runner",
        {"kind": "host.launch_runner", "workspace": "/tmp"},
    ):
        pass
    span = in_memory_exporter.get_finished_spans()[-1]
    assert "omnigent.message.payload" not in (span.attributes or {})


def test_consume_frame_span_records_redacted_payload_when_capture_on(
    in_memory_exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With content capture on, the received frame body is attached to the
    span — but secret-looking keys are redacted, the W3C propagation
    keys are dropped, and benign fields are preserved verbatim.
    """
    monkeypatch.setattr(telemetry, "_capture_content", True)
    with telemetry.consume_frame_span(
        "host.launch_runner",
        {
            "kind": "host.launch_runner",
            "binding_token": "SUPER_SECRET",
            "workspace": "/tmp/ws",
            "traceparent": "00-abc-def-01",
        },
    ):
        pass
    span = in_memory_exporter.get_finished_spans()[-1]
    payload = (span.attributes or {})["omnigent.message.payload"]
    assert "SUPER_SECRET" not in payload
    assert "[redacted]" in payload
    assert "traceparent" not in payload
    assert "/tmp/ws" in payload


def test_record_message_payload_truncates(
    in_memory_exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An oversized payload is capped so the trace backend never becomes a
    payload store.
    """
    monkeypatch.setattr(telemetry, "_capture_content", True)
    with telemetry.span("x") as span:
        telemetry.record_message_payload({"blob": "A" * 9000}, span=span)
    out = in_memory_exporter.get_finished_spans()[-1]
    payload = (out.attributes or {})["omnigent.message.payload"]
    assert payload.endswith("…[truncated]")
    assert len(payload) <= telemetry._CONTENT_MAX_LEN + len("…[truncated]")


def test_span_helper_emits_named_span_with_attributes(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    ``telemetry.span`` emits a named child span with the given
    attributes — the helper used to instrument plain infra boundaries
    (terminal attach, policy evaluation).
    """
    with telemetry.span(
        "terminal.attach",
        attributes={"session.id": "s1", "terminal.read_only": True},
    ):
        pass

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "terminal.attach"
    assert spans[0].attributes["session.id"] == "s1"
    assert spans[0].attributes["terminal.read_only"] is True


def test_span_helper_nests_under_active_trace(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    A span opened with ``telemetry.span`` nests under the currently
    active trace context, so infra spans join the request/turn trace
    rather than rooting their own.
    """
    with telemetry.trace_context_for_response(response_id=_RESP_ID):
        with telemetry.span("policy.evaluate"):
            pass

    spans = in_memory_exporter.get_finished_spans()
    assert len(spans) == 1
    assert format(spans[0].context.trace_id, "032x") == _RESP_HEX


def test_httpx_to_fastapi_propagates_trace_across_http_hop(
    in_memory_exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A real HTTP hop continues the caller's trace.

    With HTTPX client instrumentation (inject) and FastAPI server
    instrumentation (extract) both active, a request made inside a
    parent span must run its server-side handler in the *same* trace.
    This is the Phase 1 propagation invariant that makes the
    server -> runner -> harness HTTP/tunnel mesh render as one trace:
    httpx injects ``traceparent``, FastAPI extracts it, and the handler
    nests under the caller rather than rooting a new trace.
    """
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from starlette.testclient import TestClient

    monkeypatch.setenv("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION", "true")

    app = FastAPI()
    handler_trace_id: dict[str, str] = {}

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        """
        Record the trace ID active inside the server handler.

        :returns: A trivial JSON body.
        """
        ctx = otel_trace.get_current_span().get_span_context()
        handler_trace_id["value"] = format(ctx.trace_id, "032x")
        return {"ok": True}

    telemetry.instrument_fastapi_app(app)
    httpx_instrumentor = HTTPXClientInstrumentor()
    was_instrumented = httpx_instrumentor.is_instrumented_by_opentelemetry
    telemetry._instrument_httpx()
    tracer = otel_trace.get_tracer("test")
    try:
        with telemetry.trace_context_for_response(response_id=_RESP_ID):
            with tracer.start_as_current_span("client-call"):
                # Starlette's TestClient runs on an instrumented httpx
                # client, so the outbound request carries traceparent.
                response = TestClient(app).get("/ping")
                assert response.status_code == 200
    finally:
        if not was_instrumented:
            httpx_instrumentor.uninstrument()
        FastAPIInstrumentor.uninstrument_app(app)

    # The server handler ran under the caller's derived trace, proving
    # the traceparent crossed the HTTP boundary and was extracted.
    assert handler_trace_id.get("value") == _RESP_HEX, (
        f"server handler trace_id {handler_trace_id.get('value')!r} does "
        f"not match caller trace {_RESP_HEX!r} — traceparent did not "
        "propagate across the HTTP hop (inject or extract is broken)."
    )


def test_instrument_httpx_client_injects_over_custom_transport(
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """
    A client on a custom transport propagates only after instrument_client.

    The process-wide httpx instrumentation patches only httpx's *standard*
    transports, so a client built on a custom ``AsyncBaseTransport`` — the
    server->runner ``WSTunnelTransport`` — is invisible to it: outbound
    requests carry no ``traceparent`` and the runner roots a disconnected
    trace. :func:`telemetry.instrument_httpx_client` wraps the instance to
    close that gap. This guards the server->runner forward staying in the
    caller's trace; without the per-client instrumentation the dispatch
    hop silently splits into two traces again.
    """
    import asyncio

    import httpx

    captured: dict[str, dict[str, str]] = {}

    class _CapturingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, request=request)

    tracer = otel_trace.get_tracer("test")

    async def _call(client: httpx.AsyncClient) -> None:
        with telemetry.trace_context_for_response(response_id=_RESP_ID):
            with tracer.start_as_current_span("client-call"):
                await client.get("http://runner/v1/ping")
        await client.aclose()

    # Baseline: a custom transport is not reached by the global hook, so no
    # context rides along regardless of whether global httpx is instrumented.
    captured.clear()
    bare = httpx.AsyncClient(transport=_CapturingTransport(), base_url="http://runner")
    asyncio.run(_call(bare))
    assert "traceparent" not in captured["headers"], (
        "a custom-transport client unexpectedly injected traceparent without "
        "instrument_client — the per-client fix may be unnecessary; re-evaluate."
    )

    # After instrument_client the traceparent rides the custom transport,
    # pinned to the caller's response-derived trace.
    captured.clear()
    wrapped = httpx.AsyncClient(transport=_CapturingTransport(), base_url="http://runner")
    telemetry.instrument_httpx_client(wrapped)
    asyncio.run(_call(wrapped))
    traceparent = captured["headers"].get("traceparent")
    assert traceparent is not None and _RESP_HEX in traceparent, (
        f"traceparent {traceparent!r} missing or not pinned to caller trace "
        f"{_RESP_HEX!r} after instrument_client — the server->runner forward "
        "would not stay in the originating trace."
    )
