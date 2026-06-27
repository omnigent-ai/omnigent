"""
Agent-plane observability on top of the MLflow Tracing SDK.

See ``designs/OBSERVABILITY.md`` for the full design. The module
is intentionally thin — it holds only the omnigent-specific
concerns:

* **Trace ID derivation from the response ID.** Agent-plane response
  IDs are ``resp_<32-char hex>``. We reuse the hex suffix as the
  W3C trace ID so operators can look up a trace by its response ID
  without a lookup table. :func:`trace_context_for_response` wraps
  MLflow's public distributed-tracing entry point.

* **Runtime init.** :func:`init` flips
  ``MLFLOW_USE_DEFAULT_TRACER_PROVIDER=false`` so MLflow shares the
  global ``TracerProvider`` with raw OTel instrumentation
  (FastAPI / HTTPX) and flips ``MLFLOW_ENABLE_OTLP_EXPORTER=true``
  when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set — vendor-neutral OTLP
  export by default.

* **Subprocess trace propagation.** :func:`get_traceparent_env`
  serializes the current trace context into env vars the executor
  subprocess launchers can merge into their child process env.

* **A handful of record helpers** where the work is non-trivial
  (LLM usage normalization, cancellation tagging). Trivial
  operations like ``span.set_attribute(...)`` are called directly
  at instrumentation sites.

Call sites import this module for init + the trace-context wrapper,
and otherwise call ``mlflow`` / ``mlflow.entities.SpanType`` /
``span.set_inputs`` / ``span.set_outputs`` directly.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from mlflow.entities.span import LiveSpan
    from opentelemetry.sdk._logs.export import LogExporter
    from opentelemetry.sdk.metrics.export import MetricExporter
    from opentelemetry.sdk.trace import ReadableSpan, Span

_logger = logging.getLogger(__name__)

_RESP_PREFIX = "resp_"
_HEX_LEN = 32
_DUMMY_PARENT_SPAN_ID = "1000000000000001"
_W3C_VERSION = "00"
_W3C_FLAGS_SAMPLED = "01"

_capture_content: bool = False
_initialized: bool = False
_metrics_initialized: bool = False
_logs_initialized: bool = False


class _RemoteParentTraceState:
    """
    Trace IDs registered by the MLflow OTLP compatibility patch.

    :param lock: Lock protecting ``trace_ids``.
    :param trace_ids: OpenTelemetry trace IDs whose local root span has
        a remote parent.
    """

    def __init__(self) -> None:
        """
        Initialize empty remote-parent trace state.

        :returns: ``None``.
        """
        self.lock = threading.Lock()
        self.trace_ids: set[int] = set()


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
    read this flag before populating ``span.set_inputs`` /
    ``set_outputs`` with user messages or tool results. Content
    capture is off by default because messages may contain PII or
    secrets.

    :returns: ``True`` when content capture is enabled.
    """
    return _capture_content


def instrument_fastapi_app(app: FastAPI) -> None:
    """
    Optionally install OpenTelemetry FastAPI instrumentation on an app.

    FastAPI auto-instrumentation remains opt-in because MLflow's span
    processor has historically mishandled raw OTel spans from
    auto-instrumentors. Operators who want the standard FastAPI HTTP
    server spans and metrics can set
    ``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true``.

    :param app: FastAPI app instance to instrument.
    """
    if not _env_bool("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION"):
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        _logger.exception("failed to initialize FastAPI OpenTelemetry instrumentation")


