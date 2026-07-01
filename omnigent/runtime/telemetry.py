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

* **Subprocess trace propagation.** :func:`get_traceparent_env`
  serializes the current trace context into env vars the executor
  subprocess launchers can merge into their child process env.

* **A handful of record helpers** where the work is non-trivial
  (LLM usage normalization, cancellation tagging). Trivial
  operations like ``span.set_attribute(...)`` are called directly
  at instrumentation sites.
"""

from __future__ import annotations

import contextvars
import logging
import os
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from opentelemetry.context import Context
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

# Session (conversation) id for the current execution context. Set once at a
# session boundary (request hook, executor turn, forwarder task); the
# _SessionIdSpanProcessor reads it on_start and stamps `session.id` on EVERY
# span created in that context — so runner/harness operations are tagged
# generically, with no per-operation code. Default None = no stamping.
_session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "omnigent_session_id", default=None
)


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


def telemetry_enabled() -> bool:
    """
    Return whether telemetry is enabled for this process.

    Master opt-in controlled by ``OMNIGENT_TELEMETRY_ENABLED`` (off by
    default). When it is unset/false, :func:`init` is a no-op and every
    instrumentor and manual-span helper short-circuits — a default
    install installs no instrumentation, creates no spans, and emits no
    telemetry, so users who never opt in pay nothing. Read directly from
    the environment (not cached) so it is correct no matter the order in
    which instrumentation entry points run relative to :func:`init`.

    :returns: ``True`` when ``OMNIGENT_TELEMETRY_ENABLED`` is truthy.
    """
    return _env_bool("OMNIGENT_TELEMETRY_ENABLED")


# Max characters of a serialized payload to attach to a span. Bodies can be
# large; the trace backend is not a payload store, so cap aggressively.
_CONTENT_MAX_LEN = 4096

# Substrings that mark a payload key as a secret to redact even when content
# capture is on — a frame body like ``host.launch_runner`` carries a
# ``binding_token``, and we never want a credential on a span.
_REDACT_KEY_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "api_key",
    "apikey",
)


def _redact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """
    Copy a message payload, redacting secret-looking values.

    Drops the W3C propagation keys (they ride in the envelope, not the
    message) and replaces any value whose key looks like a credential
    with ``"[redacted]"``. Non-secret values pass through unchanged.

    :param payload: The decoded frame / message dict.
    :returns: A shallow copy safe to serialize onto a span.
    """
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = key.lower()
        if lowered in ("traceparent", "tracestate"):
            continue
        if any(token in lowered for token in _REDACT_KEY_SUBSTRINGS):
            redacted[key] = "[redacted]"
        else:
            redacted[key] = value
    return redacted


def _payload_to_attribute(payload: Mapping[str, Any]) -> str:
    """
    Serialize a redacted payload to a length-capped JSON string.

    :param payload: The message dict to record.
    :returns: A JSON string, truncated to :data:`_CONTENT_MAX_LEN`.
    """
    import json

    redacted = _redact_payload(payload)
    try:
        text = json.dumps(redacted, default=str)
    except (TypeError, ValueError):
        text = str(redacted)
    if len(text) > _CONTENT_MAX_LEN:
        text = text[:_CONTENT_MAX_LEN] + "…[truncated]"
    return text


def record_message_payload(
    payload: Mapping[str, Any],
    *,
    span: Any = None,
    key: str = "omnigent.message.payload",
) -> None:
    """
    Attach a message payload to a span, gated by content capture.

    No-op unless ``OMNIGENT_OTEL_CAPTURE_CONTENT`` is set — payloads may
    hold PII, so capturing the literal body of an inter-service message
    is opt-in. The payload is redacted (:func:`_redact_payload`) and
    length-capped before it is recorded.

    :param payload: The decoded frame / message dict to record.
    :param span: The span to annotate. Defaults to the currently active
        span; a no-op if none is recording.
    :param key: The span attribute name to use.
    """
    if not should_capture_content():
        return
    target = span
    if target is None:
        from opentelemetry import trace as otel_trace

        target = otel_trace.get_current_span()
    if target is None or not getattr(target, "is_recording", lambda: False)():
        return
    target.set_attribute(key, _payload_to_attribute(payload))


def set_session_id(session_id: str | None) -> None:
    """
    Stamp ``session.id`` on the currently active span.

    For session-scoped work where the conversation id is NOT in the
    request path — notably ``POST /v1/sessions`` (create), where the id
    is minted server-side and returned in the body, so the path-based
    :func:`_fastapi_session_id_hook` cannot tag it. Call this once the id
    is known so the span joins the session's ``session.id`` group.

    Best-effort and gated by the master opt-in: a no-op when telemetry is
    off, the id is falsy, or no span is recording.

    :param session_id: The Omnigent session (conversation) id, e.g.
        ``"conv_…"``.
    """
    if not session_id or not telemetry_enabled():
        return
    # Bind for the rest of this context so child spans (e.g. the create's DB
    # writes) get tagged by _SessionIdSpanProcessor too; then stamp the
    # already-started active span directly (its on_start has already passed).
    _session_id_var.set(session_id)
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute("session.id", session_id)
    except Exception:  # pragma: no cover - telemetry must never break requests
        pass


def current_session_id() -> str | None:
    """
    Return the session id bound in the current context, or ``None``.

    Lets a caller (e.g. the executor adapter) prefer the conversation id the
    request hook already bound — authoritative, from the ``/sessions/<conv>/``
    path — over a less-reliable local key.

    :returns: The active ``session.id`` value, or ``None`` if unset.
    """
    return _session_id_var.get()


@contextmanager
def session_scope(session_id: str | None) -> Iterator[None]:
    """
    Bind a session id for the current execution context and its children.

    Every span started while this scope is active is tagged with
    ``session.id`` by :class:`_SessionIdSpanProcessor`. Set it ONCE at a
    session boundary — the FastAPI request hook, an executor turn, a
    forwarder task — so all runner/harness operations (current and future,
    including DB/httpx child spans) get the attribute generically, instead
    of stamping each span by hand. No-op for a falsy id or when telemetry
    is off.

    :param session_id: The Omnigent session (conversation) id, e.g. ``conv_…``.
    """
    if not session_id or not telemetry_enabled():
        yield
        return
    token = _session_id_var.set(session_id)
    try:
        yield
    finally:
        _session_id_var.reset(token)


def _make_session_id_processor() -> Any:
    """
    Build a span processor that stamps ``session.id`` from the active
    :data:`_session_id_var` onto every recording span.

    Registered on the runtime ``TracerProvider`` (:func:`_init_otel_traces`)
    so the session id flows onto all spans — server, runner, harness, and any
    future operation — with no per-call-site code. Subclasses the SDK
    ``SpanProcessor`` so it satisfies the full processor interface (e.g. the
    internal ``_on_ending`` hook); only ``on_start`` is overridden. Built
    lazily because the OTel SDK is not a hard import dependency of this module.

    :returns: A ``SpanProcessor`` instance.
    """
    from opentelemetry.sdk.trace import SpanProcessor

    class _SessionIdSpanProcessor(SpanProcessor):
        def on_start(self, span: Any, parent_context: Any = None) -> None:
            try:
                session_id = _session_id_var.get()
                if session_id and span.is_recording():
                    span.set_attribute("session.id", session_id)
            except Exception:  # pragma: no cover - telemetry must never break spans
                pass

    return _SessionIdSpanProcessor()


def _fastapi_instrumentation_enabled() -> bool:
    """
    Decide whether to install FastAPI server instrumentation.

    Default-on when a tracing backend is configured
    (``OTEL_EXPORTER_OTLP_ENDPOINT`` is set) — that is the only situation
    where HTTP server spans have somewhere to go, and it is where
    end-to-end trace propagation across the server / runner / harness
    ASGI apps matters.

    The explicit ``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION`` flag always
    wins when set: ``true`` forces it on (e.g. with an in-memory
    exporter in tests), any other value forces it off. Bare installs
    with no backend stay uninstrumented, avoiding span overhead for
    users who are not tracing.

    :returns: ``True`` if FastAPI instrumentation should be installed.
    """
    if not telemetry_enabled():
        return False
    explicit = os.environ.get("OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION")
    if explicit is not None:
        return explicit.strip().lower() in ("true", "1", "yes")
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())


# Session id as it appears in a request path: ``/v1/sessions/<conv_…>/...``
# (runner-internal conversations use the ``agy_conv_`` prefix). Used to stamp
# ``session.id`` onto the auto-created FastAPI server span.
_SESSION_ID_IN_PATH = re.compile(r"/sessions/((?:agy_)?conv_[0-9a-f]+)")


def _fastapi_session_id_hook(span: Any, scope: Mapping[str, Any]) -> None:
    """
    FastAPI server-request hook: stamp ``session.id`` from the request path.

    Runs on every server span ``FastAPIInstrumentor`` creates. When the path
    is a session-scoped route it tags the span with the Omnigent session
    (conversation) id, so server and runner request spans — POST ``/events``,
    the SSE stream, and the tunneled server→runner hops — share the
    ``session.id`` grouping key. This is also what links the *decoupled* JSONL
    forwarder (which re-POSTs into ``/events`` under its own trace) back to a
    session: its server-side span carries the id even though it cannot share
    the originating request's trace context.

    Best-effort and defensive — telemetry must never break request handling.

    :param span: The server span started by ``FastAPIInstrumentor``.
    :param scope: The ASGI connection scope; ``scope["path"]`` is the route.
    """
    try:
        match = _SESSION_ID_IN_PATH.search(scope.get("path") or "")
        if not match:
            return
        session_id = match.group(1)
        # Bind for the request's whole span tree (DB, httpx, policy, …) via the
        # processor, then stamp the server span directly (it already started, so
        # the processor's on_start has already run for it).
        _session_id_var.set(session_id)
        if span is not None and span.is_recording():
            span.set_attribute("session.id", session_id)
    except Exception:  # pragma: no cover - telemetry must never break requests
        pass


def instrument_fastapi_app(app: FastAPI) -> None:
    """
    Install OpenTelemetry FastAPI server instrumentation on an app.

    Enabled by default whenever a tracing backend is configured (see
    :func:`_fastapi_instrumentation_enabled`); set
    ``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION`` to force it on or off.
    Installing it on the server, runner, and harness ASGI apps is what
    continues an incoming ``traceparent`` across each HTTP boundary.

    :param app: FastAPI app instance to instrument.
    """
    if not _fastapi_instrumentation_enabled():
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, server_request_hook=_fastapi_session_id_hook)
    except Exception:
        _logger.exception("failed to initialize FastAPI OpenTelemetry instrumentation")


def _instrument_httpx() -> None:
    """
    Install OpenTelemetry HTTPX client instrumentation process-wide.

    Wraps every ``httpx`` client so outbound requests inject the W3C
    ``traceparent`` header and emit a client span. This is what carries
    the trace across the TUI/SDK client → server hop, the server →
    runner reverse-tunnel (whose transport forwards request headers
    verbatim), and the native-harness policy HTTP hook.

    Disabled when ``OMNIGENT_OTEL_HTTP_CLIENT_INSTRUMENTATION=false``.
    Set this to suppress internal API call spans (server↔runner↔harness
    requests) from appearing in the trace backend alongside agent spans.

    Idempotent: ``HTTPXClientInstrumentor`` no-ops if already
    instrumented. Failures degrade quietly — tracing is best-effort and
    must never break request handling.
    """
    explicit = os.environ.get("OMNIGENT_OTEL_HTTP_CLIENT_INSTRUMENTATION")
    if explicit is not None and explicit.strip().lower() not in ("true", "1", "yes"):
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except Exception:
        _logger.exception("failed to initialize HTTPX OpenTelemetry instrumentation")


def instrument_httpx_client(client: Any) -> None:
    """
    Instrument a single httpx client built on a custom transport.

    The process-wide :func:`_instrument_httpx` only patches httpx's
    *standard* transports' request methods. A client constructed with a
    custom :class:`~httpx.AsyncBaseTransport` — notably the server→runner
    ``WSTunnelTransport`` — defines its own ``handle_async_request`` and
    is therefore invisible to that global hook: its outbound requests
    carry no ``traceparent`` and emit no client span, so the runner roots
    a fresh trace instead of nesting under the caller's span.

    ``instrument_client`` wraps the client *instance* directly, which does
    work over a custom transport. Calling it on the cached per-runner
    client makes the synchronous event-forward propagate the active trace
    context across the tunnel, so the runner's request span becomes a
    child of the originating server request — one connected trace across
    the server→runner boundary.

    Best-effort: failures degrade quietly — tracing must never break
    request handling.

    :param client: The ``httpx.AsyncClient`` (or sync ``Client``) to
        instrument in place.
    """
    if not telemetry_enabled():
        return
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor.instrument_client(client)
    except Exception:
        _logger.exception("failed to instrument httpx client over custom transport")


def instrument_sqlalchemy_engine(engine: Any) -> None:
    """
    Install OpenTelemetry instrumentation on a single SQLAlchemy engine.

    Every statement executed on ``engine`` becomes a child span under
    the active trace, so a request's database work shows up inline in
    the trace waterfall. Called once per engine at creation time (see
    ``omnigent.db.utils.get_or_create_engine``).

    ``opentelemetry-instrumentation-sqlalchemy`` is an optional
    dependency; when it is absent (bare installs without the tracing
    extras) this degrades to a no-op. Other failures are logged but not
    raised — instrumentation must never block engine creation.

    :param engine: The SQLAlchemy :class:`~sqlalchemy.engine.Engine` to
        instrument.
    """
    if not telemetry_enabled():
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine)
    except ImportError:
        _logger.debug("SQLAlchemy OpenTelemetry instrumentation not installed; skipping")
    except Exception:
        _logger.exception("failed to instrument SQLAlchemy engine")


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


def inject_trace_context(carrier: dict[str, str]) -> dict[str, str]:
    """
    Inject the active W3C trace context into a dict carrier.

    For the JSON-frame transports that no auto-instrumentor can see —
    the host-daemon tunnel (``host/frames.py``) and the session-updates
    websocket — call this on the **send** side so the frame carries a
    ``traceparent`` (and ``tracestate`` when present). The receiver pairs
    it with :func:`extract_trace_context`.

    Uses the global OpenTelemetry propagator (W3C Trace Context by
    default), so the standard lowercase header names are written. When no
    span is active the carrier is left unchanged.

    :param carrier: A mutable string-keyed dict, typically the frame
        about to be serialized to JSON.
    :returns: The same ``carrier`` for convenient chaining.
    """
    from opentelemetry.propagate import inject

    inject(carrier)
    return carrier


def extract_trace_context(carrier: Mapping[str, str]) -> Context:
    """
    Extract a remote W3C trace context from a dict carrier.

    The **receive**-side counterpart to :func:`inject_trace_context`.
    Pass the returned context to ``start_as_current_span(context=...)``
    so the span created while handling the frame nests under the sender's
    trace instead of rooting a new one.

    :param carrier: A string-keyed mapping that may hold ``traceparent`` /
        ``tracestate`` keys, e.g. a decoded JSON frame.
    :returns: An OpenTelemetry :class:`~opentelemetry.context.Context`.
        When the carrier holds no trace headers this is the current
        (possibly empty) context, so spans started under it simply root a
        new trace.
    """
    from opentelemetry.propagate import extract

    return extract(carrier)


@contextmanager
def consume_frame_span(
    name: str,
    carrier: Mapping[str, str],
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """
    Open a CONSUMER span parented on a received frame's trace context.

    The receive-side wrapper for the JSON-frame websockets: extracts the
    W3C context that :func:`inject_trace_context` wrote into ``carrier``,
    then opens a span that nests under the sender's trace. Any frame
    encoded while this span is active (e.g. a result frame sent back in
    reply) inherits it, so request/response round trips stay linked.

    :param name: Span name, e.g. ``"host.launch_runner"``.
    :param carrier: The decoded inbound frame (a JSON dict) that may
        hold ``traceparent`` / ``tracestate`` keys.
    :param attributes: Optional span attributes to set, e.g.
        ``{"host.request_id": "req_1"}``.
    :returns: A context manager yielding the started span.
    """
    if not telemetry_enabled():
        from opentelemetry.trace import INVALID_SPAN

        yield INVALID_SPAN
        return
    from opentelemetry import trace as otel_trace

    parent = extract_trace_context(carrier)
    tracer = otel_trace.get_tracer("omnigent.frames")
    with tracer.start_as_current_span(
        name,
        context=parent,
        kind=otel_trace.SpanKind.CONSUMER,
    ) as span:
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        # Record the received message body (redacted, capped) when content
        # capture is on, so operators can see exactly what crossed the wire.
        record_message_payload(carrier, span=span)
        yield span


@contextmanager
def span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[Any]:
    """
    Open a plain child span under the currently active trace context.

    A thin wrapper over the OpenTelemetry tracer for instrumenting
    infrastructure boundaries that are not JSON-frame transports — e.g.
    a terminal-attach session or an in-process policy evaluation. The
    parent is whatever context is active (a FastAPI-extracted request
    span, an agent-turn span, etc.), so the new span nests correctly
    without any explicit carrier.

    :param name: Span name, e.g. ``"terminal.attach"`` or
        ``"policy.evaluate"``.
    :param attributes: Optional span attributes to set at start.
    :returns: A context manager yielding the started span.
    """
    if not telemetry_enabled():
        from opentelemetry.trace import INVALID_SPAN

        yield INVALID_SPAN
        return
    from opentelemetry import trace as otel_trace

    tracer = otel_trace.get_tracer("omnigent")
    with tracer.start_as_current_span(name) as started:
        for key, value in (attributes or {}).items():
            started.set_attribute(key, value)
        yield started


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
            # Enrich every span with session.id from the active context (set via
            # session_scope at the request hook / executor turn / forwarder).
            provider.add_span_processor(_make_session_id_processor())
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


def init(service_name: str | None = None) -> None:
    """
    Initialize OpenTelemetry tracing for the omnigent runtime.

    Gated by the ``OMNIGENT_TELEMETRY_ENABLED`` master opt-in (off by
    default): when it is unset/false this is a no-op — no provider is
    installed, no instrumentation is wired, and no spans are created, so
    a default install incurs zero telemetry cost. Set it truthy to opt
    in; ``OTEL_EXPORTER_OTLP_ENDPOINT`` then selects the export target.

    Safe to call multiple times; the second and subsequent calls
    refresh the content-capture flag but do not re-register providers.

    :param service_name: The OpenTelemetry ``service.name`` for this
        process, e.g. ``"omni-server"`` / ``"omni-runner"`` /
        ``"omni-harness"`` / ``"omni-host"``. Each entrypoint names
        itself so the trace backend can attribute spans to a component;
        a child process overrides the parent's inherited name with its
        own. When ``None``, an operator-set ``OTEL_SERVICE_NAME`` is
        honored, otherwise it defaults to ``"omnigent"``. Deployment-
        level identity (environment, region) belongs in
        ``OTEL_RESOURCE_ATTRIBUTES``.

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

    if not telemetry_enabled():
        # Master opt-in off (the default): stay fully inert — no provider, no
        # OTEL_SERVICE_NAME mutation, no httpx/metrics/logs instrumentation, no
        # spans. Only operators who set OMNIGENT_TELEMETRY_ENABLED pay any cost.
        return

    _capture_content = _env_bool("OMNIGENT_OTEL_CAPTURE_CONTENT")

    if _initialized:
        return

    # Per-component service identity. The trace / metrics / logs
    # providers below all read OTEL_SERVICE_NAME when they build their
    # Resource, so this must run before any provider is created. A
    # passed name wins over an inherited one so each process (server /
    # runner / harness / host) is attributable in the trace backend
    # instead of collapsing to one anonymous service.
    os.environ["OTEL_SERVICE_NAME"] = (
        service_name or os.environ.get("OTEL_SERVICE_NAME") or "omnigent"
    )

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    _init_otel_traces(endpoint)
    _init_otel_metrics()
    _init_otel_logs()
    _instrument_httpx()

    # NOTE: FastAPI server instrumentation is installed by the app
    # factories (server / runner / harness) via ``instrument_fastapi_app``.
    # It defaults on when a tracing backend is configured and can be
    # forced on/off with ``OMNIGENT_OTEL_FASTAPI_INSTRUMENTATION``.

    _initialized = True
    _logger.info(
        "omnigent telemetry initialized (endpoint=%s, capture_content=%s)",
        endpoint or "<none>",
        _capture_content,
    )
