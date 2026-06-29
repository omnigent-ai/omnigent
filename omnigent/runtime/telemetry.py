"""
Agent-plane observability using the OpenTelemetry SDK directly.

See ``designs/OBSERVABILITY.md`` for the full design. The module
is intentionally thin — it holds only the omnigent-specific
concerns:

* **Trace ID derivation from the response ID.** Agent-plane response
  IDs are ``resp_<32-char hex>``. We reuse the hex suffix as the
  W3C trace ID so operators can look up a trace by its response ID
  without a lookup table. :func:`trace_context_for_response` injects
  a synthetic ``traceparent`` via the W3C TraceContext propagator.

* **Runtime init.** :func:`init` installs an OTLP ``TracerProvider``
  when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. When the endpoint is
  absent, tracing is still enabled so operators who install their own
  provider externally get spans for free; the default no-op provider
  discards them silently.

* **Subprocess OTel exporter forwarding.** :func:`get_otel_subprocess_env`
  builds the env-var dict an executor subprocess uses to ship its own
  OTel spans to the same backend. :func:`get_traceparent_env` is
  retained as a public utility for the future per-request
  trace-correlation work tracked on PR #1070.

* **A handful of record helpers** where the work is non-trivial
  (LLM usage normalization, cancellation tagging). Trivial
  operations like ``span.set_attribute(...)`` are called directly
  at instrumentation sites.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.sdk._logs.export import LogExporter
    from opentelemetry.sdk.metrics.export import MetricExporter
    from opentelemetry.trace import Span

_logger = logging.getLogger(__name__)

_RESP_PREFIX = "resp_"
_HEX_LEN = 32
# Sentinel span ID used in trace_context_for_response. start_agent_span
# detects this value and strips the parent so the agent span is exported
# as a true root span (parent_span_id absent in OTLP proto).
SENTINEL_PARENT_SPAN_ID = 0x1000000000000001

_capture_content: bool = False
_initialized: bool = False
_metrics_initialized: bool = False
_logs_initialized: bool = False


def _env_bool(name: str) -> bool:
    """
    Parse a boolean environment variable.

    Truthy values are ``"true"``, ``"1"``, ``"yes"`` (case-insensitive).
    Anything else (including unset) is ``False``.

    :param name: The environment variable name, e.g.
        ``"OMNIGENT_OTEL_CAPTURE_CONTENT"``.
    :returns: ``True`` if the env var is set to a truthy value.
    """
    return os.environ.get(name, "").strip().lower() in ("true", "1", "yes")


def should_capture_content() -> bool:
    """
    Return whether message content should be included on spans.

    Controlled by ``OMNIGENT_OTEL_CAPTURE_CONTENT``. Call sites
    read this flag before populating span inputs / outputs with user
    messages or tool results. Content capture is off by default
    because messages may contain PII or secrets.

    :returns: ``True`` when content capture is enabled.
    """
    return _capture_content


def instrument_fastapi_app(app: FastAPI) -> None:
    """
    Optionally install OpenTelemetry FastAPI instrumentation on an app.

    FastAPI auto-instrumentation is opt-in because it adds HTTP server
    spans and metrics that most deployments don't need. Set
    ``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true`` to enable.

    :param app: FastAPI app instance to instrument.
    """
    if not _env_bool("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION"):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        _logger.exception("failed to initialize FastAPI OpenTelemetry instrumentation")


def parse_provider_name(model: str) -> tuple[str, str]:
    """
    Split a provider-prefixed model string into ``(provider, model)``.

    Agent-plane model strings follow ``"<provider>/<model>"``, e.g.
    ``"openai/gpt-5.4"`` becomes ``("openai", "gpt-5.4")``. Unprefixed
    strings return an empty provider string so the span always has a
    value to record.

    :param model: The model identifier, e.g. ``"openai/gpt-5.4"``
        or ``"gpt-5.4"``.
    :returns: ``(provider, model)`` tuple. Provider is empty if the
        input has no prefix.
    """
    if "/" in model:
        provider, _, rest = model.partition("/")
        return provider, rest
    return "", model


def trace_id_from_response_id(response_id: str) -> str:
    """
    Extract the 32-char hex trace ID from an omnigent response ID.

    Response IDs have the format ``resp_<32-char hex>`` (generated
    via ``generate_task_id``). The hex suffix is a valid 128-bit
    W3C trace ID. Reusing it as the trace ID lets operators jump
    from a response ID to its trace by stripping the ``resp_``
    prefix — no lookup table, no search query.

    :param response_id: The response/task ID, e.g.
        ``"resp_d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3"``.
    :returns: The 32-char lowercase hex trace ID.
    :raises ValueError: If the response ID does not start with
        ``"resp_"`` or the hex suffix is not exactly 32 chars.
    """
    if not response_id.startswith(_RESP_PREFIX):
        raise ValueError(f"Expected {_RESP_PREFIX!r} prefix, got {response_id!r}")
    hex_part = response_id[len(_RESP_PREFIX) :]
    if len(hex_part) > _HEX_LEN:
        raise ValueError(
            f"Expected at most {_HEX_LEN} hex chars after prefix, "
            f"got {len(hex_part)} in {response_id!r}"
        )
    # Zero-pad short hex suffixes (e.g. 24-char harness-allocated
    # IDs) to a valid 128-bit W3C trace ID. The padding preserves
    # uniqueness — the original hex is a prefix of the trace ID.
    hex_part = hex_part.ljust(_HEX_LEN, "0")
    try:
        int(hex_part, 16)
    except ValueError as exc:
        raise ValueError(f"Invalid hex suffix in {response_id!r}: {exc}") from exc
    return hex_part


@contextmanager
def trace_context_for_response(
    response_id: str,
    *,
    root_response_id: str | None = None,
) -> Iterator[None]:
    """
    Set the active trace context for a workflow invocation.

    Derives the W3C trace ID from ``root_response_id`` (if set) or
    ``response_id``, then injects a synthetic ``traceparent`` header via
    the W3C TraceContext propagator to make any span started inside the
    context manager inherit this trace ID.

    For root invocations pass only ``response_id``; the trace ID is
    derived from it so direct response-ID → trace-ID lookup works.
    For sub-agent invocations pass both ``response_id`` (the
    sub-agent's own ID, exposed as ``task.id`` on the span) and
    ``root_response_id`` (the root of the spawn tree, used as the
    trace ID) so all sub-agents share the root's trace.

    :param response_id: The response/task ID for this invocation,
        e.g. ``"resp_d8e9f0a1..."``.
    :param root_response_id: The root response ID if this is a
        sub-agent invocation, otherwise ``None``.
    :raises ValueError: If ``response_id`` (or ``root_response_id``
        when set) cannot be parsed.
    """
    from opentelemetry import context
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    effective = root_response_id or response_id
    trace_id_hex = trace_id_from_response_id(effective)

    # Inject a synthetic traceparent to pin all spans to the response-derived
    # trace ID. The dummy parent span ID (1000000000000001) is a sentinel —
    # it never matches any real span so the agent span is effectively the
    # root for display purposes, even though it has a non-null parent_id in
    # the OTLP payload.
    traceparent = f"00-{trace_id_hex}-{SENTINEL_PARENT_SPAN_ID:016x}-01"
    ctx = TraceContextTextMapPropagator().extract({"traceparent": traceparent})
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


# OTel GenAI semantic convention attribute keys for token usage.
_GEN_AI_INPUT_TOKENS = "gen_ai.usage.input_tokens"
_GEN_AI_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
_GEN_AI_TOTAL_TOKENS = "gen_ai.usage.total_tokens"
_GEN_AI_CACHE_READ_TOKENS = "gen_ai.usage.cache_read_input_tokens"
_GEN_AI_CACHE_CREATION_TOKENS = "gen_ai.usage.cache_creation_input_tokens"

# OTel GenAI metric instrument names. See
# https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-metrics/
_METRIC_TOKEN_USAGE = "gen_ai.client.token.usage"
_METRIC_OPERATION_DURATION = "gen_ai.client.operation.duration"
_METRIC_TOOL_DURATION = "omnigent.tool.duration"

# Metric attribute keys
_ATTR_TOKEN_TYPE = "gen_ai.token.type"

# Lazy meter cache. Initialized on first record call; cleared by
# tests via _reset_instrument_cache_for_tests.
_meter = None
_token_usage_histogram = None
_operation_duration_histogram = None
_tool_duration_histogram = None
_tool_allowlist: frozenset[str] | None = None


def _get_meter():
    """Lazy-init the meter from the configured MeterProvider."""
    global _meter
    if _meter is None:
        from opentelemetry import metrics as otel_metrics

        _meter = otel_metrics.get_meter("omnigent.runtime.telemetry")
    return _meter


def _get_token_usage_histogram():
    global _token_usage_histogram
    if _token_usage_histogram is None:
        _token_usage_histogram = _get_meter().create_histogram(
            name=_METRIC_TOKEN_USAGE,
            unit="{token}",
            description="Number of input or output tokens used per LLM request.",
        )
    return _token_usage_histogram


def _get_operation_duration_histogram():
    global _operation_duration_histogram
    if _operation_duration_histogram is None:
        _operation_duration_histogram = _get_meter().create_histogram(
            name=_METRIC_OPERATION_DURATION,
            unit="s",
            description="Wall-clock duration of a GenAI client operation (agent turn).",
        )
    return _operation_duration_histogram


def _get_tool_duration_histogram():
    global _tool_duration_histogram
    if _tool_duration_histogram is None:
        _tool_duration_histogram = _get_meter().create_histogram(
            name=_METRIC_TOOL_DURATION,
            unit="s",
            description="Duration of a single tool call inside an agent turn.",
        )
    return _tool_duration_histogram


def _get_tool_allowlist() -> frozenset[str] | None:
    """
    Read OMNIGENT_TOOL_METRIC_ALLOWLIST (comma-separated tool names)
    once and cache. When set, tool names outside the allowlist bucket
    to "_other" on the tool.duration metric to bound cardinality.
    Returns None when unset (no bucketing).
    """
    global _tool_allowlist
    if _tool_allowlist is None:
        raw = os.environ.get("OMNIGENT_TOOL_METRIC_ALLOWLIST", "").strip()
        if raw:
            _tool_allowlist = frozenset(name.strip() for name in raw.split(",") if name.strip())
    return _tool_allowlist


def _bucket_tool_name(tool_name: str) -> str:
    """Bucket non-allowlisted tool names to '_other' if allowlist is set."""
    allowlist = _get_tool_allowlist()
    if allowlist is None:
        return tool_name
    return tool_name if tool_name in allowlist else "_other"


def _reset_instrument_cache_for_tests() -> None:
    """
    Public-named helper for test isolation: clears the lazy meter +
    instrument caches so the next call rebinds against whatever
    MeterProvider the test fixture installed.

    Tests should call this in fixture teardown to avoid the singleton
    meter leaking across tests.
    """
    global _meter, _token_usage_histogram, _operation_duration_histogram
    global _tool_duration_histogram, _tool_allowlist
    _meter = None
    _token_usage_histogram = None
    _operation_duration_histogram = None
    _tool_duration_histogram = None
    _tool_allowlist = None


def record_token_usage_metric(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """
    Emit gen_ai.client.token.usage histogram data points. One point
    per non-None token count (input + output) with gen_ai.token.type
    attribute distinguishing them.

    Silent no-op on emission error (provider unset, exporter down,
    bad value coerce). Operators see the silence via the
    omnigent.telemetry.emission.failures counter, not via raised
    exceptions in the request path.

    :param input_tokens: Prompt-token count or None.
    :param output_tokens: Completion-token count or None.
    :param provider: gen_ai.provider.name attribute value (e.g.
        "anthropic", "openai", "databricks"). Omitted when None.
    :param model: gen_ai.request.model attribute value.
    """
    try:
        histogram = _get_token_usage_histogram()
        common: dict[str, str] = {}
        if provider:
            common["gen_ai.provider.name"] = provider
        if model:
            common["gen_ai.request.model"] = model
        if input_tokens is not None:
            try:
                value = int(input_tokens)
            except (TypeError, ValueError):
                return
            histogram.record(value, attributes={**common, _ATTR_TOKEN_TYPE: "input"})
        if output_tokens is not None:
            try:
                value = int(output_tokens)
            except (TypeError, ValueError):
                return
            histogram.record(value, attributes={**common, _ATTR_TOKEN_TYPE: "output"})
    except Exception:
        _logger.debug("token-usage metric emission failed", exc_info=True)


def record_operation_duration_metric(
    *,
    duration_seconds: float,
    provider: str | None = None,
    model: str | None = None,
    error_type: str | None = None,
) -> None:
    """Emit gen_ai.client.operation.duration histogram data point."""
    try:
        histogram = _get_operation_duration_histogram()
        attributes: dict[str, str] = {"gen_ai.operation.name": "invoke_agent"}
        if provider:
            attributes["gen_ai.provider.name"] = provider
        if model:
            attributes["gen_ai.request.model"] = model
        if error_type:
            attributes["error.type"] = error_type
        histogram.record(duration_seconds, attributes=attributes)
    except Exception:
        _logger.debug("operation-duration metric emission failed", exc_info=True)


def record_tool_duration_metric(
    *,
    tool_name: str,
    duration_seconds: float,
    error_type: str | None = None,
) -> None:
    """Emit omnigent.tool.duration histogram data point with tool.name attr."""
    try:
        histogram = _get_tool_duration_histogram()
        attributes: dict[str, str] = {"tool.name": _bucket_tool_name(tool_name)}
        if error_type:
            attributes["error.type"] = error_type
        histogram.record(duration_seconds, attributes=attributes)
    except Exception:
        _logger.debug("tool-duration metric emission failed", exc_info=True)


def shutdown_metrics() -> None:
    """
    Flush + shut down the MeterProvider on process exit so the
    PeriodicExportingMetricReader (typically 60s) does not drop
    accumulated data points. Register via atexit or FastAPI lifespan.
    """
    try:
        from opentelemetry import metrics as otel_metrics

        provider = otel_metrics.get_meter_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown(timeout_millis=5000)
    except Exception:
        _logger.debug("MeterProvider shutdown failed", exc_info=True)



def record_llm_usage(span: Span, usage: dict[str, Any]) -> None:
    """
    Record token usage on an LLM span.

    Uses OTel GenAI semantic convention attributes
    (``gen_ai.usage.*``) so the data is readable by any OTel backend
    without MLflow-specific translation.

    Cache breakdown attributes are recorded only when present.
    Their absence is meaningful (the provider did not report
    caching) and should not be masked with invented zeros.

    :param span: The LLM span to annotate.
    :param usage: Token usage dict from the LLM response. Known
        keys: ``"input_tokens"``, ``"output_tokens"``,
        ``"total_tokens"``, ``"cache_read_input_tokens"``,
        ``"cache_creation_input_tokens"``.
    """
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    total = usage.get("total_tokens")
    if total is None:
        total = input_tokens + output_tokens
    span.set_attribute(_GEN_AI_INPUT_TOKENS, input_tokens)
    span.set_attribute(_GEN_AI_OUTPUT_TOKENS, output_tokens)
    span.set_attribute(_GEN_AI_TOTAL_TOKENS, int(total))
    if "cache_read_input_tokens" in usage:
        span.set_attribute(_GEN_AI_CACHE_READ_TOKENS, int(usage["cache_read_input_tokens"]))
    if "cache_creation_input_tokens" in usage:
        span.set_attribute(
            _GEN_AI_CACHE_CREATION_TOKENS, int(usage["cache_creation_input_tokens"])
        )

    # Emit the matching gen_ai.client.token.usage metric data point so
    # operators get per-key/per-model cost visibility without parsing
    # spans. The provider + model attributes come from the agent span's
    # gen_ai.provider.name / gen_ai.request.model which the executor
    # adapter set in start_agent_span (PR #1050 wiring).
    provider = span.attributes.get("gen_ai.provider.name") if span.attributes else None
    model = span.attributes.get("gen_ai.request.model") if span.attributes else None
    record_token_usage_metric(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider=str(provider) if provider else None,
        model=str(model) if model else None,
    )


def record_error(span: Span, exc: BaseException) -> None:
    """
    Mark a span as failed with an ``error.type`` attribute.

    ``span.record_exception`` captures the stack trace and message;
    this helper adds the ``error.type`` attribute (exception class
    name) so operators can filter by class in the trace backend
    without reading the exception event.

    :param span: The span to mark as failed.
    :param exc: The exception that caused the failure.
    """
    from opentelemetry.trace import StatusCode

    span.set_status(StatusCode.ERROR, str(exc))
    span.set_attribute("error.type", type(exc).__name__)
    span.set_attribute("error.message", str(exc))
    span.record_exception(exc)


def record_cancellation(span: Span) -> None:
    """
    Mark a span as cancelled.

    Neither OTel nor MLflow has a dedicated ``CANCELLED`` status, so
    we use ``ERROR`` with ``error.type = "cancelled"`` as the
    distinguishing attribute. Operators filter cancelled traces via
    the attribute.

    :param span: The span to mark as cancelled.
    """
    from opentelemetry.trace import StatusCode

    span.set_status(StatusCode.ERROR)
    span.set_attribute("error.type", "cancelled")


def get_traceparent_env() -> dict[str, str]:
    """
    Serialize the current trace context into env vars for subprocess
    inheritance.

    Used by executor subprocess launchers (Claude Agent SDK) to
    propagate the parent trace into a child process that emits its
    own OTel spans — the child's spans nest under the omnigent
    root span in the same trace.

    :returns: A dict with ``TRACEPARENT`` (and optionally
        ``TRACESTATE``) suitable for merging into the ``env`` dict
        passed to ``subprocess.Popen`` or executor SDK options.
        Empty dict when no span is active.
    """
    from opentelemetry.trace.propagation.tracecontext import (
        TraceContextTextMapPropagator,
    )

    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    result: dict[str, str] = {}
    if "traceparent" in carrier:
        result["TRACEPARENT"] = carrier["traceparent"]
    if "tracestate" in carrier:
        result["TRACESTATE"] = carrier["tracestate"]
    return result


# Standard OTel env vars forwarded into executor subprocesses so the
# child OTLP exporter targets the same collector as the parent. The
# list is intentionally small: only the knobs needed to point a fresh
# OTel SDK at the same endpoint with matching transport + auth.
_OTEL_FORWARDED_ENV_VARS: tuple[str, ...] = (
    "OTEL_EXPORTER_OTLP_PROTOCOL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_SERVICE_NAME",
)


def get_otel_subprocess_env(*, claude_sdk: bool = False) -> dict[str, str]:
    """
    Build the env-var dict that lets an executor subprocess export its
    own OTel spans to the same collector omnigent uses.

    Forwards the standard OTel exporter knobs
    (``OTEL_EXPORTER_OTLP_PROTOCOL`` / ``_ENDPOINT`` / ``_HEADERS``,
    ``OTEL_SERVICE_NAME``) so a child process can spin up its own OTel
    SDK and ship spans to the configured backend. Spawn-env builders
    merge this dict into the per-spawn env overrides passed to the
    harness wrap.

    Returns an empty dict when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
    unset. Without a configured collector, the child has nowhere to
    export to and the executor stays on its baked-in defaults so no
    half-configured OTel state leaks into the subprocess.

    **TRACEPARENT injection is intentionally NOT included.** omnigent's
    executor subprocess is long-running per session and handles many
    agent turns; the agent span is per-request and is created after
    the subprocess has already started. A single TRACEPARENT set at
    spawn time cannot represent the per-request parent context, so
    subprocess spans land in their own traces rather than nested under
    a per-request omnigent agent span. Per-request trace correlation
    over the SDK channel is tracked separately; see the design
    discussion on PR #1070.

    :param claude_sdk: When ``True``, also set
        ``CLAUDE_CODE_ENABLE_TELEMETRY=1`` and
        ``OTEL_TRACES_EXPORTER=otlp`` so the Claude Agent SDK's
        built-in telemetry hooks turn on. These are no-ops in other
        executor subprocesses, so they're gated.
    :returns: A dict suitable for merging into the env dict passed to
        an executor subprocess. Empty when no OTLP endpoint is
        configured.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return {}

    result: dict[str, str] = {}
    for name in _OTEL_FORWARDED_ENV_VARS:
        value = os.environ.get(name)
        if value is not None:
            result[name] = value
    if claude_sdk:
        result["CLAUDE_CODE_ENABLE_TELEMETRY"] = "1"
        result["OTEL_TRACES_EXPORTER"] = "otlp"
    return result


def _metrics_exporter_name() -> str:
    """
    Return the configured OpenTelemetry metrics exporter name.

    ``OTEL_METRICS_EXPORTER`` is the standard OpenTelemetry knob. If
    it is unset and an OTLP endpoint is configured, Omnigent uses
    ``"otlp"`` so server performance metrics are exported alongside
    traces.

    :returns: Exporter name, e.g. ``"otlp"`` or ``"none"``.
    """
    configured = os.environ.get("OTEL_METRICS_EXPORTER")
    if configured is not None:
        return configured.strip().lower()
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return "otlp"
    return "none"


def _otlp_protocol() -> str:
    """
    Return the configured OTLP transport protocol.

    OpenTelemetry's default OTLP protocol is gRPC; Omnigent follows
    that default unless ``OTEL_EXPORTER_OTLP_PROTOCOL`` explicitly
    requests HTTP/protobuf.

    :returns: ``"grpc"`` or ``"http/protobuf"``.
    :raises ValueError: If the protocol is unsupported.
    """
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").strip().lower()
    if protocol in ("", "grpc"):
        return "grpc"
    if protocol == "http/protobuf":
        return "http/protobuf"
    raise ValueError(f"Unsupported OTLP protocol for metrics export: {protocol!r}")


def _create_otlp_span_exporter() -> Any:
    """
    Create an OTLP span exporter using standard OTel environment vars.

    :returns: OTLP span exporter configured from the process environment.
    :raises ValueError: If ``OTEL_EXPORTER_OTLP_PROTOCOL`` is not supported.
    """
    protocol = _otlp_protocol()
    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter()
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter()


def _create_otlp_metric_exporter() -> MetricExporter:
    """
    Create an OTLP metric exporter using standard OTel environment vars.

    :returns: OTLP metric exporter configured from the process
        environment.
    :raises ValueError: If ``OTEL_EXPORTER_OTLP_PROTOCOL`` is not
        supported.
    """
    protocol = _otlp_protocol()
    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        return OTLPMetricExporter()
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter,
    )

    return OTLPMetricExporter()