def _patch_mlflow_otel_remote_parent_spans() -> None:
    """
    Patch MLflow's OTLP span processor for auto-instrumented server spans.

    MLflow 3.11.1 assumes that any span with a parent already has a
    matching MLflow trace registered in ``InMemoryTraceManager``. That
    assumption fails for FastAPI/ASGI auto-instrumented server spans
    whose parent is remote, e.g. a platform-provided incoming
    ``traceparent``. In that case the span is a child in the distributed
    trace but the local root for this process. Without this patch,
    MLflow passes ``trace_id=None`` into ``LiveSpan`` and request
    handling fails.

    The patch treats "parent exists but no local MLflow trace mapping"
    as a remote-parent local root: register a trace on start and pop it
    on end. Existing MLflow-managed traces continue through MLflow's
    original path.
    """
    try:
        from mlflow.entities.span import SpanType, create_mlflow_span
        from mlflow.tracing.processor.otel import OtelSpanProcessor
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        _logger.debug("MLflow OTLP span processor patch skipped", exc_info=True)
        return

    if getattr(OtelSpanProcessor, "_omnigent_remote_parent_patch", False):
        return

    original_on_end = OtelSpanProcessor.on_end

    def _remote_parent_state(processor: Any) -> _RemoteParentTraceState:
        """
        Return patch-owned trace state for one MLflow processor.

        :param processor: MLflow ``OtelSpanProcessor`` instance.
        :returns: Mutable trace state attached to ``processor``.
        """
        state = getattr(processor, "_omnigent_remote_parent_state", None)
        if state is None:
            state = _RemoteParentTraceState()
            processor._omnigent_remote_parent_state = state
        return state

    def patched_on_start(
        self: Any,
        span: Span,
        parent_context: Any = None,
    ) -> None:
        """
        Register raw OTel spans before MLflow writes invalid metadata.

        MLflow's implementation calls ``create_mlflow_span`` without
        ``span_type`` for raw OTel spans, which writes
        ``mlflow.spanType=None``. The OTLP exporter rejects ``None``
        attribute values. Treat raw OTel spans as ``UNKNOWN`` spans so
        they remain exportable.

        :param self: MLflow ``OtelSpanProcessor`` instance.
        :param span: OpenTelemetry span being started.
        :param parent_context: Optional explicit parent context passed
            by OpenTelemetry.
        """
        should_register = getattr(self, "_should_register_traces", False)
        trace_manager = getattr(self, "_trace_manager", None)
        if not should_register or trace_manager is None:
            BatchSpanProcessor.on_start(self, span, parent_context=parent_context)
            return

        if not span.parent:
            trace_info = self._create_trace_info(span)  # type: ignore[attr-defined]
            trace_id = trace_info.trace_id
            trace_manager.register_trace(span.context.trace_id, trace_info)
        else:
            trace_id = trace_manager.get_mlflow_trace_id_from_otel_id(span.context.trace_id)
            if trace_id is None:
                trace_info = self._create_trace_info(span)  # type: ignore[attr-defined]
                trace_id = trace_info.trace_id
                trace_manager.register_trace(
                    span.context.trace_id,
                    trace_info,
                    is_remote_trace=True,
                )
                state = _remote_parent_state(self)
                with state.lock:
                    state.trace_ids.add(span.context.trace_id)

        trace_manager.register_span(create_mlflow_span(span, trace_id, SpanType.UNKNOWN))
        BatchSpanProcessor.on_start(self, span, parent_context=parent_context)

    def patched_on_end(self: Any, span: ReadableSpan) -> None:
        """
        Pop remote-parent local roots after MLflow exports the span.

        :param self: MLflow ``OtelSpanProcessor`` instance.
        :param span: OpenTelemetry span being ended.
        """
        original_on_end(self, span)
        state = getattr(self, "_omnigent_remote_parent_state", None)
        if state is None:
            return
        should_pop = False
        with state.lock:
            if span.context.trace_id in state.trace_ids:
                state.trace_ids.remove(span.context.trace_id)
                should_pop = True
        if should_pop:
            trace_manager = getattr(self, "_trace_manager", None)
            if trace_manager is not None:
                trace_manager.pop_trace(span.context.trace_id)

    OtelSpanProcessor.on_start = patched_on_start
    OtelSpanProcessor.on_end = patched_on_end
    OtelSpanProcessor._omnigent_remote_parent_patch = True


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
    ``response_id``, then calls MLflow's public distributed-tracing
    API to register the trace and make it current. Any span started
    inside the context manager inherits this trace ID.

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
    from mlflow.tracing.distributed import (
        set_tracing_context_from_http_request_headers,
    )

    effective = root_response_id or response_id
    trace_id_hex = trace_id_from_response_id(effective)
    traceparent = f"{_W3C_VERSION}-{trace_id_hex}-{_DUMMY_PARENT_SPAN_ID}-{_W3C_FLAGS_SAMPLED}"
    with set_tracing_context_from_http_request_headers({"traceparent": traceparent}):
        yield


