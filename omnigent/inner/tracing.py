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

# Imported lazily inside methods to avoid a hard runtime.telemetry
# import at module load time. Helpers used: ``parse_provider_name``
# (model-string → provider/model split) and ``should_capture_content``
# (content gating via ``OMNIGENT_OTEL_CAPTURE_CONTENT``).

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
        from omnigent.runtime.telemetry import parse_provider_name, should_capture_content

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
        # Existing omnigent attributes plus the GenAI Agent Spans
        # semantic-convention attributes so non-MLflow OTel backends
        # render the span as an agent invocation. See
        # https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/
        attrs: dict[str, str] = {
            "agent.name": agent_name,
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": agent_name,
        }
        if model:
            attrs["model"] = model
            provider, model_name = parse_provider_name(model)
            if provider:
                attrs["gen_ai.provider.name"] = provider
            if model_name:
                attrs["gen_ai.request.model"] = model_name
        if parent_ended and parent is not None:
            attrs["parent_span_id"] = str(parent.span_id)
            parent = None
        # Gate the user message on the content-capture flag. Agent name
        # is metadata (always set on attributes), the user message is
        # content (may contain PII / secrets).
        inputs: dict[str, Any] = {}
        if should_capture_content():
            inputs["user_message"] = user_message
        span = cast(
            "LiveSpan",
            mlflow.start_span_no_context(
                name=f"agent:{agent_name}",
                span_type="AGENT",
                parent_span=parent,
                inputs=inputs,
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
        from omnigent.runtime.telemetry import should_capture_content

        if span is None:
            return
        if should_capture_content():
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
    ) -> LiveSpan:
        """Begin a CHAT_MODEL span for an executor.run_turn call."""
        from omnigent.runtime.telemetry import parse_provider_name, should_capture_content

        mlflow = _mlflow()
        # GenAI semantic-convention attributes for the chat span. See
        # https://opentelemetry.io/docs/specs/semconv/gen-ai/llm-spans/
        attributes: dict[str, str] = {
            "gen_ai.operation.name": "chat",
        }
        if model:
            attributes["model"] = model
            provider, model_name = parse_provider_name(model)
            if provider:
                attributes["gen_ai.provider.name"] = provider
            if model_name:
                attributes["gen_ai.request.model"] = model_name
        inputs: dict[str, Any] = {}
        if should_capture_content():
            inputs["messages"] = _truncate_messages(messages)
        return cast(
            "LiveSpan",
            mlflow.start_span_no_context(
                name="llm_call",
                span_type="CHAT_MODEL",
                parent_span=self._current_span,
                inputs=inputs,
                attributes=attributes,
            ),
        )

    def end_llm_span(
        self,
        span: LiveSpan | None,
        response_text: str | None = None,
        status: str = "OK",
        error: str | None = None,
    ) -> None:
        from omnigent.runtime.telemetry import should_capture_content

        if span is None:
            return
        # ``response_text`` is LLM-generated content. Gate on the
        # content-capture flag so a default install does not leak
        # responses into the trace UI. When capture is enabled, pass
        # the optional through to MLflow directly: ``None`` is a valid
        # output value that renders as an explicitly-absent response,
        # whereas ``""`` looks like the model returned an empty string.
        if should_capture_content():
            span.set_outputs({"response": response_text})
        if error:
            span.set_attribute("error.message", error)
            span.set_status("ERROR")
        else:
            span.set_status(status)
        span.end()

    def start_tool_span(
        self,
        tool_name: str,
        tool_args: dict[str, TraceValue],
    ) -> LiveSpan:
        """Begin a TOOL span."""
        from omnigent.runtime.telemetry import should_capture_content

        mlflow = _mlflow()
        # GenAI semantic-convention attributes for tool execution.
        # ``tool.name`` mirrors the OTel GenAI tool-spans convention.
        attributes: dict[str, str] = {
            "gen_ai.operation.name": "execute_tool",
            "tool.name": tool_name,
        }
        # ``tool`` (the name) is metadata, always included; ``args`` may
        # contain secrets or credentials so is gated on content capture.
        inputs: dict[str, Any] = {"tool": tool_name}
        if should_capture_content():
            inputs["args"] = _safe_serialize(tool_args)
        span = cast(
            "LiveSpan",
            mlflow.start_span_no_context(
                name=f"tool:{tool_name}",
                span_type="TOOL",
                parent_span=self._current_span,
                inputs=inputs,
                attributes=attributes,
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
        from omnigent.runtime.telemetry import should_capture_content

        if span is None:
            return
        # Tool results may contain credentials, file contents, or
        # other sensitive payloads. Gate on content capture.
        if should_capture_content():
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

    def start_policy_span(
        self,
        policy_name: str,
        phase: str,
        content: TraceValue = None,
    ) -> LiveSpan:
        """Begin a GUARDRAIL span for a policy evaluation."""
        from omnigent.runtime.telemetry import should_capture_content

        mlflow = _mlflow()
        # Policy name + phase are metadata. ``content`` is the actual
        # text being checked and may carry secrets; gate on the flag.
        inputs: dict[str, Any] = {"policy": policy_name, "phase": phase}
        if should_capture_content():
            inputs["content"] = _safe_serialize(content)
        return cast(
            "LiveSpan",
            mlflow.start_span_no_context(
                name=f"policy:{policy_name}",
                span_type="GUARDRAIL",
                parent_span=self._current_span,
                inputs=inputs,
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