def _init_otel_traces(endpoint: str) -> None:
    """
    Initialize the OpenTelemetry SDK tracer provider.

    When ``endpoint`` is set, installs a ``TracerProvider`` backed by
    an OTLP ``BatchSpanProcessor``. When absent, tracing is still
    enabled so operators who install their own provider externally get
    spans; the default no-op provider discards them silently.

    :param endpoint: ``OTEL_EXPORTER_OTLP_ENDPOINT`` value (may be empty).
    """
    try:
        if endpoint:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import SERVICE_NAME, Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            service_name = os.environ.get("OTEL_SERVICE_NAME", "omnigent")
            provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
            provider.add_span_processor(BatchSpanProcessor(_create_otlp_span_exporter()))
            trace.set_tracer_provider(provider)

        from omnigent.inner.tracing import enable_tracing

        enable_tracing()
    except Exception:
        _logger.exception("failed to initialize OpenTelemetry tracing")


def _init_otel_metrics() -> None:
    """
    Initialize the OpenTelemetry SDK meter provider when configured.

    Metrics remain no-op unless the operator configures an OTLP
    endpoint or sets ``OTEL_METRICS_EXPORTER=otlp``. Setting
    ``OTEL_METRICS_EXPORTER=none`` explicitly disables metrics.
    """
    global _metrics_initialized

    if _metrics_initialized:
        return

    exporter_name = _metrics_exporter_name()
    if exporter_name == "none":
        _metrics_initialized = True
        return
    if exporter_name != "otlp":
        _logger.warning(
            "unsupported OTEL_METRICS_EXPORTER=%s; server metrics export disabled",
            exporter_name,
        )
        _metrics_initialized = True
        return

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource

        exporter = _create_otlp_metric_exporter()
        reader = PeriodicExportingMetricReader(exporter)
        service_name = os.environ.get("OTEL_SERVICE_NAME", "omnigent")
        provider = MeterProvider(
            metric_readers=[reader],
            resource=Resource.create({SERVICE_NAME: service_name}),
        )
        otel_metrics.set_meter_provider(provider)
        _metrics_initialized = True
    except Exception:
        _logger.exception("failed to initialize OpenTelemetry metrics")
        _metrics_initialized = True