def record_llm_usage(span: LiveSpan, usage: dict[str, Any]) -> None:
    """
    Record token usage on an LLM span.

    MLflow stores usage as a single JSON dict under
    ``mlflow.chat.tokenUsage`` and translates each field to the
    corresponding ``gen_ai.usage.*`` attribute on OTLP export —
    ``input_tokens``, ``output_tokens``, ``total_tokens``, plus
    optional cache fields.

    Cache breakdown attributes are recorded only when present.
    Their absence is meaningful (the provider did not report
    caching) and should not be masked with invented zeros.

    :param span: The LLM span to annotate.
    :param usage: Token usage dict from the LLM response. Known
        keys: ``"input_tokens"``, ``"output_tokens"``,
        ``"total_tokens"``, ``"cache_read_input_tokens"``,
        ``"cache_creation_input_tokens"``.
    """
    from mlflow.tracing.constant import SpanAttributeKey, TokenUsageKey

    payload: dict[str, int] = {
        TokenUsageKey.INPUT_TOKENS: int(usage.get("input_tokens", 0)),
        TokenUsageKey.OUTPUT_TOKENS: int(usage.get("output_tokens", 0)),
    }
    total = usage.get("total_tokens")
    if total is None:
        total = payload[TokenUsageKey.INPUT_TOKENS] + payload[TokenUsageKey.OUTPUT_TOKENS]
    payload[TokenUsageKey.TOTAL_TOKENS] = int(total)
    if "cache_read_input_tokens" in usage:
        payload[TokenUsageKey.CACHE_READ_INPUT_TOKENS] = int(usage["cache_read_input_tokens"])
    if "cache_creation_input_tokens" in usage:
        payload[TokenUsageKey.CACHE_CREATION_INPUT_TOKENS] = int(
            usage["cache_creation_input_tokens"]
        )
    span.set_attribute(SpanAttributeKey.CHAT_USAGE, payload)


def record_error(span: LiveSpan, exc: BaseException) -> None:
    """
    Mark a span as failed with an ``error.type`` attribute.

    MLflow's ``span.record_exception`` already captures the stack
    trace and message; this helper adds the ``error.type``
    attribute (exception class name) so operators can filter by
    class in the trace backend without reading the exception event.

    ``exc`` is typed ``Exception`` (not ``BaseException``) to match
    MLflow's ``record_exception`` signature and because every
    in-tree caller catches ``Exception`` or a subclass; we don't
    report telemetry for ``KeyboardInterrupt`` / ``SystemExit``.

    :param span: The span to mark as failed.
    :param exc: The exception that caused the failure.
    """
    from mlflow.entities.span_status import SpanStatusCode

    span.set_status(SpanStatusCode.ERROR)
    span.set_attribute("error.type", type(exc).__name__)
    span.set_attribute("error.message", str(exc))
    span.record_exception(exc)


def record_cancellation(span: LiveSpan) -> None:
    """
    Mark a span as cancelled.

    Neither OTel nor MLflow has a dedicated ``CANCELLED`` status, so
    we use ``ERROR`` with ``error.type = "cancelled"`` as the
    distinguishing attribute. Operators filter cancelled traces via
    the attribute.

    :param span: The span to mark as cancelled.
    """
    from mlflow.entities.span_status import SpanStatusCode

    span.set_status(SpanStatusCode.ERROR)
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
    Build the env-var dict an executor subprocess needs to nest its
    spans under the omnigent parent span.

    Combines :func:`get_traceparent_env` (W3C ``TRACEPARENT`` /
    ``TRACESTATE`` for parent-context propagation) with the standard
    OTel exporter knobs (``OTEL_EXPORTER_OTLP_PROTOCOL`` /
    ``_ENDPOINT`` / ``_HEADERS``, ``OTEL_SERVICE_NAME``) so a child
    process can spin up its own OTel SDK and ship spans to the same
    collector. Spawn-env builders merge this dict into the per-spawn
    env overrides passed to the harness wrap.

    Returns an empty dict when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is
    unset. Without a configured collector, the child has nowhere to
    export to and the executor stays on its baked-in defaults — no
    half-configured OTel state leaks into the subprocess.

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
    # TRACEPARENT / TRACESTATE come from the active OTel context. With
    # no active span the propagator emits no carrier entries, so
    # ``get_traceparent_env`` returns an empty dict — the child still
    # gets the exporter knobs but has no parent to nest under.
    result.update(get_traceparent_env())
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


