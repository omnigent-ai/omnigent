"""
Shared adapter that wraps any inner :class:`Executor` instance as
a :class:`HarnessApp` subclass.

The four per-harness wraps (claude-sdk, codex, pi,
openai-agents-sdk) all use this adapter — they only differ in the
inner ``Executor`` they construct. The adapter handles:

- Per-conversation lazy executor construction (Layer 1 state on
  the adapter instance).
- Per-turn translation of Omnigent :class:`CreateResponseRequest` →
  inner :class:`Message` list + :class:`ExecutorConfig`.
- Per-turn translation of inner :class:`ExecutorEvent`s →
  typed Omnigent SSE events emitted via :meth:`TurnContext.emit`.
- Forwarding ``request.tools`` and ``request.instructions`` to
  the inner Executor; wiring a ``_tool_executor`` callback so
  the inner SDK round-trips spec-declared tools through
  :meth:`TurnContext.dispatch_tool` (the scaffold's
  action_required path).
- Cancellation propagation (POST /cancel → ctx.cancelled →
  inner ``Executor.interrupt_session``).
- Per-conversation cleanup on shutdown.

V1 limitations (documented in §Autonomous decisions):

- **No per-conversation executor configuration via spec.** The
  per-harness wrap's executor factory determines configuration
  (cwd, model, sandbox, credentials) at construction time. A
  follow-up should thread spec config through the request body
  or via subprocess env vars.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import Response

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnCancelled,
    TurnComplete,
)
from omnigent.inner.tracing import TracingContext, is_tracing_enabled
from omnigent.runtime.harnesses._scaffold import HarnessApp, PolicyVerdictPayload, TurnContext
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server.schemas import (
    CreateResponseRequest,
    ElicitationRequestParams,
    InjectionConsumedEvent,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    ReasoningStartedEvent,
    ReasoningSummaryTextDeltaEvent,
    ReasoningTextDeltaEvent,
)

_logger = logging.getLogger(__name__)

# Status string the per-harness wraps emit on observed
# function_call items (i.e., tools the inner SDK already executed
# natively). Distinct from ``"action_required"`` which is what
# server-dispatched tool calls use — see §Sub-agent representation
# in designs/SERVER_HARNESS_CONTRACT.md.
_OBSERVED_TOOL_CALL_STATUS = "in_progress"


# Prefix the Claude SDK applies to MCP-registered tool names
# (e.g. ``mcp__omnigent__sys_terminal_launch``). Tools whose
# name starts with this prefix round-trip through the inner
# executor's ``_tool_executor`` callback -> :func:`_bridge_one_dispatch`
# -> ``ctx.dispatch_tool``, which emits an action_required
# function_call event POST-STREAM (the SDK's MCP-server handler
# fires after the assistant message finishes streaming).
#
# Adapter strategy for MCP tools:
#
# 1. Emit the observed function_call event INLINE (when the
#    inner SDK yields ToolCallRequest). That's what gives the
#    REPL the ``⏵ tool_name`` line interleaved with text rather
#    than bunched at the end of the turn.
# 2. Queue the SDK's ``tool_use_id`` so the post-stream
#    dispatch uses the SAME ``call_id`` (see
#    ``self._pending_mcp_call_ids``). Correlated ids let the
#    SDK client's ``BlockStream`` dedupe the action_required
#    event against the inline observed event — single render,
#    no duplicates.
# 3. Skip the observed function_call_output emission on
#    ToolCallComplete — the dispatch's PATCH handler emits the
#    paired output. Keeps the dedup story symmetric.
_MCP_TOOL_NAME_PREFIX = "mcp__"


def _finalize_trace_status(response_id: str) -> None:
    """PATCH the trace status to OK on the MLflow server.

    OTLP-ingested traces stay "In progress" because the server has
    no signal that all spans have arrived. This call explicitly
    marks the trace as complete after the OTel provider is flushed.
    """
    try:
        from omnigent.runtime.telemetry import trace_id_from_response_id

        trace_id = trace_id_from_response_id(response_id)
        request_id = f"tr-{trace_id}"

        tracking_uri = os.environ.get("MLFLOW_TRACKING_URI") or os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", ""
        )
        if not tracking_uri:
            return
        import httpx

        httpx.Client(timeout=5).patch(
            f"{tracking_uri.rstrip('/')}/api/2.0/mlflow/traces/{request_id}",
            json={"status": "OK"},
        ).close()
    except Exception:
        _logger.debug("failed to finalize trace status", exc_info=True)


def _strip_mcp_tool_prefix(name: str) -> str:
    """
    Strip the Claude SDK MCP tool prefix from a tool name.

    The Claude SDK names MCP tools as ``mcp__{server}__{tool}``
    (e.g. ``mcp__omnigent__sys_terminal_launch``). The bare
    name (``sys_terminal_launch``) is what the Omnigent wire shape
    and persisted conversation items carry — kept in sync with
    :func:`omnigent.runtime.workflow._observed_tool_call_sse_dicts`
    and :func:`_build_observed_tool_items` so the SSE name, the
    store-item name, and the adapter's emitted name all agree.

    Mirrors :func:`omnigent.runtime.workflow._strip_mcp_tool_prefix`
    — kept local here to avoid a cross-module import that
    would tighten the dependency between the harness adapter
    and the workflow package; both modules need this helper for
    different consumers.

    :param name: Tool name, possibly MCP-prefixed.
    :returns: The bare tool name. Only ``mcp__<server>__<tool>``
        shapes are stripped; bare names with ``__`` pass through.
    """
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


class ExecutorAdapter(HarnessApp):
    """
    :class:`HarnessApp` subclass that drives any inner
    :class:`Executor` instance.

    The per-harness wrap supplies an ``executor_factory`` —
    a zero-arg callable that constructs the inner executor. The
    adapter calls it lazily on the first turn (so heavyweight
    constructors like :class:`ClaudeSDKExecutor`'s eager
    Databricks credential resolution don't fire at FastAPI
    boot, only when a real conversation starts).

    The factory's return value is cached as Layer 1 state on the
    adapter instance — reused across turns for the conversation's
    lifetime. ``shutdown()`` calls ``executor.close()`` on
    teardown.

    :param executor_factory: Zero-arg callable returning a fresh
        :class:`Executor`. Tests pass a ``lambda:
        MockExecutor()``; production wraps pass a lambda that
        constructs the real per-harness Executor with config
        appropriate to the spec, e.g. ``lambda:
        ClaudeSDKExecutor(databricks=True, databricks_profile="<your-profile>")``.
    :param session_key: Stable identifier the inner executor uses
        to scope per-session state (clients, subprocesses). The
        adapter passes this to ``executor.close_session()`` and
        ``executor.interrupt_session()``. Defaults to a uuid hex
        — production wraps may want to set it from the
        ``conversation_id`` once the runner exposes it on
        ``app.state``.
    """

    def __init__(
        self,
        executor_factory: Callable[[], Executor],
        session_key: str | None = None,
    ) -> None:
        super().__init__()
        self._executor_factory = executor_factory
        self._session_key = session_key or uuid.uuid4().hex
        # Layer 1: lazily-constructed inner executor, reused
        # across turns for the conversation's lifetime.
        self._executor: Executor | None = None
        # Per-turn pointer to the active :class:`TurnContext`,
        # rebound at the top of each :meth:`run_turn` and cleared
        # in its finally. The stable ``_tool_executor`` bridge
        # (installed once on first use) reads from this slot so
        # the MCP handlers cached inside ``ClaudeSDKClient`` —
        # which closure-capture whatever ``_tool_executor`` was
        # set on the FIRST turn — always dispatch into the
        # CURRENT turn's ctx. Without this, turn N>1's tool calls
        # park a Future inside turn 1's already-dead ctx and the
        # SDK hangs forever waiting for a result that nobody can
        # deliver.
        self._current_ctx: TurnContext | None = None
        self._current_agent: str | None = None
        # FIFO queue of inner-SDK tool-use ids, one entry per
        # ToolCallRequest the executor parses. Populated by
        # :meth:`_translate_event` whenever ``event.metadata``
        # carries a ``call_id`` (the inner Claude SDK / OpenAI
        # Agents SDK / Codex / Pi executors all stamp it for
        # bridged tools). Drained by :meth:`_stable_tool_executor`
        # so each :func:`_bridge_one_dispatch` call reuses the
        # SAME ``call_id`` the observed function_call event
        # already carried — that's what lets the Omnigent REPL dedupe
        # the inline observed render with the post-stream
        # action_required render. Without this correlation, the
        # two events have different uuids, the SDK client sees
        # them as separate tool calls, and the REPL renders
        # ``⏵ tool_name`` twice plus an empty result panel for
        # the orphan call (the 2026-04-29 user-reported
        # duplicate-render + empty-box regression for
        # openai-agents — same shape as the 2026-04-28 claude-
        # sdk regression resolved for MCP-prefixed
        # tools). See commit 989bfde for the prior
        # suppress-observed mitigation that introduced the
        # end-of-turn ordering regression this queue resolves.
        self._pending_mcp_call_ids: deque[str] = deque()
        # Per-session tracing context. Created lazily on the first
        # turn when tracing is enabled; reused across turns so the
        # span parent chain stays rooted on the session's executor.
        self._tracing_ctx: TracingContext | None = None

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        """
        Drive the inner executor and translate its events.

        Lazily constructs the inner executor on the first call.
        Subsequent calls reuse the cached instance. Wires a
        per-turn ``_tool_executor`` callback so the inner SDK
        can round-trip spec-declared tools through Omnigent via the
        scaffold's ``dispatch_tool`` (action_required) path.
        The hook is reset to ``None`` after the turn so cross-
        turn references can't accidentally fire stale dispatch
        contexts.

        :param request: Decoded :class:`CreateResponseRequest`
            synthesized by the harness scaffold for this turn.
        :param ctx: Per-turn :class:`TurnContext` from the
            scaffold.
        """
        executor = self._ensure_executor()
        messages = _translate_input_to_messages(request.input)
        # Stamp every message with the adapter's session_key so
        # the inner :class:`ClaudeSDKExecutor` keys its cached
        # SDK client under THAT key. Without this stamp, the
        # executor's :meth:`_session_key` falls back to
        # ``"default"`` (no ``session_id`` on any message), and
        # subsequent :meth:`Executor.enqueue_session_message`
        # calls — which use ``self._session_key`` from this
        # adapter — return ``False`` because the cached client
        # is under a DIFFERENT key. Symptom (the user reported
        # this): mid-turn steering arrives in the harness, the
        # adapter forwards to enqueue_session_message, but the
        # SDK never sees the new prompt and Claude keeps going.
        for message in messages:
            message["session_id"] = self._session_key
        extra: dict[str, Any] = {}
        if request.reasoning is not None:
            effort = request.reasoning.get("effort")
            if effort:
                extra["reasoning_effort"] = effort
        if request.max_output_tokens is not None:
            extra["max_tokens"] = int(request.max_output_tokens)
        # request.model is the agent name; request.model_override carries
        # the per-request /model override. Threaded into cfg.model so the
        # inner executor's per-turn precedence picks it up over the spec
        # default (HARNESS_*_MODEL).
        config = ExecutorConfig(model=request.model_override, extra=extra)
        tools = _normalize_tool_schemas(request.tools or [])
        system_prompt = request.instructions or ""
        # Install the stable bridge ONCE on the executor. The SDK
        # caches its client on first turn and the MCP handlers
        # closure-capture ``self._tool_executor`` then. A fresh
        # bridge per turn would leave those handlers pointing at
        # a turn-1 closure forever — every later tool call would
        # dispatch into a dead ctx and the SDK would hang.
        # Rebind ``_current_ctx`` / ``_current_agent`` per turn
        # instead; the stable bridge reads from those slots at
        # call time. ``_tool_executor`` is harness-specific (not
        # declared on the inner :class:`Executor` ABC), so the
        # attribute set is best-effort — executors that don't
        # read it ignore the value, executors that DO read it
        # (Claude SDK) honor the round-trip protocol.
        if getattr(executor, "_tool_executor", None) is None:  # type: ignore[attr-defined]
            executor._tool_executor = self._stable_tool_executor  # type: ignore[attr-defined]
        # Install the elicitation handler once on first use, same
        # stable-reference pattern as ``_tool_executor``. The SDK's
        # ``can_use_tool`` callback is constructed per-turn inside
        # ClaudeSDKExecutor.run_turn() from this attribute, so the
        # closure always reads ``_current_ctx`` at call time.
        if getattr(executor, "_elicitation_handler", None) is None:  # type: ignore[attr-defined]
            executor._elicitation_handler = self._stable_elicitation_handler  # type: ignore[attr-defined]
        # Install the policy evaluator bridge once on first use, same
        # stable-reference pattern as ``_tool_executor``. The inner
        # executor calls this before/after each LLM call to evaluate
        # LLM_REQUEST / LLM_RESPONSE policies via a round-trip to the
        # Omnigent server (routed through the runner).
        if getattr(executor, "_policy_evaluator", None) is None:  # type: ignore[attr-defined]
            executor._policy_evaluator = self._stable_policy_evaluator  # type: ignore[attr-defined]
        self._current_ctx = ctx
        self._current_agent = request.model
        # Reset the MCP call-id queue at turn start. A prior turn
        # that errored mid-stream (e.g. cancelled while a tool_use
        # block had been parsed but its MCP-handler hadn't fired
        # yet) could leave entries behind; carrying them into the
        # next turn would mis-correlate the new turn's first
        # MCP dispatches with stale observed events from the
        # previous turn. Clearing makes each turn's correlation
        # window self-contained.
        self._pending_mcp_call_ids.clear()

        # --- Tracing setup ------------------------------------------------
        # Create a TracingContext per turn when tracing is enabled.
        # The trace_context_for_response wrapper derives the W3C
        # trace ID from the response_id so operators can look up
        # traces by response ID without a mapping table.
        tracing = is_tracing_enabled()
        if tracing and self._tracing_ctx is None:
            self._tracing_ctx = TracingContext()
        tctx = self._tracing_ctx if tracing else None
        agent_span = None
        # Active tool span for correlating ToolCallRequest → ToolCallComplete.
        _active_tool_span = None
        _active_tool_parent = None

        user_message = _extract_last_user_message(request.input)
        # --- End tracing setup --------------------------------------------

        # Watcher for mid-turn steering injections. The scaffold
        # routes incoming steering events with
        # ``previous_response_id == ctx.response_id`` onto
        # ``ctx.next_injection`` (see _push_injection in the
        # scaffold). The watcher converts each injection into an
        # :meth:`Executor.enqueue_session_message` call so the
        # inner SDK delivers the new user message into its
        # in-flight session — without this hook, AP-forwarded
        # steering would queue forever and the LLM would never
        # see it.
        injection_watcher = asyncio.create_task(
            self._watch_injections(ctx, executor),
            name=f"executor-adapter-injection-watch:{ctx.response_id}",
        )
        try:
            # Wrap the executor loop in the trace context so all
            # MLflow spans share the response-derived trace ID.
            # The context manager is built outside the `with` so we
            # can fall back to nullcontext if the response_id format
            # doesn't match (e.g. 24-char hex vs expected 32).
            trace_cm: contextlib.AbstractContextManager[None] = contextlib.nullcontext()
            if tctx:
                try:
                    from omnigent.runtime.telemetry import trace_context_for_response

                    trace_cm = trace_context_for_response(response_id=ctx.response_id)
                except Exception:
                    _logger.debug("trace_context_for_response unavailable", exc_info=True)
            with trace_cm:
                if tctx is not None:
                    agent_span = tctx.start_agent_span(
                        agent_name=request.model or "unknown",
                        user_message=user_message,
                        model=request.model_override or request.model,
                    )

                response_text: str | None = None
                async for event in executor.run_turn(
                    messages=messages,
                    tools=tools,
                    system_prompt=system_prompt,
                    config=config,
                ):
                    if ctx.cancelled.is_set():
                        if tctx is not None and agent_span is not None:
                            from omnigent.runtime.telemetry import record_cancellation

                            record_cancellation(agent_span)
                            tctx.end_agent_span(agent_span, response=None, status="ERROR")
                            agent_span = None
                        await executor.interrupt_session(self._session_key)
                        return
                    # --- Tracing: emit spans per event ---
                    if tctx is not None:
                        if isinstance(event, ToolCallRequest):
                            _active_tool_parent = tctx._current_span
                            _active_tool_span = tctx.start_tool_span(
                                _strip_mcp_tool_prefix(event.name),
                                event.args or {},
                            )
                        elif isinstance(event, ToolCallComplete):
                            if _active_tool_span is not None:
                                tctx.end_tool_span(
                                    _active_tool_span,
                                    result=event.result,
                                    status="ERROR" if event.error else "OK",
                                    error=event.error,
                                    duration_ms=event.duration_ms,
                                    parent_span=_active_tool_parent,
                                )
                                _active_tool_span = None
                                _active_tool_parent = None
                        elif isinstance(event, TurnComplete):
                            response_text = event.response
                            if event.usage is not None:
                                from omnigent.runtime.telemetry import record_llm_usage

                                # Record usage on the agent span for
                                # aggregate visibility.
                                record_llm_usage(agent_span, event.usage)
                    # --- End tracing ---
                    self._translate_event(event, ctx)
                    if isinstance(event, TurnComplete):
                        if tctx is not None and agent_span is not None:
                            tctx.end_agent_span(agent_span, response=response_text)
                            agent_span = None
                        return
                    if isinstance(event, TurnCancelled):
                        ctx.cancelled.set()
                        if tctx is not None and agent_span is not None:
                            from omnigent.runtime.telemetry import record_cancellation

                            record_cancellation(agent_span)
                            tctx.end_agent_span(agent_span, response=None, status="ERROR")
                        return
                    if isinstance(event, ExecutorError):
                        if tctx is not None and agent_span is not None:
                            tctx.end_agent_span(
                                agent_span,
                                response=None,
                                status="ERROR",
                                error=event.message,
                            )
                            agent_span = None
                        raise RuntimeError(f"inner executor error: {event.message}")
        except BaseException:
            # End agent span on unhandled exceptions so it's not
            # left open (which would leak on the OTel provider).
            if tctx is not None and agent_span is not None:
                tctx.end_agent_span(
                    agent_span, response=None, status="ERROR", error="unhandled exception"
                )
                agent_span = None
            raise
        finally:
            # Stop the injection watcher and let it drain so a
            # late ``next_injection`` doesn't fire after we've
            # moved on. ``injection_watcher`` is a long-poll
            # against an ``asyncio.Queue.get`` — cancelling is
            # the only way to break it.
            injection_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await injection_watcher
            # Flush the OTel provider and finalize the trace status
            # on the MLflow server. Without the flush, the
            # BatchSpanProcessor may not have exported the final
            # spans. Without the PATCH, the OTLP-ingested trace
            # stays "In progress" because the server has no
            # signal that all spans have arrived.
            if tctx is not None:
                try:
                    from opentelemetry import trace as otel_trace

                    provider = otel_trace.get_tracer_provider()
                    if hasattr(provider, "force_flush"):
                        provider.force_flush(timeout_millis=5000)
                except Exception:
                    pass
                _finalize_trace_status(ctx.response_id)
            # Clear the per-turn pointers so a stray late callback
            # (e.g. one fired after the SDK's stream closed) sees
            # ``None`` and returns an explicit error rather than
            # silently dispatching into the just-finished ctx.
            self._current_ctx = None
            self._current_agent = None

    async def _handle_interrupt_event(self) -> Response:
        """Cancel the turn AND drop the inner executor session.

        The base handler sets ``ctx.cancelled`` (terminal event becomes
        ``response.cancelled``) and clears the inject target. But the live
        client — dropping which is what actually stops the in-flight
        generation and forces the next turn to rebuild fresh — was only
        dropped by the run loop's between-events ``interrupt_session`` call,
        which is skipped when the turn is blocked awaiting the first token or
        torn down via HTTP disconnect. The next turn then reuses the client
        and flushes the abandoned generation: the post-cancel stream dump +
        the off-by-one. Drop it here, synchronously on the interrupt.

        :returns: The base handler's 204 response.
        :raises OmnigentError: 404 (from the base handler) when no turn is
            in flight.
        """
        response = await super()._handle_interrupt_event()
        # ``self._executor`` is set once a turn has run; an interrupt only
        # reaches here when one is in flight, so it is non-None in practice.
        # interrupt_session is best-effort and idempotent (a no-op if the run
        # loop already dropped the session), so call it bare like that path.
        if self._executor is not None:
            await self._executor.interrupt_session(self._session_key)
        return response

    async def _watch_injections(self, ctx: TurnContext, executor: Executor) -> None:
        """
        Loop forwarding ``ctx.next_injection`` to the inner SDK.

        Polls :meth:`TurnContext.next_injection` (a queue
        backed by the scaffold's ``_push_injection``) and
        translates each :class:`CreateResponseRequest` into an
        :meth:`Executor.enqueue_session_message` call so the
        inner SDK delivers the new user message into its
        in-flight session. Loops until cancelled by the
        :meth:`run_turn` finally block.

        Best-effort throughout — a failed injection logs and
        continues; a malformed request body (missing user
        text) is skipped; the watcher never raises out of the
        loop body. Errors that escape would teardown the watch
        task and surface as a confusing test/test-env failure
        in the parent run_turn.

        :param ctx: The active turn's context.
        :param executor: The inner executor to inject into.
            Must implement ``enqueue_session_message`` for the
            forwarding to actually deliver — executors that
            don't (legacy harnesses) silently no-op.
        """
        while True:
            try:
                injection = await ctx.next_injection(timeout=None)
            except asyncio.CancelledError:
                return
            if injection is None:
                # ``next_injection(None)`` blocks forever on the
                # queue, so a None return here means the queue
                # protocol changed or the scaffold pushed a
                # sentinel. Defensive — bail rather than spin.
                return
            text = _extract_user_text(injection.input)
            if not text:
                # Empty / malformed input — skip rather than
                # call the SDK with garbage.
                _logger.warning(
                    "skipping in-band injection with no text payload: %r",
                    injection.input,
                )
                continue
            if ctx.cancelled.is_set():
                # Turn interrupted — don't deliver into the dying session.
                return
            try:
                accepted = await executor.enqueue_session_message(self._session_key, text)
            except Exception:
                _logger.exception(
                    "inner executor.enqueue_session_message failed; in-band injection lost"
                )
                continue
            if not accepted:
                _logger.warning(
                    "inner executor refused in-band injection "
                    "(supports_live_message_queue=False?); LLM will "
                    "not see the steered message until the next turn"
                )
                continue
            # The executor consumed this injection into the running turn.
            # Echo the runner's correlation id back as an
            # ``injection.consumed`` marker so the runner drops the
            # buffered copy and does not re-deliver it as a continuation
            # turn (RUNNER_MESSAGE_INGEST.md Part B). Only meaningful when
            # the runner stamped an injection_id; fresh-turn injections and
            # legacy callers leave it unset.
            injection_id = getattr(injection, "injection_id", None)
            if injection_id:
                ctx.emit(
                    InjectionConsumedEvent(
                        type="injection.consumed",
                        injection_id=injection_id,
                    )
                )

    async def _stable_tool_executor(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Cached bridge the inner SDK keeps over the lifetime of
        the executor instance.

        Reads :attr:`_current_ctx` and :attr:`_current_agent` at
        call time so the dispatch always lands in the active
        turn's :class:`TurnContext`. If neither is set (a
        stale callback fired after the turn ended, or the SDK
        called the executor without a wrapping ``run_turn``),
        returns a clear error payload — letting the call park
        on a dead ctx would just hang the SDK indefinitely.

        For MCP-prefixed tools, drains the next ``tool_use_id``
        from :attr:`_pending_mcp_call_ids` and passes it to
        :func:`_bridge_one_dispatch` so the dispatch's
        action_required event shares a ``call_id`` with the
        inline observed event already emitted by
        :meth:`_translate_event`. The deque is FIFO and the
        inner SDK invokes MCP-server handlers in the same
        order it parsed the corresponding tool_use blocks, so
        positional pop is correct.

        :param tool_name: Tool name from the LLM's call. Carries
            the MCP prefix for SDK-registered tools (e.g.
            ``"mcp__omnigent__sys_terminal_launch"``).
        :param args: Decoded argument dict.
        :returns: A dict suitable as the MCP tool result.
        """
        ctx = self._current_ctx
        agent = self._current_agent
        if ctx is None or agent is None:
            _logger.warning(
                "tool callback fired with no active turn context (tool=%s); returning error",
                tool_name,
            )
            return {"error": "no active turn context for tool dispatch"}
        # Pop the matching ``tool_use_id`` for MCP tools so the
        # dispatch reuses the observed event's call_id. Non-MCP
        # tools and out-of-order edge cases (queue empty when an
        # MCP tool fires) fall through to a freshly-allocated id
        # in :func:`_bridge_one_dispatch` — that loses the dedup
        # but doesn't break the dispatch protocol.
        # ``_stable_tool_executor`` IS the SDK's MCP-server tool
        # callback — only invoked for MCP-routed tools. The
        # callback receives the BARE tool name (the MCP wrapper
        # strips the ``mcp__omnigent__`` prefix before
        # dispatching), so a ``startswith("mcp__")`` guard here
        # would always be False and the queue would never pop.
        # Pop whenever there's a queued id; non-MCP paths don't
        # populate the queue, so this can't accidentally drain
        # an unrelated id.
        correlated_call_id: str | None = None
        if self._pending_mcp_call_ids:
            correlated_call_id = self._pending_mcp_call_ids.popleft()
        return await _bridge_one_dispatch(
            ctx,
            agent,
            tool_name,
            args,
            call_id=correlated_call_id,
        )

    async def _stable_elicitation_handler(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """
        Cached bridge the inner SDK keeps over the executor's lifetime.

        Called by :class:`omnigent.inner.claude_sdk_executor.ClaudeSDKExecutor`
        via ``options.can_use_tool`` when the Claude CLI requests
        permission before executing a tool and ``permission_mode`` is not
        ``"bypassPermissions"``. Reads :attr:`_current_ctx` at call time
        so the elicitation always lands in the active turn's context —
        same rebind-per-turn pattern as :meth:`_stable_tool_executor`.

        If no turn context is active (callback fired after the turn ended
        or before a turn starts), returns ``False`` to deny by default
        rather than granting unreviewed permission.

        :param tool_name: Tool name Claude wants to call, e.g. ``"Bash"``.
        :param tool_input: Arguments dict for the tool call,
            e.g. ``{"command": "ls -la"}``.
        :returns: ``True`` when the user approves the tool call;
            ``False`` when they deny it.
        """
        ctx = self._current_ctx
        if ctx is None:
            # Elicitation callback fired with no active turn — this is a
            # code-level invariant violation (run_turn clears _current_ctx
            # in its finally block; a callback should never arrive after
            # that). Deny by default: fail safe rather than grant permission
            # that was never reviewed.
            _logger.error(
                "elicitation callback fired with no active turn context "
                "(tool=%s); denying by default",
                tool_name,
            )
            return False

        elicitation_id = f"elicit_{secrets.token_hex(16)}"
        # Build a concise preview: truncate long args so the UI widget
        # stays readable. 300 chars matches AP's policy-engine preview.
        try:
            preview = json.dumps(tool_input, ensure_ascii=False)
        except (TypeError, ValueError):
            preview = repr(tool_input)
        preview = preview[:300]

        params = ElicitationRequestParams(
            mode="form",
            message=f"Claude wants to call **{tool_name}**",
            requestedSchema=None,
            url=None,
            phase="tool_call",
            policy_name="claude_sdk_permission",
            content_preview=f"{tool_name}({preview})",
        )
        result = await ctx.elicit(elicitation_id, params)
        return result.action == "accept"

    async def _stable_policy_evaluator(
        self,
        phase: str,
        data: dict[str, Any],
    ) -> PolicyVerdictPayload:
        """
        Cached bridge the inner executor keeps over its lifetime.

        Called by the inner executor before (``PHASE_LLM_REQUEST``)
        and after (``PHASE_LLM_RESPONSE``) each LLM call. Routes
        the evaluation through the scaffold's
        :meth:`TurnContext.evaluate_policy` round-trip, which emits
        a ``policy_evaluation.requested`` SSE event and parks on a
        Future until the runner delivers the verdict.

        The round-trip has a timeout (see ``_POLICY_EVAL_TIMEOUT_S``
        in ``_scaffold.py``) so a stalled verdict defaults to ALLOW
        instead of hanging the executor.

        :param phase: Proto-style phase string, e.g.
            ``"PHASE_LLM_REQUEST"`` or ``"PHASE_LLM_RESPONSE"``.
        :param data: Event data dict for the policy engine.
        :returns: The policy verdict from the Omnigent server.
        """
        ctx = self._current_ctx
        if ctx is None:
            _logger.warning(
                "policy evaluator fired with no active turn context (phase=%s); "
                "returning ALLOW by default",
                phase,
            )
            return PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
        evaluation_id = f"poleval_{secrets.token_hex(16)}"
        return await ctx.evaluate_policy(evaluation_id, phase, data)

    def _ensure_executor(self) -> Executor:
        """
        Construct the inner executor on first use; return cached
        instance thereafter.

        :returns: The per-conversation inner :class:`Executor`.
        """
        if self._executor is None:
            self._executor = self._executor_factory()
        return self._executor

    def _translate_event(self, event: ExecutorEvent, ctx: TurnContext) -> None:
        """
        Translate one inner :class:`ExecutorEvent` into Omnigent SSE
        events emitted via ``ctx.emit``.

        Per the v1 limitations in the module docstring, all
        :class:`ToolCallRequest` / :class:`ToolCallComplete`
        events are treated as observed-only (the SDK already
        executed the tool natively) — they surface as paired
        function_call + function_call_output items with
        ``status: "completed"`` per §Sub-agent representation.

        :param event: The inner executor event to translate.
        :param ctx: The per-turn context; events are pushed onto
            its queue.
        """
        if isinstance(event, TextChunk):
            ctx.emit(
                OutputTextDeltaEvent(
                    type="response.output_text.delta",
                    delta=event.text,
                )
            )
        elif isinstance(event, ReasoningChunk):
            # Translate inner reasoning to the Omnigent wire shape so the
            # workflow sees the same SSE events whether the executor
            # runs inline or behind the harness scaffold.
            if event.event_type == "reasoning_started":
                ctx.emit(
                    ReasoningStartedEvent(type="response.reasoning.started"),
                )
            elif event.event_type == "reasoning_summary":
                ctx.emit(
                    ReasoningSummaryTextDeltaEvent(
                        type="response.reasoning_summary_text.delta",
                        delta=event.delta,
                    ),
                )
            else:
                # ``reasoning_text`` and any future flavor land here.
                ctx.emit(
                    ReasoningTextDeltaEvent(
                        type="response.reasoning_text.delta",
                        delta=event.delta,
                    ),
                )
        elif isinstance(event, ToolCallRequest):
            # Observed function_call item, emitted INLINE as the
            # inner SDK parses each tool_use block — that's what
            # gives the REPL a ``⏵ tool_name`` line interleaved
            # with assistant text rather than bunched at the end
            # of the response.
            #
            # For MCP-prefixed tool names we ALSO queue the
            # ``tool_use_id`` so the matching :func:`_bridge_one_dispatch`
            # call (fired post-stream when the SDK invokes the
            # MCP-server handler) uses the same call_id. With the
            # ids correlated, the SDK client's ``BlockStream``
            # dedupes the post-stream action_required event
            # against this observed event — keeping the inline
            # render and avoiding the 2026-04-28 duplicate-render
            # bug. Without the queue, the two events would have
            # different uuids and the REPL would render
            # ``⏵ tool_name`` twice (which is what 989bfde
            # mitigated by suppressing this branch entirely —
            # but at the cost of inline rendering, the regression
            # this fix reverses).
            #
            # The emitted ``name`` is the bare tool name. The
            # inner SDK passes the MCP-prefixed form
            # (``mcp__omnigent__sys_terminal_launch``) but the
            # Omnigent wire shape and persisted conversation items
            # carry the bare form — kept consistent with
            # ``omnigent/runtime/workflow.py``'s
            # ``_observed_tool_call_sse_dicts`` /
            # ``_build_observed_tool_items`` pair so SSE name and
            # store-item name don't drift.
            tool_use_id = _call_id_from_metadata(event.metadata)
            # Push the tool_use_id onto the correlation queue
            # whenever the executor stamped one. The original
            # gate restricted this to MCP-prefixed names (the
            # claude-sdk path), but every wrapped harness whose
            # tools round-trip through :meth:`_stable_tool_executor`
            # needs the same correlation — openai-agents
            # included. Without this, the dispatch path's
            # action_required emit gets a fresh uuid, the AP
            # client can't dedupe against the inline observed
            # event, and the REPL renders the call twice plus an
            # empty result panel for the orphan call.
            if tool_use_id is not None:
                self._pending_mcp_call_ids.append(tool_use_id)
            call_id = tool_use_id or f"call_{uuid.uuid4().hex[:12]}"
            bare_name = _strip_mcp_tool_prefix(event.name)
            ctx.emit(
                OutputItemDoneEvent(
                    type="response.output_item.done",
                    item={
                        "id": f"fc_{uuid.uuid4().hex[:12]}",
                        "type": "function_call",
                        "status": _OBSERVED_TOOL_CALL_STATUS,
                        "name": bare_name,
                        "arguments": _serialize_args(event.args),
                        "call_id": call_id,
                        "agent": ctx.response_id,
                    },
                )
            )
        elif isinstance(event, ToolCallComplete):
            # Paired function_call_output. The executor doesn't
            # always echo the call_id back on ToolCallComplete;
            # when it doesn't, consumers correlate by position
            # (the most recent function_call without an output).
            #
            # Every tool routed through _stable_tool_executor →
            # ctx.dispatch_tool already has its function_call_output
            # emitted by dispatch_tool when the Future resolves.
            # Emitting a second one here would duplicate it on the
            # SSE stream and produce ghost "Waiting for output"
            # cards in the Web UI. Suppress for all dispatched
            # tools — the scaffold is the single source of output
            # events for round-tripped calls.
            if self._current_ctx is not None:
                return
            call_id = _call_id_from_metadata(getattr(event, "metadata", None)) or ""
            item: dict[str, Any] = {
                "id": f"fco_{uuid.uuid4().hex[:12]}",
                "type": "function_call_output",
                "call_id": call_id,
                # Cap the mirror; the inner SDK already consumed the full result.
                "output": cap_tool_output(_serialize_tool_result(event)),
            }
            raw_args = getattr(event, "metadata", {}).get("arguments")
            if isinstance(raw_args, dict):
                item["arguments"] = raw_args
            ctx.emit(OutputItemDoneEvent(type="response.output_item.done", item=item))
        elif isinstance(event, TurnComplete):
            # If the executor produced a final assistant message
            # via TurnComplete.response (rather than through
            # streaming TextChunks), emit it now. Streamed
            # TextChunks have already gone out via the branch
            # above; in that case TurnComplete.response is
            # typically None or a duplicate that the inner
            # executor sets for non-streaming consumers.
            if event.response is not None:
                # Avoid double-emitting if the response text
                # matches the streamed deltas — but that
                # accumulator lives on Session today, not on the
                # adapter. For v1 we trust streaming-mode
                # executors to leave response=None, and only emit
                # on non-streaming paths.
                pass
            # Capture provider-reported usage so _build_terminal_event
            # can include it in the response.completed SSE payload.
            # The harness HTTP client reads response["usage"] from that
            # payload to populate TurnComplete.usage on the Omnigent side.
            if event.usage is not None:
                ctx.provider_usage = event.usage
        # ExecutorError handled by the caller (re-raises so the
        # scaffold can build a response.failed terminal event).

    def _build_error_detail(self, exception: BaseException) -> Any:
        """
        Map an inner-executor exception onto a contract-recognized
        error code.

        Default :meth:`HarnessApp._build_error_detail` uses
        ``type(exception).__name__`` as the code, which never
        matches AP's retryable allowlist
        (:data:`omnigent.runtime.harnesses._client_executor._RETRYABLE_HARNESS_ERROR_CODES`).
        Result before this override: a known transient failure
        like ``anthropic.RateLimitError`` would surface as
        ``code="RateLimitError"``, AP's allowlist wouldn't match,
        retry-classification would call it permanent, and the
        workflow would never retry. This method closes that gap
        for the harnesses the adapter wraps (claude-sdk, plus the
        codex / openai-agents-sdk / pi wraps once they land).

        Translation precedence:

        1. :class:`omnigent.errors.OmnigentError` (incl.
           :class:`RetryableLLMError` / :class:`PermanentLLMError`)
           — already carries a semantic ``code`` string per the
           project's own classification, so use it verbatim.
        2. OpenAI SDK exceptions — surfaced by the inner OpenAI
           Agents SDK / Open Responses SDK executors used by the
           codex / openai-agents wraps. Maps onto the AP
           allowlist's semantic codes
           (``"rate_limit_exceeded"``, ``"server_error"``,
           ``"timeout"``, ``"connection_error"``).
        3. ``claude_agent_sdk`` exceptions — the Claude Code CLI
           wrapper used by the claude-sdk wrap. Currently only
           ``CLIConnectionError`` maps to a retryable code; the
           other CLI errors (NotFound, JSONDecode, ProcessError)
           are non-retryable and fall through to the base
           implementation so operators see the class name.
        4. ``httpx`` exceptions for SDKs that surface raw
           ``httpx.TimeoutException`` / ``httpx.ConnectError``
           rather than wrapping them.
        5. Fallback: base class implementation
           (``type(exception).__name__``).

        :param exception: The exception :meth:`run_turn` raised.
        :returns: An :class:`ErrorDetail` whose ``code`` matches
            the Omnigent allowlist for known retryable failures, and is
            still informative (provider exception class) for the
            rest.
        """
        from omnigent.errors import OmnigentError
        from omnigent.server.schemas import ErrorDetail

        if isinstance(exception, OmnigentError):
            # Project-internal structured errors already carry a
            # semantic code (e.g. ``RetryableLLMError(code="timeout")``).
            return ErrorDetail(code=exception.code, message=str(exception))

        code = classify_inner_exception(exception)
        if code is not None:
            return ErrorDetail(code=code, message=str(exception))

        # Unknown exception type — preserve the class name so
        # operators can still grep for it in logs, even though
        # AP's retry allowlist won't match.
        return super()._build_error_detail(exception)

    async def on_shutdown(self) -> None:
        """
        Release the inner executor's resources on subprocess
        shutdown.

        Overrides :meth:`HarnessApp.on_shutdown`; called from
        the scaffold's lifespan teardown path. Closes the
        executor's session and the executor itself so child
        processes (e.g. ``claude --output-format``) are reaped
        rather than orphaned.
        """
        if self._executor is not None:
            await self._executor.close_session(self._session_key)
            await self._executor.close()
            self._executor = None


def _classify_openai_exception(exception: BaseException) -> str | None:
    """
    Map an OpenAI SDK exception onto the Omnigent semantic code allowlist.

    The :mod:`openai` package is the runtime SDK for the
    openai-agents-sdk + codex wraps (and the open-responses
    inner executor). Its exception hierarchy mirrors HTTP
    semantics. We only translate the variants that match AP's
    retryable allowlist verbatim — adding more codes here without
    extending the allowlist would just produce strings Omnigent ignores.

    Lazy-imports :mod:`openai` because the package isn't a hard
    dependency of every wrap (the claude-sdk wrap goes through the
    Claude Code CLI, not the OpenAI API). ``ImportError`` returns
    ``None`` so the caller falls through to other classifiers —
    never crashes the error path on a missing optional dep.

    :param exception: The exception :meth:`ExecutorAdapter.run_turn`
        caught.
    :returns: A semantic code from the Omnigent allowlist (e.g.
        ``"rate_limit_exceeded"``), or ``None`` when *exception*
        is not an OpenAI SDK exception we recognize.
    """
    try:
        import openai
    except ImportError:
        return None

    if isinstance(exception, openai.RateLimitError):
        return "rate_limit_exceeded"
    if isinstance(exception, openai.APITimeoutError):
        return "timeout"
    if isinstance(exception, openai.APIConnectionError):
        return "connection_error"
    if isinstance(exception, openai.InternalServerError):
        # 500-class server-side failures from the gateway. Worth
        # retrying — usually transient capacity issues. Matches
        # AP's existing ``"server_error"`` allowlist entry.
        return "server_error"
    # Context-window overflow may arrive as a direct BadRequestError
    # or wrapped inside an openai-agents SDK exception. The generic
    # classifier walks the cause chain for all providers.
    from omnigent.llms.errors import is_context_length_exceeded

    if is_context_length_exceeded(exception):
        return "context_length_exceeded"
    return None


def _classify_claude_sdk_exception(exception: BaseException) -> str | None:
    """
    Map a :mod:`claude_agent_sdk` exception onto the Omnigent semantic
    code allowlist.

    The Claude Code CLI wrapper surfaces a small exception set
    (``CLIConnectionError``, ``CLIJSONDecodeError``,
    ``CLINotFoundError``, ``ProcessError``,
    ``ClaudeSDKError``). Only ``CLIConnectionError`` matches a
    retryable allowlist entry — ``CLINotFoundError`` (binary
    missing) and ``CLIJSONDecodeError`` (corrupt CLI output)
    are non-retryable so they fall through to the base
    implementation where operators see the class name.

    :param exception: The exception :meth:`ExecutorAdapter.run_turn`
        caught.
    :returns: A semantic code from the Omnigent allowlist, or ``None``
        when *exception* isn't a recognized retryable
        :mod:`claude_agent_sdk` exception.
    """
    try:
        import claude_agent_sdk
    except ImportError:
        return None

    # ``CLINotFoundError`` is a subclass of ``CLIConnectionError``
    # in the SDK's hierarchy — match it FIRST and return ``None``
    # so the missing-binary case falls through to the base
    # implementation (non-retryable: a missing CLI won't appear
    # spontaneously). Without this guard, ``isinstance(not_found,
    # CLIConnectionError)`` matches and we'd loop on a permanent
    # configuration failure.
    if isinstance(exception, claude_agent_sdk.CLINotFoundError):
        return None
    if isinstance(exception, claude_agent_sdk.CLIConnectionError):
        return "connection_error"
    return None


def _classify_httpx_exception(exception: BaseException) -> str | None:
    """
    Map an :mod:`httpx` exception onto the Omnigent semantic code allowlist.

    Some inner executors (notably ``litellm``-backed paths for
    non-Anthropic providers) surface raw httpx exceptions instead
    of wrapping them in a provider-specific class. Translating
    these here keeps retry classification accurate regardless of
    which transport layer raised.

    Lazy-imports :mod:`httpx` for symmetry with the Anthropic
    classifier — though httpx is currently a hard project
    dependency, the lazy form keeps this module's import graph
    minimal for harnesses that never raise httpx errors.

    :param exception: The exception :meth:`ExecutorAdapter.run_turn`
        caught.
    :returns: A semantic code from the Omnigent allowlist, or ``None``
        when *exception* is not an httpx exception we recognize.
    """
    try:
        import httpx
    except ImportError:
        return None

    if isinstance(exception, httpx.TimeoutException):
        return "timeout"
    if isinstance(exception, httpx.ConnectError):
        return "connection_error"
    # ``httpx.ReadError`` / ``WriteError`` / ``CloseError`` are
    # transport-level network failures (peer closed the
    # connection mid-stream, broken pipe, etc.). They're
    # siblings of ``ConnectError`` under ``NetworkError`` and
    # behave the same way for retry purposes — the connection
    # is gone, a fresh attempt may succeed. Without this branch,
    # users see the bare class name (``[llm] ReadError``) with
    # no semantic code, and the Omnigent retry classifier treats them
    # as permanent.
    if isinstance(exception, httpx.NetworkError):
        return "connection_error"
    # ``httpx.RemoteProtocolError`` ("peer closed connection without
    # sending complete message body" — the canonical symptom of a
    # subprocess being SIGKILL'd mid-stream). Sits under
    # ``ProtocolError`` → ``TransportError``, NOT under
    # ``NetworkError``, so the prior branch doesn't catch it.
    # Without this branch, a SIGKILL during streaming surfaces as
    # an unrecognized exception and the retry classifier treats it
    # as permanent — the L2 retry layer never fires for the exact
    # case it was designed to recover from.
    if isinstance(exception, httpx.RemoteProtocolError):
        return "connection_error"
    return None


def _classify_anthropic_exception(exception: BaseException) -> str | None:
    """
    Map an :mod:`anthropic` SDK exception onto the Omnigent semantic
    code allowlist.

    The Anthropic Python SDK is the underlying transport for the
    Claude CLI subprocess used by the claude-sdk wrap; raw SDK
    exceptions sometimes surface upward when the CLI fails to
    intercept them (e.g. mid-stream gateway errors that bypass
    the CLI's framing layer). Without a dedicated classifier
    these would fall through to ``[llm] RateLimitError`` /
    ``[llm] APIConnectionError`` etc., which the Omnigent retry
    allowlist doesn't match — silent demotion of retryable
    failures to permanent.

    Lazy-imports :mod:`anthropic` because the package isn't a
    hard dependency of every wrap (only the claude-sdk path
    pulls it in transitively via the CLI). ``ImportError``
    returns ``None`` so the caller falls through to other
    classifiers — matches the established pattern of the
    sibling classifiers in this module.

    Phase 3 of ``designs/RETRY_ACROSS_HARNESSES.md`` —
    consolidates the per-SDK fan-out at
    :func:`classify_inner_exception`.

    :param exception: The exception :meth:`ExecutorAdapter.run_turn`
        caught.
    :returns: A semantic code from the Omnigent allowlist, or ``None``
        when *exception* is not an Anthropic SDK exception we
        recognize.
    """
    try:
        import anthropic
    except ImportError:
        return None

    if isinstance(exception, anthropic.RateLimitError):
        return "rate_limit_exceeded"
    if isinstance(exception, anthropic.APITimeoutError):
        return "timeout"
    if isinstance(exception, anthropic.APIConnectionError):
        return "connection_error"
    if isinstance(exception, anthropic.InternalServerError):
        # 500-class server-side failures from Anthropic — same
        # semantic as ``_classify_openai_exception``: transient
        # capacity issues that retry typically resolves.
        return "server_error"
    return None


def classify_inner_exception(exception: BaseException) -> str | None:
    """
    Map any inner-SDK exception onto the Omnigent semantic code allowlist.

    Single entry point that fans out across the per-SDK
    classifiers. First match wins. Returns ``None`` when no
    classifier recognizes the exception — caller is expected
    to fall back to ``type(exception).__name__`` so operators
    can still grep.

    Phase 3 of ``designs/RETRY_ACROSS_HARNESSES.md``: this
    function replaces the per-call fan-out at
    :meth:`ExecutorAdapter._build_error_detail`'s old code,
    which inlined three separate classifier calls. New
    classifiers (e.g. for a Pi-specific exception type)
    plug in here once and benefit every consumer.

    Order matters when SDK exception hierarchies overlap. Today
    none do — each lazy import only matches its own SDK's
    classes — but if Anthropic ever subclasses an httpx
    exception (or vice versa), a more-specific classifier
    must come first. Documented as a sequence rather than a
    dict so the order is explicit.

    :param exception: The exception
        :meth:`ExecutorAdapter.run_turn` caught.
    :returns: A semantic code from the Omnigent allowlist (e.g.
        ``"rate_limit_exceeded"``), or ``None`` when no
        classifier matched.
    """
    for classifier in (
        _classify_openai_exception,
        _classify_anthropic_exception,
        _classify_claude_sdk_exception,
        _classify_httpx_exception,
    ):
        code = classifier(exception)
        if code is not None:
            return code
    # Generic fallback: context-window overflow from any SDK that
    # stamps a recognized code on the exception or its cause chain
    # but wasn't caught by the per-SDK classifiers above (e.g.
    # because the SDK isn't installed so its classifier short-
    # circuited on ImportError).
    from omnigent.llms.errors import is_context_length_exceeded

    if is_context_length_exceeded(exception):
        return "context_length_exceeded"
    return None


def _normalize_tool_schemas(
    schemas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Flatten OpenAI Chat-Completions tool schemas to inner shape.

    Omnigent emits :meth:`omnigent.tools.base.Tool.get_schema` in
    OpenAI Chat Completions shape: ``{"type": "function",
    "function": {"name", "description", "parameters"}}``. The
    inner :class:`Executor` ABC and its
    :func:`omnigent.inner.claude_sdk_executor._build_mcp_tools`
    consumer read ``schema.get("name")`` / ``schema.get(...)``
    directly — they expect a flat shape. Without this
    translation the inner SDK registers MCP tools with empty
    names, which the LLM cannot call.

    Each schema is converted to:
    ``{"name": ..., "description": ..., "parameters": ...}``.
    Schemas already in flat shape (``"name"`` at top level) pass
    through unchanged so this function is idempotent.

    :param schemas: AP-emitted tool schemas in either shape.
    :returns: Schemas in flat shape ready for the inner
        Executor.
    """
    flat: list[dict[str, Any]] = []
    for schema in schemas:
        # Flat-form: validate ``name`` is non-empty even on the
        # pass-through branch. The inner SDK's
        # ``_build_mcp_tools`` reads ``schema.get("name")``
        # directly; an entry like ``{"name": ""}`` would slip
        # through and produce the "Tool name cannot be empty"
        # warning + render a phantom MCP tool the LLM can't
        # call. Drop those with the same warning the chat-
        # completions branch uses.
        if "name" in schema:
            name = schema.get("name")
            if not isinstance(name, str) or not name:
                _logger.warning(
                    "skipping flat-form tool schema with empty name: %r",
                    schema,
                )
                continue
            flat.append(schema)
            continue
        # Chat-completions form — pull from the ``function``
        # sub-dict. Same skip-on-missing-name rule.
        function = schema.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            _logger.warning("skipping tool schema with no name: %r", schema)
            continue
        translated: dict[str, Any] = {"name": name}
        description = function.get("description")
        if isinstance(description, str):
            translated["description"] = description
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            translated["parameters"] = parameters
        flat.append(translated)
    return flat


async def _bridge_one_dispatch(
    ctx: TurnContext,
    agent: str,
    tool_name: str,
    args: dict[str, Any],
    *,
    call_id: str | None = None,
) -> dict[str, Any]:
    """
    Round-trip one tool call through ``ctx.dispatch_tool``.

    JSON-encodes *args*, awaits the result, and shapes the
    return so the inner Claude SDK's MCP-tool wrapper hands a
    sensible payload back to the LLM. Dispatch failures become
    ``{"error": <str>}`` so the SDK marks the MCP response as
    an error rather than hanging.

    :param ctx: The current turn's context.
    :param agent: Agent name (required on the function_call item).
    :param tool_name: Tool name from the LLM's call.
    :param args: Decoded arguments dict from the LLM.
    :param call_id: Optional explicit ``call_id`` to use on the
        emitted action_required event. When provided (typically
        the SDK's ``tool_use_id`` threaded through
        :meth:`ExecutorAdapter._stable_tool_executor`'s queue),
        lets the SDK client dedupe this event against the inline
        observed event that :meth:`_translate_event` already
        emitted with the same id. ``None`` falls back to a
        freshly-allocated uuid — the dispatch still works but the
        observed and action_required events render as separate
        ``⏵ tool_name`` lines.
    :returns: A dict suitable as the MCP tool result.
    """
    import json

    if call_id is None:
        call_id = f"call_{uuid.uuid4().hex[:12]}"
    try:
        output = await ctx.dispatch_tool(
            call_id=call_id,
            name=tool_name,
            arguments=json.dumps(args),
            agent=agent,
        )
    except Exception as exc:
        _logger.exception("dispatch_tool failed for %s", tool_name)
        return {"error": str(exc)}
    # Try to parse the output as JSON — if Omnigent serialized the
    # tool result via ``ToolResult.content`` (a JSON string),
    # parsing here gives the SDK a structured dict it can show
    # the LLM cleanly. Falls back to raw string under a
    # ``result`` key for non-JSON outputs.
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"result": output}
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


def _extract_last_user_message(
    input_value: str | list[dict[str, Any]],
) -> str:
    """Extract the last user message text from a request input.

    Handles both conversation-history shape (list of message items
    with ``role``/``content``) and single-turn shape (plain string
    or content-block list). Used by tracing to populate the agent
    span's ``user_message`` input.

    :param input_value: The request's ``input`` field.
    :returns: The text of the last user message, or empty string.
    """
    if isinstance(input_value, str):
        return input_value
    # Conversation-history shape: find last user message
    last_user_text = ""
    for item in input_value:
        role = item.get("role")
        if role == "user":
            content = item.get("content")
            if isinstance(content, str):
                last_user_text = content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                if parts:
                    last_user_text = "\n".join(parts)
        # Single-turn content-block shape (no role key)
        elif role is None:
            text = item.get("text")
            if isinstance(text, str):
                last_user_text = text
    return last_user_text


def _extract_user_text(
    input_value: str | list[dict[str, Any]],
) -> str:
    """
    Pull plain-text content from a steering injection's input.

    Used by :meth:`ExecutorAdapter._watch_injections` to convert
    a scaffold-side :class:`CreateResponseRequest` into a single
    string the inner executor's
    :meth:`Executor.enqueue_session_message` can deliver. Mirrors
    the structure of :func:`_translate_input_to_messages` but
    returns a plain string rather than a Message dict.

    :param input_value: The injection's ``input`` field — either
        a plain string (shorthand) or a list of content blocks.
    :returns: Concatenated text content. Empty string when no
        text blocks are present.
    """
    if isinstance(input_value, str):
        return input_value
    parts: list[str] = []
    for block in input_value:
        text_value = block.get("text")
        if isinstance(text_value, str):
            parts.append(text_value)
    return "\n".join(parts)


def _translate_input_to_messages(
    input_value: str | list[dict[str, Any]],
) -> list[Message]:
    """
    Convert :class:`CreateResponseRequest.input` into inner
    :class:`Message` list.

    Two shapes are accepted on the wire:

    1. **Conversation-history shape** — a list whose entries are
       ``{"type": "message", "role": "...", "content": [...]}``
       items, optionally interleaved with typed items
       (``function_call``, ``function_call_output``,
       ``reasoning``, native tool calls). This is what
       :func:`_translate_messages_to_input` produces for the
       full Layer 2 history; one inner :class:`Message` is
       emitted per role-keyed message item, and tool-related
       items are dropped (the inner :class:`ClaudeSDKExecutor`'s
       ``_build_prompt`` only consumes role-keyed messages
       when serializing the "Conversation so far:" prefix). The
       contract here is "preserve user/assistant turns;
       tool-call records are reconstructed from the SDK's own
       Layer 1 state on resume," which is the right tradeoff
       for the small inner-Message contract.
    2. **Single-turn fallback shape** — a plain string or a
       list of content-block dicts (``input_text``, etc.) with
       no role wrappers. AP's older clients (and any direct
       hand-off that hasn't been migrated yet) still send this.
       Concatenated into a single user-role message — same
       behavior the harness has always had for the
       single-message case.

    :param input_value: The request's ``input`` field.
    :returns: One inner :class:`Message` per conversation turn
        when history is present; a single user message
        otherwise.
    """
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]

    history_messages = _extract_role_keyed_messages(input_value)
    if history_messages:
        return history_messages

    # Fallback: legacy single-turn path for inputs that don't
    # carry role wrappers (e.g. a bare list of content blocks).
    # Preserves multimodal blocks when present.
    content = _normalize_message_content(input_value)
    return [{"role": "user", "content": content}]


def _extract_role_keyed_messages(
    input_value: list[dict[str, Any]],
) -> list[Message]:
    """
    Pull role-keyed message items out of an Omnigent ``input`` list.

    Looks for ``{"type": "message", "role": ..., "content": ...}``
    entries (the shape :func:`_translate_messages_to_input`
    produces from the workflow's history list). Each match
    becomes one inner :class:`Message` whose content is either
    a plain string (text-only messages) or the original list
    of typed content blocks (when ``input_image`` /
    ``input_file`` blocks are present alongside text).

    Tool-call items (``function_call``, ``function_call_output``,
    ``reasoning``, native tool items) are intentionally
    skipped: the inner SDK reconstructs those from its own
    Layer 1 state when resuming, so duplicating them in the
    serialized prompt would just confuse the LLM.

    :param input_value: The Omnigent ``input`` list.
    :returns: One :class:`Message` per role-keyed entry, or
        an empty list when *input_value* contains no
        role-keyed items (caller falls back to the legacy
        single-user-message path).
    """
    messages: list[Message] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or "role" not in item:
            continue
        role = item["role"]
        content = _normalize_message_content(item.get("content"))
        # Skip empty messages (e.g. assistant turns that only
        # produced tool calls without text). Their absence
        # doesn't break the prompt — the prior user turn still
        # carries the question and the next user turn carries
        # the follow-up.
        if not content:
            continue
        messages.append({"role": role, "content": content})
    return messages


_MULTIMODAL_BLOCK_TYPES: frozenset[str] = frozenset({"input_image", "input_file", "input_audio"})


def _normalize_message_content(
    content: Any,
) -> str | list[dict[str, Any]]:
    """
    Normalize Responses API message ``content`` for inner executors.

    When the content list contains only text blocks
    (``input_text`` / ``output_text``), collapses to a plain
    string (the common text-only fast path). When multimodal
    blocks (``input_image``, ``input_file``, ``input_audio``)
    are present, returns the full block list so inner executors
    can forward image/file content to the LLM.

    :param content: The ``content`` field of a Responses API
        message item, e.g. ``[{"type": "input_text",
        "text": "..."}]``.
    :returns: A plain string when all blocks are text-only, or
        the original content block list when multimodal blocks
        are present. Empty string for ``None`` or non-list
        inputs.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    has_multimodal = any(
        isinstance(block, dict) and block.get("type") in _MULTIMODAL_BLOCK_TYPES
        for block in content
    )
    if has_multimodal:
        return content

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text_value = block.get("text")
        if isinstance(text_value, str):
            parts.append(text_value)
    return "\n".join(parts)


def _serialize_args(args: dict[str, Any]) -> str:
    """
    JSON-encode a tool-call arguments dict.

    :param args: The arguments dict from
        :class:`ToolCallRequest.args`.
    :returns: A JSON string the AP-side function_call item
        carries verbatim.
    """
    import json

    encoded: str = json.dumps(args)
    return encoded


def _serialize_tool_result(event: ToolCallComplete) -> str:
    """
    Stringify a :class:`ToolCallComplete` for the
    function_call_output's ``output`` field.

    The inner executor populates ``result`` with whatever shape
    the wrapped SDK hands back. For the Claude SDK that's the
    Anthropic ``ToolResultBlock.content`` value, which can be
    one of:

    - A plain ``str`` — pass through unchanged.
    - A list of typed content blocks (``{"type": "text", "text":
      ...}``, image blocks, etc.) — join the ``text`` fields so
      the LLM-rendered tool output reaches Omnigent intact. Without
      this branch, ``str([{...}])`` would emit a Python repr
      (literal ``[{'type': 'text', 'text': '...'}]``) and
      function_call_output would carry garbage instead of the
      actual command output.
    - Anything else (dicts, ints, ``None``, …) — best-effort
      JSON encode, falling back to ``repr`` so we never
      silently drop the value.

    Failure path:

    - ``error`` is preferred when ``result`` is None and the
      status carries one.
    - Empty string is a last resort — the inner Executor
      contract is that one of result/error is populated;
      logged so a regression surfaces in the Omnigent logs.

    :param event: The completion event.
    :returns: A string suitable for the AP-side
        function_call_output's ``output`` field.
    """
    if event.result is not None:
        return _stringify_tool_payload(event.result)
    if event.error is not None:
        return f"[error] {event.error}"
    _logger.warning(
        "ToolCallComplete for %s had neither result nor error",
        event.name,
    )
    return ""


def _stringify_tool_payload(value: Any) -> str:
    """
    Coerce a tool's result payload into a string.

    See :func:`_serialize_tool_result` for the full rationale —
    this helper is split out so the list-of-blocks branch is
    independently testable without standing up a full
    :class:`ToolCallComplete`.

    :param value: Anything the inner Executor put in
        ``ToolCallComplete.result``.
    :returns: A string suitable for serialization on the wire.
    """
    import json

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Anthropic-style content blocks: pull out every
        # ``text`` field. Non-text blocks (image, tool_use, etc.)
        # have no string projection here and are skipped — a
        # follow-up could route image blocks into AP's file-
        # store, but for the v1 stdout-of-Bash use case skipping
        # them is correct.
        text_parts: list[str] = []
        for block in value:
            if not isinstance(block, dict):
                continue
            block_text = block.get("text")
            if isinstance(block_text, str):
                text_parts.append(block_text)
        if text_parts:
            return "".join(text_parts)
        # No usable text blocks — fall through to JSON encode so
        # callers still get a structured representation rather
        # than an empty string.
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def _call_id_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    """
    Extract a call_id from an executor's per-call metadata dict.

    Different inner executors stash the call_id under different
    keys (Claude SDK uses ``"call_id"``; Codex uses ``"call_id"``
    on the protocol envelope; Pi uses an opaque id). For v1 we
    look for the conventional ``"call_id"`` key first; harness
    wraps that need other keys can subclass and override.

    :param metadata: The metadata dict from
        :class:`ToolCallRequest.metadata` (or ``None``).
    :returns: The call_id string if present and stringy, else
        ``None``.
    """
    if not metadata:
        return None
    value = metadata.get("call_id")
    if isinstance(value, str):
        return value
    return None
