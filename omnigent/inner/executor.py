"""Executor adapter interface for Omnigent.

An Executor translates between the framework's abstract message/tool model
and a concrete LLM or agent harness backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import json
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeAlias, runtime_checkable

# ---------------------------------------------------------------------------
# Type aliases for JSON-shaped executor boundaries
# ---------------------------------------------------------------------------

# Executor-facing conversation message: ``{"role", "content", "metadata",
# "session_id"?, ...}``. The canonical home for the shape that peer executor
# modules currently duplicate file-locally. Heterogeneous JSON keyed by
# string; consumers isinstance-narrow per ``role``.
Message: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# Omnigent tool schema passed to the LLM:
# ``{"name", "description", "parameters" (JSON-Schema)}``.
ToolSpec: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# ``ToolCallRequest.args`` / ``ToolCallComplete.result`` / ``ExecutorConfig
# .extra`` carry arbitrary JSON through from provider SDKs. The inner values
# are opaque at this layer.
ToolArgs: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
ToolResult: TypeAlias = Any  # type: ignore[explicit-any]
ToolCallMetadata: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
ExecutorExtra: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# ``enqueue_session_message`` content — arbitrary user-supplied payload
# (string text or a structured JSON value).
EnqueuedContent: TypeAlias = Any  # type: ignore[explicit-any]


# ``iterate_blocking_stream`` adapts provider-SDK iterators whose item types
# come from third-party libraries (openai, anthropic). We keep the signature
# provider-opaque via this TypeAlias — callers narrow with their own ``cast``
# or per-event isinstance checks, matching the existing peer-file pattern.
ProviderStreamItem: TypeAlias = Any  # type: ignore[explicit-any]


@runtime_checkable
class _ClosableIterator(Protocol):
    """Iterator that may optionally expose a ``close`` method.

    Provider SDK streams (OpenAI ``Stream``, Anthropic event streams, etc.)
    implement the iterator protocol and usually expose ``close`` for early
    termination. Declaring it as a Protocol lets ``iterate_blocking_stream``
    call ``close`` without a ``getattr(..., "close", ...)`` detour.
    """

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ExecutorConfig:
    """Per-turn configuration handed to an :class:`Executor`.

    :param model: The backend-specific model identifier (e.g.
        ``"databricks-claude-sonnet-4"`` or ``"gpt-5.3-codex"``), or
        ``None`` when no model has been pinned by the agent spec — each
        executor picks its own default in that case.
    :param temperature: Sampling temperature forwarded to the LLM.
    :param max_tokens: Upper bound on generated tokens for a single
        model response.
    :param extra: Arbitrary executor-specific kwargs merged into the
        underlying SDK call, e.g. ``{"stepwise_internal_turns": True}``.
    """

    model: str | None = None
    temperature: float = 0.0
    max_tokens: int = 100000
    extra: ExecutorExtra = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Events yielded by an executor during a turn
# ---------------------------------------------------------------------------


@dataclass
class ExecutorEvent:
    """Base class for events from an executor."""


@dataclass
class TextChunk(ExecutorEvent):
    """Streaming text output.

    :param text: The incremental assistant text delta emitted by the
        executor. Always a real string — empty strings are never yielded
        so downstream renderers don't waste frames on no-ops.
    """

    text: str


@dataclass
class ReasoningChunk(ExecutorEvent):
    """Streaming reasoning / chain-of-thought output.

    The workflow's ``_event_to_sse_dict`` maps this onto the
    ``response.reasoning_text.delta`` / ``response.reasoning.started`` SSE
    events.

    :param delta: The incremental reasoning text. Empty string for a
        ``"reasoning_started"`` marker emitted when a reasoning block
        opens but its content is encrypted/redacted (so no further
        deltas follow).
    :param event_type: One of ``"reasoning_text"``,
        ``"reasoning_summary"``, or ``"reasoning_started"``.
    """

    delta: str
    event_type: str


@dataclass
class ToolCallRequest(ExecutorEvent):
    """The LLM wants to call a tool.

    :param name: The tool's registered name, e.g. ``"sql_query"``.
    :param args: JSON-shaped arguments the LLM supplied for the call,
        e.g. ``{"query": "SELECT 1"}``.
    :param metadata: Executor-supplied per-call metadata (call id,
        provider-native fields, etc.). Opaque to the Session layer.
    """

    name: str
    args: ToolArgs = field(default_factory=dict)
    metadata: ToolCallMetadata = field(default_factory=dict)


@dataclass
class TurnComplete(ExecutorEvent):
    """The LLM has finished its turn with a final text response.

    :param response: The final assistant text for the turn, or ``None``
        when the turn ended without producing text (e.g.
        ``continue_turn=True`` continuation signals, or harness runs
        that hit an internal turn cap without yielding text). ``None``
        is distinct from an explicit empty string, which means "the
        model produced no text on purpose."
    :param modified_by_policy: True when output policy evaluation
        rewrote or blocked the response.
    :param continue_turn: True when the executor is signalling that it
        has more work to do in another internal turn and the Session
        should loop back without emitting an assistant message yet.
    :param usage: Provider-reported token usage for this turn, or
        ``None`` when the executor does not report usage. Known keys:
        ``"input_tokens"``, ``"output_tokens"``, ``"total_tokens"``,
        ``"cache_read_input_tokens"``, ``"cache_creation_input_tokens"``.
        e.g. ``{"input_tokens": 1523, "output_tokens": 847,
        "total_tokens": 2370}``.
    """

    response: str | None = None
    modified_by_policy: bool = False
    continue_turn: bool = False
    usage: dict[str, Any] | None = None


class ToolCallStatus(str, enum.Enum):
    SUCCESS = "success"
    ERROR = "error"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass
class ToolCallComplete(ExecutorEvent):
    """A tool call has finished executing (emitted by Session, not Executor).

    :param name: The tool's registered name, e.g. ``"sql_query"``.
    :param status: Outcome of the call — SUCCESS / ERROR / BLOCKED /
        CANCELLED.
    :param result: Raw tool return value (JSON-serialisable or an SDK
        payload). Preserved untouched for downstream consumers.
    :param error: Human-readable error message when ``status`` is not
        SUCCESS, otherwise ``None``. ``None`` (not ``""``) indicates
        "no error to report"; callers that render errors already
        branch on status before reading this field.
    :param duration_ms: Wall-clock tool execution time in milliseconds.
    :param metadata: Per-call metadata mirroring
        :attr:`ToolCallRequest.metadata` — primarily the
        provider-native ``call_id`` so downstream consumers can
        pair this completion with its originating request.
    """

    name: str
    status: ToolCallStatus = ToolCallStatus.SUCCESS
    result: ToolResult = None
    error: str | None = None
    duration_ms: float = 0.0
    metadata: ToolCallMetadata = field(default_factory=dict)


@dataclass
class TurnCancelled(ExecutorEvent):
    """The current assistant turn was cancelled before completion."""

    reason: str = "user_cancelled"
    phase: str = "model"


@dataclass
class ExecutorError(ExecutorEvent):
    """Something went wrong.

    :param message: Human-readable description of the failure, always
        populated by the emitting executor.
    :param retryable: ``True`` when the failure represents a transient
        turn-level error the provider/harness might succeed on retry,
        e.g. a codex app-server ``turn/failed`` or ``method == "error"``
        carrying a tool exit code. ``False`` (default) for harness-level
        failures (auth, SDK crash, protocol violation) that would recur.
        Consumed by the omnigent workflow to pick between
        :class:`RetryableLLMError` and :class:`PermanentLLMError`.
    """

    message: str
    retryable: bool = False


def _close_stream_quietly(stream: Iterator[ProviderStreamItem]) -> None:
    """Close ``stream`` if it exposes a ``close`` method, swallowing errors.

    Direct attribute access via the ``_ClosableIterator`` Protocol narrowing
    keeps mypy's attr-defined check honest (no ``getattr`` detour).
    """
    if isinstance(stream, _ClosableIterator):
        with contextlib.suppress(Exception):
            stream.close()


# Queue signals produced by the background worker in ``iterate_blocking_stream``.
# Modelled as typed dataclass variants so mypy can exhaustively narrow without
# a stringly-typed ``("kind", payload)`` tuple.
@dataclass(frozen=True)
class _StreamItem:
    payload: ProviderStreamItem


@dataclass(frozen=True)
class _StreamError:
    exc: BaseException


@dataclass(frozen=True)
class _StreamDone:
    pass


async def iterate_blocking_stream(
    stream: Iterator[ProviderStreamItem],
) -> AsyncIterator[ProviderStreamItem]:
    """Bridge a blocking Python iterator into the async world.

    Some provider SDKs expose synchronous streaming iterators. Iterating them
    directly inside ``async def run_turn()`` blocks the event loop and makes
    interactive UIs feel frozen. This helper consumes such iterators on a
    dedicated background thread and forwards items through a thread-safe
    queue without depending on the loop's default executor.
    """
    import queue as sync_queue

    signal_queue: sync_queue.Queue[_StreamItem | _StreamError | _StreamDone] = sync_queue.Queue()
    stop = threading.Event()

    def _worker() -> None:
        try:
            for item in stream:
                if stop.is_set():
                    break
                signal_queue.put(_StreamItem(payload=item))
        except Exception as exc:  # noqa: BLE001 — stream worker forwards any exception to the caller thread
            signal_queue.put(_StreamError(exc=exc))
        finally:
            _close_stream_quietly(stream)
            signal_queue.put(_StreamDone())

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    try:
        while True:
            try:
                signal = signal_queue.get_nowait()
            except sync_queue.Empty:
                await asyncio.sleep(0.001)
                continue
            if isinstance(signal, _StreamItem):
                yield signal.payload
                continue
            if isinstance(signal, _StreamError):
                raise signal.exc
            return
    finally:
        stop.set()
        _close_stream_quietly(stream)
        thread.join(timeout=0.5)


@dataclass(frozen=True)
class MessageSplit:
    """Result of separating a message list into its persisted and transient halves.

    :param persisted: The leading messages that belong to the session's
        durable history. Executors that advance a history cursor count
        only these.
    :param transient: The trailing framework-injected messages (e.g. the
        unread-inbox notice) that are delivered to the executor for this
        turn but are NOT stored in the session's persistent history.
    """

    persisted: list[Message]
    transient: list[Message]


def split_transient_tail(
    messages: list[Message],
) -> MessageSplit:
    """Split off trailing transient framework messages from persisted history.

    Some framework-injected messages (e.g. the unread-inbox notice produced by
    ``Session._framework_notice_message``) are appended to the end of the
    message list passed to executors but are NOT stored in the session's
    persistent history. Executors that do incremental delta tracking via a
    history cursor must treat these transient items separately, otherwise the
    cursor lands past the transient item and skips it on the next turn.

    A message is considered transient if its ``metadata`` dict has a truthy
    ``"framework"`` key. Transient items are only recognized at the trailing
    end of the list.

    :param messages: The full list of messages handed to an executor for a
        turn, e.g. ``[{"role": "user", ...}, {"role": "assistant", ...}]``
        optionally followed by framework notice messages.
    :returns: A :class:`MessageSplit` whose ``persisted`` field holds the
        durable-history prefix and whose ``transient`` field holds the
        trailing framework messages (possibly empty).
    """
    split_idx = len(messages)
    while split_idx > 0:
        meta = messages[split_idx - 1].get("metadata", {})
        if isinstance(meta, dict) and meta.get("framework"):
            split_idx -= 1
            continue
        break
    return MessageSplit(
        persisted=messages[:split_idx],
        transient=messages[split_idx:],
    )


@dataclass(frozen=True)
class ToolResultClassification:
    """Outcome of inspecting a tool result payload for UI/event consumption.

    :param status: The :class:`ToolCallStatus` that best describes the
        result (``SUCCESS``, ``ERROR``, ``BLOCKED``, or ``CANCELLED``).
    :param error: A human-readable error message, or the empty string when
        the result is not an error. Preserved as ``""`` (not ``None``) so
        callers can forward it directly into the ``error`` field of
        :class:`ToolCallComplete`, which is also ``str``.
    """

    status: ToolCallStatus
    error: str


def classify_tool_result(
    result: ToolResult,
    *,
    fallback_to_string: bool = False,
) -> ToolResultClassification:
    """Classify a tool result for UI/event consumption.

    :param result: The raw tool result payload to inspect. May be ``None``,
        a primitive, a dict (the common shape, with optional ``error`` /
        ``blocked`` / ``cancelled`` / ``content`` / ``result`` / ``output``
        / ``text`` keys), a list (recursed element-wise), a string
        (optionally JSON), or an SDK object exposing ``model_dump`` /
        ``__dict__``.
    :param fallback_to_string: When True, treat opaque payloads (non-JSON
        strings, objects with no recognizable error shape) as errors and
        surface their string form. Used when the caller already knows the
        tool failed (e.g. Claude SDK's ``is_error`` flag) but the payload
        doesn't self-describe as an error.
    :returns: A :class:`ToolResultClassification` whose ``status`` is the
        inferred :class:`ToolCallStatus` and whose ``error`` is the
        extracted message (``""`` when no error was found).
    """
    if result is None:
        return ToolResultClassification(status=ToolCallStatus.SUCCESS, error="")

    if isinstance(result, dict):
        if result.get("cancelled"):
            return ToolResultClassification(
                status=ToolCallStatus.CANCELLED,
                error=str(result.get("reason", "cancelled")),
            )
        if result.get("error"):
            return ToolResultClassification(
                status=ToolCallStatus.ERROR,
                error=str(result["error"]),
            )
        if result.get("blocked"):
            return ToolResultClassification(
                status=ToolCallStatus.BLOCKED,
                error=str(result.get("reason", "BLOCKED")),
            )
        for key in ("content", "result", "output", "text"):
            if key in result:
                nested = classify_tool_result(
                    result[key],
                    fallback_to_string=fallback_to_string,
                )
                if nested.error:
                    return nested
        return ToolResultClassification(status=ToolCallStatus.SUCCESS, error="")

    if isinstance(result, list):
        for item in result:
            nested = classify_tool_result(
                item,
                fallback_to_string=fallback_to_string,
            )
            if nested.error:
                return nested
        return ToolResultClassification(status=ToolCallStatus.SUCCESS, error="")

    if isinstance(result, str):
        stripped = result.strip()
        if not stripped:
            return ToolResultClassification(status=ToolCallStatus.SUCCESS, error="")
        try:
            parsed = json.loads(stripped)
        except (TypeError, json.JSONDecodeError):
            if fallback_to_string:
                return ToolResultClassification(
                    status=ToolCallStatus.ERROR,
                    error=stripped,
                )
            return ToolResultClassification(status=ToolCallStatus.SUCCESS, error="")
        nested = classify_tool_result(
            parsed,
            fallback_to_string=fallback_to_string,
        )
        if nested.error:
            return nested
        if fallback_to_string:
            return ToolResultClassification(
                status=ToolCallStatus.ERROR,
                error=stripped,
            )
        return ToolResultClassification(status=ToolCallStatus.SUCCESS, error="")

    if hasattr(result, "model_dump"):
        return classify_tool_result(
            result.model_dump(by_alias=True, exclude_none=True),
            fallback_to_string=fallback_to_string,
        )

    if hasattr(result, "__dict__"):
        return classify_tool_result(
            vars(result),
            fallback_to_string=fallback_to_string,
        )

    if fallback_to_string:
        return ToolResultClassification(status=ToolCallStatus.ERROR, error=str(result))
    return ToolResultClassification(status=ToolCallStatus.SUCCESS, error="")


# ---------------------------------------------------------------------------
# Abstract executor
# ---------------------------------------------------------------------------


class Executor:
    """Abstract interface for LLM backends and agent harnesses.

    Subclass this and implement ``run_turn`` for each backend.
    """

    async def run_turn(
        self,
        messages: list[Message],  # noqa: ARG002 — abstract method signature; subclasses implement
        tools: list[ToolSpec],  # noqa: ARG002 — abstract method signature; subclasses implement
        system_prompt: str,  # noqa: ARG002 — abstract method signature; subclasses implement
        config: ExecutorConfig | None = None,  # noqa: ARG002 — abstract method signature; subclasses implement
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Run one turn of the agent loop.

        Yields ExecutorEvent instances (TextChunk, ToolCallRequest, TurnComplete,
        or ExecutorError).
        """
        raise NotImplementedError
        # Make this an async generator
        yield  # pragma: no cover

    def supports_streaming(self) -> bool:
        return False

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        """Whether this executor executes tools inside its own agent loop.

        When True, the Session should NOT re-execute tools on ToolCallRequest.
        Instead it should pass through ToolCallRequest/ToolCallComplete from
        the executor as-is (they are informational events from the internal
        tool-call loop).
        """
        return False

    def max_context_tokens(self) -> int | None:
        return None

    async def close_session(self, session_key: str) -> None:  # noqa: ARG002 — default no-op; subclasses with per-session state override
        """
        Release resources associated with one Omnigent session.

        Executors that keep per-session state (for example persistent agent
        harness subprocesses or SDK clients) should override this.
        """
        return

    async def interrupt_session(self, session_key: str) -> bool:  # noqa: ARG002 — default no-op; subclasses override to support interruption
        """Ask the executor to interrupt a currently running turn, if supported."""
        return False

    async def compact_session(self, session_key: str) -> dict[str, Any]:  # noqa: ARG002 — default no-op; subclasses override to support in-place compaction
        """
        Compact a live session's context in place, if supported.

        Executors backed by a harness that owns its own context (e.g. the
        Claude Agent SDK) should override this to trigger the harness's own
        compaction — the runner's transcript-side compaction cannot shrink
        that context. The default reports the capability is absent so callers
        fall back to runner/server-side compaction.

        :param session_key: The session to compact.
        :returns: A result dict with ``status`` — ``"unsupported"`` by
            default; overrides return ``"success"`` (with ``pre_tokens`` /
            ``post_tokens`` / ``trigger``), ``"no_session"``, or ``"error"``.
        """
        return {"status": "unsupported"}

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:  # noqa: ARG002 — default no-op; subclasses override to support live queueing
        """Send a new user message to a live session without interrupting it, if supported."""
        return False

    def supports_live_message_queue(self) -> bool:
        """Whether ``enqueue_session_message()`` is expected to work during a running turn."""
        return False

    def supports_tool_boundary_interrupt(self) -> bool:
        """Whether queued user input can be applied by interrupting after a tool boundary."""
        return False

    def supports_stepwise_internal_turns(self) -> bool:
        """Whether the executor can pause and resume its own agent loop between turns."""
        return False

    async def close(self) -> None:
        """Release executor-wide resources.

        Session objects call ``close_session()``. Test fixtures or embedding
        applications can call ``close()`` when they are done with the executor
        itself.
        """
        return


# ---------------------------------------------------------------------------
# Mock executor for testing
# ---------------------------------------------------------------------------


class MockExecutor(Executor):
    """A mock executor that returns scripted responses.

    Usage::

        executor = MockExecutor()
        executor.enqueue_response("Hello!")
        executor.enqueue_tool_call("sql_query", {"query": "SELECT 1"})
        executor.enqueue_response("Done.")
    """

    def __init__(self) -> None:
        self._turns: list[list[ExecutorEvent]] = []

    def enqueue_response(self, text: str) -> None:
        """Add a simple text response turn."""
        self._turns.append([TurnComplete(response=text)])

    def enqueue_tool_call(
        self,
        tool_name: str,
        args: ToolArgs | None = None,
        follow_up_response: str | None = None,
    ) -> None:
        """Add a turn that calls a tool then gives a final response.

        :param tool_name: The tool the scripted LLM should invoke,
            e.g. ``"sql_query"``.
        :param args: Arguments the scripted LLM passes to the tool,
            e.g. ``{"query": "SELECT 1"}``. ``None`` is treated as
            ``{}``.
        :param follow_up_response: Optional assistant text to enqueue
            as the NEXT turn after the tool result comes back, e.g.
            ``"Found 3 rows."``. ``None`` (the default) means "don't
            schedule a follow-up turn" — the caller can enqueue
            additional turns explicitly.
        """
        events: list[ExecutorEvent] = [
            ToolCallRequest(name=tool_name, args=args or {}),
        ]
        # After the tool result is fed back, the executor will be called again;
        # we enqueue the follow-up as a separate turn.
        self._turns.append(events)
        if follow_up_response is not None:
            self._turns.append([TurnComplete(response=follow_up_response)])

    def enqueue_events(self, events: list[ExecutorEvent]) -> None:
        """Add a raw list of events as one turn."""
        self._turns.append(events)

    async def run_turn(
        self,
        messages: list[Message],  # noqa: ARG002 — MockExecutor ignores input; replays scripted turns
        tools: list[ToolSpec],  # noqa: ARG002 — MockExecutor ignores input; replays scripted turns
        system_prompt: str,  # noqa: ARG002 — MockExecutor ignores input; replays scripted turns
        config: ExecutorConfig | None = None,  # noqa: ARG002 — MockExecutor ignores input; replays scripted turns
    ) -> AsyncIterator[ExecutorEvent]:
        if not self._turns:
            yield TurnComplete(response="[MockExecutor: no more scripted turns]")
            return
        events = self._turns.pop(0)
        for event in events:
            yield event