def _create_otlp_metric_exporter() -> MetricExporter:
    """
    Create an OTLP metric exporter using standard OTEL environment vars.

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
    Create an OTLP log exporter using standard OTEL environment vars.

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
    Initialize MLflow Tracing for the omnigent runtime.

    Safe to call multiple times; the second and subsequent calls
    refresh the content-capture flag but do not re-register providers.

    Three modes based on the environment:

    * **OTLP export to an external collector.** When
      ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, we flip
      ``MLFLOW_ENABLE_OTLP_EXPORTER=true`` so MLflow exports via
      OTLP to the operator's collector (Jaeger, Tempo, MLflow
      tracking server's ``/v1/traces``, etc.) rather than to an
      MLflow tracking server's internal store.

    * **MLflow tracking server.** When ``OTEL_EXPORTER_OTLP_ENDPOINT``
      is unset but ``MLFLOW_TRACKING_URI`` is set, MLflow exports
      traces to the configured tracking server. We leave this path
      untouched.

    * **No-op.** When neither is set, MLflow emits no-op spans —
      zero overhead on span creation.

    Unified mode (``MLFLOW_USE_DEFAULT_TRACER_PROVIDER=false``) is
    forced so the global OTel ``TracerProvider`` is shared between
    MLflow and raw OTel instrumentation (FastAPI, HTTPX).
    """
    global _capture_content, _initialized

    _capture_content = _env_bool("OMNIGENT_OTEL_CAPTURE_CONTENT")

    if _initialized:
        return

    # Unified provider mode: MLflow shares the global TracerProvider
    # with raw OTel instrumentation so FastAPI/HTTPX auto-instrumented
    # spans live in the same trace as our MLflow spans.
    os.environ.setdefault("MLFLOW_USE_DEFAULT_TRACER_PROVIDER", "false")

    # When an OTLP endpoint is configured, explicitly flip MLflow's
    # OTLP exporter flag. MLflow requires this in addition to the
    # standard OTel env vars (which it also respects).
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        os.environ.setdefault("MLFLOW_ENABLE_OTLP_EXPORTER", "true")

    try:
        _patch_mlflow_otel_remote_parent_spans()
        import mlflow.tracing

        mlflow.tracing.enable()

        # Enable the inner tracing module so TracingContext spans are
        # created for every agent turn. Without this, telemetry.init()
        # sets up the OTel provider but no spans are emitted because the
        # per-session tracing flag stays False.
        from omnigent.inner.tracing import enable_tracing

        enable_tracing()
    except ImportError:
        # mlflow is an optional dependency (`omnigent[tracing]`). When it
        # is absent, tracing is simply disabled — degrade quietly rather
        # than logging a full traceback on every server start.
        _logger.info(
            "MLflow not installed; tracing disabled. Install `omnigent[tracing]` to enable it."
        )
    except Exception:
        _logger.exception("failed to initialize MLflow tracing")

    _init_otel_metrics()
    _init_otel_logs()

    # NOTE: FastAPI auto-instrumentation remains opt-in via
    # ``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION=true``. MLflow's span
    # processor has historically mishandled raw OTel spans from
    # auto-instrumentors. See ``instrument_fastapi_app`` for the
    # guarded integration point used by the server factory.

    _initialized = True
    _logger.info(
        "omnigent telemetry initialized (endpoint=%s, capture_content=%s)",
        endpoint or "<none>",
        _capture_content,
    )
