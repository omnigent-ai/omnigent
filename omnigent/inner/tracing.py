"""OpenTelemetry tracing integration for Omnigent.

Emits structured traces for every agent turn, tool call, sub-agent
invocation, and policy evaluation so the full execution tree is visible
in any OTel-compatible backend (Jaeger, Tempo, Grafana, MLflow Traces, etc.).

Usage::

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

    from .tracing import enable_tracing
    enable_tracing()          # turns on tracing globally
    # ... run agents as normal via Session ...

Or per-session::

    session = Session(agent_def=agent_def, executor=executor)
    session.tracing_enabled = True

Span hierarchy for a typical turn::

    agent:<name>  (openinference.span.kind=AGENT)
    ├── llm_call  (openinference.span.kind=LLM)
    ├── tool:<tool_name>  (openinference.span.kind=TOOL)
    │   └── agent:<sub_agent>  (openinference.span.kind=AGENT)
    │       ├── llm_call
    │       └── tool:<sub_tool>
    ├── policy:<policy_name>  (openinference.span.kind=GUARDRAIL)
    └── llm_call
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from opentelemetry.trace import Span

from .executor import Message

logger = logging.getLogger(__name__)

# OTel span attributes accept arbitrary JSON-ish values.
TraceValue: TypeAlias = Any  # type: ignore[explicit-any]

# OpenInference semantic conventions for span kinds.
_SPAN_KIND_ATTR = "openinference.span.kind"
_SPAN_KIND_AGENT = "AGENT"
_SPAN_KIND_LLM = "LLM"
_SPAN_KIND_TOOL = "TOOL"
_SPAN_KIND_GUARDRAIL = "GUARDRAIL"

# OpenInference / OTel GenAI attribute keys for I/O.
_INPUT_VALUE = "input.value"
_OUTPUT_VALUE = "output.value"
_LLM_MODEL_NAME = "llm.model_name"

# ---------------------------------------------------------------------------
# Global enable/disable
# ---------------------------------------------------------------------------

_tracing_enabled: bool = False


def enable_tracing() -> None:
    """Enable OTel tracing globally for all Omnigent sessions."""
    global _tracing_enabled
    _tracing_enabled = True


def disable_tracing() -> None:
    """Disable OTel tracing globally."""
    global _tracing_enabled
    _tracing_enabled = False


def is_tracing_enabled() -> bool:
    return _tracing_enabled


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------


def _tracer() -> Any:
    from opentelemetry import trace

    return trace.get_tracer("omnigent")


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------


class TracingContext:
    """Holds the active span stack for a single session/turn.

    Spans are created with an explicit parent reference so we are not tied to
    thread-local or async-context-var storage — the Session explicitly
    passes its ``TracingContext`` to every helper that needs to create
    child spans.
    """

    def __init__(self, session_id: str | None = None) -> None:
        self._root_span: Span | None = None
        self._current_span: Span | None = None
        # parent span from parent context (for sub-agents)
        self._inherited_parent: Span | None = None
        self.enabled: bool = True
        # Omnigent session (conversation) id, stamped as ``session.id`` on
        # every span this context creates. An agent turn can root its own
        # trace (the response-id-seeded root, decoupled from the request
        # trace), so this attribute is what keeps those spans groupable by
        # session in the backend even when they share no trace id.
        self.session_id: str | None = session_id

    @property
    def active(self) -> bool:
        return self.enabled and self._root_span is not None

    def start_agent_span(
        self,
        agent_name: str,
        user_message: str,
        model: str | None = None,
    ) -> Span:
        """Begin the root AGENT span for a turn."""
        from opentelemetry import trace

        parent = self._current_span
        parent_ended = False
        if parent is not None:
            # A finished span is no longer recording.
            if not parent.is_recording():
                parent_ended = True

        attrs: dict[str, str] = {
            _SPAN_KIND_ATTR: _SPAN_KIND_AGENT,
            "agent.name": agent_name,
        }
        if self.session_id:
            attrs["session.id"] = self.session_id
        if model:
            attrs[_LLM_MODEL_NAME] = model
        if parent_ended and parent is not None:
            ctx = parent.get_span_context()
            if ctx is not None:
                attrs["parent_span_id"] = format(ctx.span_id, "016x")
            parent = None

        if parent is None:
            # No explicit parent span. Check if the current OTel context
            # contains the sentinel parent injected by trace_context_for_response.
            # If so, build a context that carries the trace ID but has no
            # parent span — this makes the agent span a true root span in
            # the OTLP export (parent_span_id absent), so MLflow finalizes
            # the trace status to OK instead of leaving it IN_PROGRESS.
            from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

            from omnigent.runtime.telemetry import SENTINEL_PARENT_SPAN_ID

            current_ctx = trace.get_current_span().get_span_context()
            if current_ctx is not None and current_ctx.span_id == SENTINEL_PARENT_SPAN_ID:
                # Inject a NonRecordingSpan with span_id=0 as the fake root.
                # The Python OTLP exporter skips parent_span_id when span_id
                # is 0, so the exported proto has no parentSpanId field.
                root_ctx = SpanContext(
                    trace_id=current_ctx.trace_id,
                    span_id=0,
                    is_remote=True,
                    trace_flags=TraceFlags(TraceFlags.SAMPLED),
                )
                ctx_carrier = trace.set_span_in_context(NonRecordingSpan(root_ctx))
            else:
                ctx_carrier = None
        else:
            ctx_carrier = trace.set_span_in_context(parent)
        span = _tracer().start_span(
            name=f"agent:{agent_name}",
            context=ctx_carrier,
            attributes=attrs,
        )
        span.set_attribute(_INPUT_VALUE, _truncate_str(user_message))
        if self._root_span is None:
            self._root_span = span
        self._current_span = span
        return span

    def end_agent_span(
        self,
        span: Span | None,
        response: str | None,
        error: str | None = None,
    ) -> None:
        """End an AGENT span."""
        if span is None:
            return
        from opentelemetry.trace import StatusCode

        if response is not None:
            span.set_attribute(_OUTPUT_VALUE, _truncate_str(response))
        if error:
            span.set_attribute("error.message", error)
            span.set_status(StatusCode.ERROR, error)
        else:
            span.set_status(StatusCode.OK)
        span.end()
        if span is self._root_span:
            self._root_span = None
            self._current_span = self._inherited_parent
        elif span is self._current_span:
            self._current_span = self._inherited_parent

    def start_llm_span(
        self,
        messages: list[Message] | None = None,
        model: str | None = None,
    ) -> Span:
        """Begin an LLM span for an executor.run_turn call."""
        from opentelemetry import trace

        attrs: dict[str, str] = {_SPAN_KIND_ATTR: _SPAN_KIND_LLM}
        if self.session_id:
            attrs["session.id"] = self.session_id
        if model:
            attrs[_LLM_MODEL_NAME] = model

        ctx_carrier = (
            trace.set_span_in_context(self._current_span)
            if self._current_span is not None
            else None
        )
        span = _tracer().start_span(
            name="llm_call",
            context=ctx_carrier,
            attributes=attrs,
        )
        truncated = _truncate_messages(messages)
        span.set_attribute(_INPUT_VALUE, json.dumps(truncated))
        return span

    def end_llm_span(
        self,
        span: Span | None,
        response_text: str | None = None,
        error: str | None = None,
    ) -> None:
        if span is None:
            return
        from opentelemetry.trace import StatusCode

        if response_text is not None:
            span.set_attribute(_OUTPUT_VALUE, _truncate_str(response_text))
        if error:
            span.set_attribute("error.message", error)
            span.set_status(StatusCode.ERROR, error)
        else:
            span.set_status(StatusCode.OK)
        span.end()

    def start_tool_span(
        self,
        tool_name: str,
        tool_args: dict[str, TraceValue],
    ) -> Span:
        """Begin a TOOL span."""
        from opentelemetry import trace

        ctx_carrier = (
            trace.set_span_in_context(self._current_span)
            if self._current_span is not None
            else None
        )
        span = _tracer().start_span(
            name=f"tool:{tool_name}",
            context=ctx_carrier,
            attributes={_SPAN_KIND_ATTR: _SPAN_KIND_TOOL},
        )
        if self.session_id:
            span.set_attribute("session.id", self.session_id)
        span.set_attribute("tool.name", tool_name)
        span.set_attribute(_INPUT_VALUE, _safe_serialize_str(tool_args))
        self._current_span = span
        return span

    def end_tool_span(
        self,
        span: Span | None,
        result: TraceValue = None,
        error: str | None = None,
        duration_ms: float = 0.0,
        parent_span: Span | None = None,
    ) -> None:
        if span is None:
            return
        from opentelemetry.trace import StatusCode

        span.set_attribute(_OUTPUT_VALUE, _safe_serialize_str(result))
        if duration_ms:
            span.set_attribute("duration_ms", duration_ms)
        if error:
            span.set_attribute("error.message", error)
            span.set_status(StatusCode.ERROR, error)
        else:
            span.set_status(StatusCode.OK)
        span.end()
        if span is self._current_span:
            self._current_span = parent_span

    def start_policy_span(
        self,
        policy_name: str,
        phase: str,
        content: TraceValue = None,
    ) -> Span:
        """Begin a GUARDRAIL span for a policy evaluation."""
        from opentelemetry import trace

        ctx_carrier = (
            trace.set_span_in_context(self._current_span)
            if self._current_span is not None
            else None
        )
        span = _tracer().start_span(
            name=f"policy:{policy_name}",
            context=ctx_carrier,
            attributes={
                _SPAN_KIND_ATTR: _SPAN_KIND_GUARDRAIL,
                "policy.name": policy_name,
                "policy.phase": phase,
            },
        )
        if self.session_id:
            span.set_attribute("session.id", self.session_id)
        span.set_attribute(_INPUT_VALUE, _safe_serialize_str(content))
        return span

    def end_policy_span(
        self,
        span: Span | None,
        action: str = "allow",
        reason: str | None = None,
    ) -> None:
        if span is None:
            return
        from opentelemetry.trace import StatusCode

        span.set_attribute("policy.action", action)
        if reason is not None:
            span.set_attribute("policy.reason", reason)
        if action == "deny":
            span.set_status(StatusCode.ERROR)
        else:
            span.set_status(StatusCode.OK)
        span.end()

    def create_child_context(self) -> TracingContext:
        """Create a child TracingContext for a sub-agent, parented to the
        current span of this context."""
        child = TracingContext(session_id=self.session_id)
        child.enabled = self.enabled
        child._current_span = self._current_span
        child._inherited_parent = self._current_span
        return child


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _safe_serialize(value: TraceValue, max_len: int = 4000) -> TraceValue:
    """Make a value JSON-safe for OTel span attributes."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > max_len:
            return value[:max_len] + "...(truncated)"
        return value
    if isinstance(value, dict):
        return {str(k): _safe_serialize(v, max_len) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_serialize(v, max_len) for v in value]
    try:
        s = json.dumps(value, default=str)
        if len(s) > max_len:
            return s[:max_len] + "...(truncated)"
        return s
    except Exception:  # noqa: BLE001
        s = str(value)
        if len(s) > max_len:
            return s[:max_len] + "...(truncated)"
        return s


def _safe_serialize_str(value: TraceValue, max_len: int = 4000) -> str:
    """Serialize a value to a string for OTel span string attributes."""
    serialized = _safe_serialize(value, max_len)
    if serialized is None:
        return ""
    if isinstance(serialized, str):
        return serialized
    try:
        return json.dumps(serialized)
    except Exception:  # noqa: BLE001
        return str(serialized)


def _truncate_str(value: str, max_len: int = 4000) -> str:
    if len(value) > max_len:
        return value[:max_len] + "...(truncated)"
    return value


def _truncate_messages(
    messages: list[Message] | None,
    max_messages: int = 20,
) -> list[Message]:
    """Keep the last N messages for LLM span inputs."""
    if not messages:
        return []
    truncated = messages[-max_messages:]
    result = []
    for m in truncated:
        content = m.get("content")
        if isinstance(content, str) and len(content) > 2000:
            content = content[:2000] + "...(truncated)"
        result.append(
            {
                "role": m.get("role", "unknown"),
                "content": content if content is not None else "",
            }
        )
    return result