def _logs_exporter_name() -> str:
    """
    Return the configured OpenTelemetry logs exporter name.

    ``OTEL_LOGS_EXPORTER`` is the standard OpenTelemetry knob. If
    it is unset and an OTLP endpoint is configured, Omnigent uses
    ``"otlp"`` so log records flow alongside traces and metrics.

    :returns: Exporter name, e.g. ``"otlp"`` or ``"none"``.
    """
    configured = os.environ.get("OTEL_LOGS_EXPORTER")
    if configured is not None:
        return configured.strip().lower()
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return "otlp"
    return "none"


def _create_otlp_log_exporter() -> LogExporter:
    """
    Create an OTLP log exporter using standard OTel environment vars.

    :returns: OTLP log exporter configured from the process
        environment.
    :raises ValueError: If ``OTEL_EXPORTER_OTLP_PROTOCOL`` is not
        supported.
    """
    protocol = _otlp_protocol()
    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )

        return OTLPLogExporter()
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
        OTLPLogExporter,
    )

    return OTLPLogExporter()


def _init_otel_logs() -> None:
    """
    Initialize the OpenTelemetry LoggerProvider when configured.

    Bridges Python ``logging`` to OTel so logs emitted inside an
    active span carry ``trace_id`` and ``span_id`` automatically.
    No-op when no OTLP endpoint is configured or
    ``OTEL_LOGS_EXPORTER=none`` is set.

    Mirrors :func:`_init_otel_metrics`: a ``LoggerProvider`` is
    registered globally, an OTLP log exporter is attached via a
    ``BatchLogRecordProcessor``, and a ``LoggingHandler`` is
    installed on the root logger so any ``logging.getLogger`` call
    in the runtime flows through the bridge.
    """
    global _logs_initialized

    if _logs_initialized:
        return

    exporter_name = _logs_exporter_name()
    if exporter_name == "none":
        _logs_initialized = True
        return
    if exporter_name != "otlp":
        _logger.warning(
            "unsupported OTEL_LOGS_EXPORTER=%s; log bridge disabled",
            exporter_name,
        )
        _logs_initialized = True
        return

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource

        service_name = os.environ.get("OTEL_SERVICE_NAME", "omnigent")
        provider = LoggerProvider(
            resource=Resource.create({SERVICE_NAME: service_name}),
        )
        exporter = _create_otlp_log_exporter()
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        set_logger_provider(provider)

        handler = LoggingHandler(logger_provider=provider)
        root_logger = logging.getLogger()
        # Mark the handler so re-init does not stack duplicates on
        # the root logger when init() runs again after a flag reset.
        handler.set_name("omnigent-otel-log-bridge")
        for existing in root_logger.handlers:
            if existing.get_name() == "omnigent-otel-log-bridge":
                root_logger.removeHandler(existing)
        root_logger.addHandler(handler)
        _logs_initialized = True
    except Exception:
        _logger.exception("failed to initialize OpenTelemetry logs")
        _logs_initialized = True


