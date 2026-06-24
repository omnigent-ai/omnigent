"""MLflow tracing integration for Omnigent.

Emits structured traces for every agent turn, tool call, sub-agent
invocation, and policy evaluation so the full execution tree is visible
in the MLflow Traces UI.

Usage::

    import mlflow
    from .tracing import enable_tracing

    mlflow.set_tracking_uri("sqlite:///mlflow.db")
    mlflow.set_experiment("my_agent")

    enable_tracing()          # turns on tracing globally
    # ... run agents as normal via Session ...

Or per-session::

    session = Session(agent_def=agent_def, executor=executor)
    session.tracing_enabled = True

Span hierarchy for a typical turn::

    agent:<name>  (AGENT)
    ├── llm_call  (CHAT_MODEL)
    ├── tool:<tool_name>  (TOOL)
    │   └── agent:<sub_agent>  (AGENT)          # if the tool is a sub-agent
    │       ├── llm_call  (CHAT_MODEL)
    │       └── tool:<sub_tool>  (TOOL)
    ├── policy:<policy_name>  (GUARDRAIL)
    └── llm_call  (CHAT_MODEL)

Requirements:
    ``mlflow`` must be installed (``pip install 'omnigent[tracing]'``).
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import TYPE_CHECKING, Any, TypeAlias, cast

if TYPE_CHECKING:
    from mlflow.entities.span import LiveSpan

from .executor import Message

logger = logging.getLogger(__name__)

# MLflow span attributes / inputs / outputs accept arbitrary JSON-ish
# values; the serialisation helpers below traverse them with explicit
# isinstance checks, so the type is genuinely heterogeneous at the
# tracing boundary.
TraceValue: TypeAlias = Any  # type: ignore[explicit-any]

# ---------------------------------------------------------------------------
# Global enable/disable
# ---------------------------------------------------------------------------

_tracing_enabled: bool = False


def enable_tracing() -> None:
    """Enable MLflow tracing globally for all Omnigent sessions."""
    global _tracing_enabled
    _tracing_enabled = True


def disable_tracing() -> None:
    """Disable MLflow tracing globally."""
    global _tracing_enabled
    _tracing_enabled = False


def is_tracing_enabled() -> bool:
    return _tracing_enabled


# ---------------------------------------------------------------------------
# Lazy MLflow import
# ---------------------------------------------------------------------------


def _mlflow() -> ModuleType:
    """Lazily import mlflow so the module can be loaded without it installed."""
    try:
        import mlflow
    except ImportError as exc:
        raise ImportError(
            "MLflow tracing requires the 'mlflow' package. "
            "Install it with: pip install 'omnigent[tracing]'"
        ) from exc
    return mlflow


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------


class TracingContext:
    """Holds the active span stack for a single session/turn.

    Spans are created via ``start_span_no_context`` so we are not tied to
    thread-local or async-context-var storage — the Session explicitly
    passes its ``TracingContext`` to every helper that needs to create
    child spans.
    """

    def __init__(self) -> None:
        self._root_span: LiveSpan | None = None
        self._current_span: LiveSpan | None = None
        # parent span from parent context (for sub-agents)
        self._inherited_parent: LiveSpan | None = None
        self.enabled: bool = True

    @property
    def active(self) -> bool:
        return self.enabled and self._root_span is not None

    def start_agent_span(
        self,
        agent_name: str,
        user_message: str,
        model: str | None = None,
    ) -> LiveSpan:
        """Begin the root AGENT span for a turn."""
        mlflow = _mlflow()
        parent = self._current_span
        # If the intended parent span has already been ended (e.g. the
        # parent turn finished before this async sub-agent ran), don't
        # pass it — MLflow rejects ended parents.  Instead, start a new
        # root trace and record the lineage as an attribute.
        parent_ended = False
        if parent is not None:
            # LiveSpan.end_time_ns is set to a positive int when ended.
            end_time_ns = parent.end_time_ns
            if isinstance(end_time_ns, int) and end_time_ns > 0:
                parent_ended = True
        attrs: dict[str, str] = {"agent.name": agent_name}
        if model:
            attrs["model"] = model
        if parent_ended and parent is not None:
            attrs["parent_span_id"] = str(parent.span_id)
            parent = None
        span = cast(
            "LiveSpan",
            mlflow.start_span_no_context(
                name=f"agent:{agent_name}",
                span_type="AGENT",
                parent_span=parent,
                inputs={"user_message": user_message},
                attributes=attrs,
            ),
        )
        if self._root_span is None:
            self._root_span = span
        self._current_span = span
        return span

    def end_agent_span(
        self,
        span: LiveSpan | None,
        response: str | None,
        status: str = "OK",
        error: str | None = None,
    ) -> None:
        """End an AGENT span.

        :param span: Span handle returned by ``start_agent_span``, or
            ``None`` when tracing was disabled at start time.
        :param response: Final assistant text attached to the span's
            outputs. ``None`` when no response was produced (e.g.
            turn ended mid-tool without a TurnComplete).
        :param status: MLflow span status, e.g. ``"OK"`` / ``"ERROR"``.
        :param error: Optional error message attached as
            ``error.message``; also forces the status to ``"ERROR"``.
        """
        if span is None:
            return
        span.set_outputs({"response": response})
        if error:
            span.set_attribute("error.message", error)
            span.set_status("ERROR")
        else:
            span.set_status(status)
        span.end()
        if span is self._root_span:
            self._root_span = None
            # Restore to inherited parent (for child contexts) or None.
            self._current_span = self._inherited_parent
        elif span is self._current_span:
            self._current_span = self._inherited_parent

    def start_llm_span(
        self,
        messages: list[Message] | None = None,
        model: str | None = None,
        operation: str = "chat",
    ) -> LiveSpan:
        """Begin a CHAT_MODEL span for an executor.run_turn call.

        :param messages: Conversation messages sent to the model.
        :param model: Provider-prefixed model string, e.g.
            ``"openai/gpt-5.4"``. The provider prefix is split out so the
            ``gen_ai.provider.name`` and ``gen_ai.request.model``
            semantic-convention attributes can be set on the span and
            later read back at end-time to key the GenAI metrics.
        :param operation: GenAI operation name attached to the span as
            ``gen_ai.operation.name`` (default ``"chat"``).
        """
        mlflow = _mlflow()
        attributes: dict[str, str] = {}
        if model:
            attributes["model"] = model
            from omnigent.runtime.telemetry import parse_provider_name

            provider, request_model = parse_provider_name(model)
            if request_model:
                attributes["gen_ai.request.model"] = request_model
            if provider:
                attributes["gen_ai.provider.name"] = provider
        attributes["gen_ai.operation.name"] = operation
        return cast(
            "LiveSpan",
            mlflow.start_span_no_context(
                name="llm_call",
                span_type="CHAT_MODEL",
                parent_span=self._current_span,
                inputs={"messages": _truncate_messages(messages)},
                attributes=attributes,
            ),
        )

    def end_llm_span(
        self,
        span: LiveSpan | None,
        response_text: str | None = None,
        status: str = "OK",
        error: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """End a CHAT_MODEL span and emit the GenAI metric instruments.

        :param span: Span handle returned by ``start_llm_span``, or
            ``None`` when tracing was disabled at start time.
        :param response_text: Assistant text attached to the span's
            outputs.
        :param status: MLflow span status, e.g. ``"OK"`` / ``"ERROR"``.
        :param error: Optional error message attached as
            ``error.message``; also forces the status to ``"ERROR"``.
        :param usage: Token usage dict, used to record the
            ``gen_ai.client.token.usage`` histogram in addition to the
            on-span attribute set elsewhere via
            ``telemetry.record_llm_usage``.
        """
        if span is None:
            return
        # Pass the optional through to MLflow directly; ``None`` is a
        # valid output value that renders as an explicitly-absent
        # response in the trace UI, whereas ``""`` looks like the
        # model returned an empty string.
        span.set_outputs({"response": response_text})
        if error:
            span.set_attribute("error.message", error)
            span.set_status("ERROR")
        else:
            span.set_status(status)
        span.end()
        self._emit_llm_metrics(span, usage)

    def _emit_llm_metrics(
        self,
        span: LiveSpan,
        usage: dict[str, Any] | None,
    ) -> None:
        """Emit ``gen_ai.client.*`` metric data points for one LLM span.

        Reads ``gen_ai.provider.name`` / ``gen_ai.request.model`` /
        ``gen_ai.operation.name`` back off the span so the metric
        emission stays in sync with the on-span semantic-convention
        attributes set by ``start_llm_span``.

        :param span: The ended LLM span.
        :param usage: Optional token usage dict; when present, recorded
            on the token-usage histogram.
        """
        try:
            from omnigent.runtime.telemetry import (
                record_operation_duration_metric,
                record_token_usage_metric,
            )

            provider = _read_str_attr(span, "gen_ai.provider.name")
            request_model = _read_str_attr(span, "gen_ai.request.model")
            operation = _read_str_attr(span, "gen_ai.operation.name") or "chat"

            start_ns = span.start_time_ns or 0
            end_ns = span.end_time_ns or 0
            if start_ns and end_ns and end_ns >= start_ns:
                duration_s = (end_ns - start_ns) / 1e9
                record_operation_duration_metric(
                    duration_s,
                    provider=provider or "",
                    model=request_model or "",
                    operation=operation,
                )

            if usage:
                record_token_usage_metric(
                    usage,
                    provider=provider or "",
                    model=request_model or "",
                )
        except Exception:  # noqa: BLE001 — telemetry must never break the request path
            logger.debug("failed to emit LLM metrics", exc_info=True)

    def start_tool_span(
        self,
        tool_name: str,
        tool_args: dict[str, TraceValue],
    ) -> LiveSpan:
        """Begin a TOOL span."""
        mlflow = _mlflow()
        span = cast(
            "LiveSpan",
            mlflow.start_span_no_context(
                name=f"tool:{tool_name}",
                span_type="TOOL",
                parent_span=self._current_span,
                inputs={"tool": tool_name, "args": _safe_serialize(tool_args)},
            ),
        )
        self._current_span = span
        return span

    def end_tool_span(
        self,
        span: LiveSpan | None,
        result: TraceValue = None,
        status: str = "OK",
        error: str | None = None,
        duration_ms: float = 0.0,
        parent_span: LiveSpan | None = None,
    ) -> None:
        if span is None:
            return
        span.set_outputs({"result": _safe_serialize(result)})
        if duration_ms:
            span.set_attribute("duration_ms", duration_ms)
        if error:
            span.set_attribute("error.message", error)
            span.set_status("ERROR")
        else:
            span.set_status(status)
        span.end()
        # Restore parent as current.
        if span is self._current_span:
            self._current_span = parent_span
        # Emit the tool duration histogram from the recorded ms value.
        # ``span.name`` is the ``tool:<name>`` form set in ``start_tool_span``;
        # strip the prefix so the metric attribute holds the plain tool name.
        if duration_ms:
            try:
                from omnigent.runtime.telemetry import record_tool_duration_metric

                tool_name = span.name or ""
                if tool_name.startswith("tool:"):
                    tool_name = tool_name[len("tool:") :]
                record_tool_duration_metric(duration_ms / 1000.0, tool_name)
            except Exception:  # noqa: BLE001 — telemetry must never break the request path
                logger.debug("failed to emit tool duration metric", exc_info=True)

    def start_policy_span(
        self,
        policy_name: str,
        phase: str,
        content: TraceValue = None,
    ) -> LiveSpan:
        """Begin a GUARDRAIL span for a policy evaluation."""
        mlflow = _mlflow()
        return cast(
            "LiveSpan",
            mlflow.start_span_no_context(
                name=f"policy:{policy_name}",
                span_type="GUARDRAIL",
                parent_span=self._current_span,
                inputs={
                    "policy": policy_name,
                    "phase": phase,
                    "content": _safe_serialize(content),
                },
            ),
        )

    def end_policy_span(
        self,
        span: LiveSpan | None,
        action: str = "allow",
        reason: str | None = None,
    ) -> None:
        if span is None:
            return
        # ``reason`` is optional; pass ``None`` through to MLflow so
        # the trace UI distinguishes "no reason given" from "empty
        # reason string" rather than collapsing both to ``""``.
        span.set_outputs({"action": action, "reason": reason})
        if action == "deny":
            span.set_status("ERROR")
        else:
            span.set_status("OK")
        span.end()

    def create_child_context(self) -> TracingContext:
        """Create a child TracingContext for a sub-agent, parented to the
        current span of this context."""
        child = TracingContext()
        child.enabled = self.enabled
        child._current_span = self._current_span
        child._inherited_parent = self._current_span
        return child


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _safe_serialize(value: TraceValue, max_len: int = 4000) -> TraceValue:
    """Make a value JSON-safe for MLflow span attributes/inputs/outputs."""
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
        import json

        s = json.dumps(value, default=str)
        if len(s) > max_len:
            return s[:max_len] + "...(truncated)"
        return s
    except Exception:  # noqa: BLE001 — best-effort serialization for trace spans
        s = str(value)
        if len(s) > max_len:
            return s[:max_len] + "...(truncated)"
        return s


def _read_str_attr(span: LiveSpan, key: str) -> str | None:
    """Read a string attribute off a span, tolerating JSON-wrapped values.

    MLflow stores span attribute values via ``json.dumps``. Strings come
    back wrapped in quotes when read via ``span.get_attribute`` on the
    raw OTel attributes mapping; this helper unwraps them so callers can
    treat the result as a plain string.

    :param span: LiveSpan to read from.
    :param key: Attribute key.
    :returns: The unwrapped string value, or ``None`` when the attribute
        is unset or the value is not a string.
    """
    raw = span.get_attribute(key)
    if raw is None:
        return None
    if isinstance(raw, str):
        if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
            try:
                import json

                decoded = json.loads(raw)
                if isinstance(decoded, str):
                    return decoded
            except (json.JSONDecodeError, ValueError):
                return raw
        return raw
    return None


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