def init() -> None:
    """
    Initialize OpenTelemetry tracing for the omnigent runtime.

    Safe to call multiple times; the second and subsequent calls
    refresh the content-capture flag but do not re-register providers.

    Two modes based on the environment:

    * **OTLP export to an external collector.** When
      ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, installs a
      ``TracerProvider`` backed by an OTLP ``BatchSpanProcessor``
      (Jaeger, Tempo, Grafana, etc.).

    * **No-op / external provider.** When the endpoint is absent,
      tracing is still enabled so operators who configure their own
      ``TracerProvider`` externally get spans automatically. The
      default OTel no-op provider discards spans silently.
    """
    global _capture_content, _initialized

    _capture_content = _env_bool("OMNIGENT_OTEL_CAPTURE_CONTENT")

    if _initialized:
        return

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    _init_otel_traces(endpoint)
    _init_otel_metrics()
    _init_otel_logs()

    _initialized = True
    _logger.info(
        "omnigent telemetry initialized (endpoint=%s, capture_content=%s)",
        endpoint or "<none>",
        _capture_content,
    )


# Atexit registration so PeriodicExportingMetricReader flushes
# its buffer on process exit (default 60s interval would drop on
# SIGTERM otherwise). Idempotent — shutdown_metrics swallows
# double-call errors.
import atexit as _atexit
_atexit.register(shutdown_metrics)

