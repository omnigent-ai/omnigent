"""
Shared :class:`HarnessApp` adapter that drives any inner :class:`Executor`.

The four per-harness wraps (claude-sdk, codex, pi, openai-agents-sdk) all use this adapter —
they differ only in the executor they construct. The adapter handles lazy executor construction,
per-turn message/event translation, tool/elicitation/policy bridging, cancellation propagation,
injection forwarding, and shutdown cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import uuid
from collections import deque
from collections.abc import Callable
from typing import Any

from fastapi import Response

from omnigent.errors import ElicitationDeclinedError
from omnigent.inner.executor import (
    CompactionComplete,
    CompactionStarted,
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
from omnigent.policies.types import FAIL_CLOSED_PHASES
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

# Observed tool calls use "in_progress" (distinct from "action_required" for server-dispatched).
_OBSERVED_TOOL_CALL_STATUS = "in_progress"


# Prefix for Claude SDK MCP-registered tool names (e.g. ``mcp__omnigent__sys_terminal_launch``).
# Inline observed events are emitted at ToolCallRequest; the ``tool_use_id`` is queued so the
# post-stream dispatch reuses the same ``call_id``, letting BlockStream dedupe the two renders.
_MCP_TOOL_NAME_PREFIX = "mcp__"


# Interrupt and reap use SEPARATE budgets: the short slice keeps a wedged interrupt from
# starving the reap (subprocess-backed executors terminate only in close/close_session).
INTERRUPT_TIMEOUT_S = 3.0
_INTERRUPT_SLICE_S = 1.5

# Consecutive orphaned tool callbacks (no active turn context) before forcing a Tier-1 SDK reset.
# Reset to zero at each ``run_turn`` start so a single late straggler never trips it.
_ORPHAN_RESYNC_THRESHOLD = 3


def _strip_mcp_tool_prefix(name: str) -> str:
    """Strip the ``mcp__{server}__`` prefix from a Claude SDK tool name.

    Kept local (not imported from workflow) to avoid a cross-module dependency.
    Only ``mcp__<server>__<tool>`` shapes are stripped; bare names pass through.
    """
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


# Prefix for local host-tool-bridge calls (``sys_os_*``). An orphaned host-tool callback
# is the deterministic respawn-desync signature, so it triggers a Tier-1 reset on the first
# occurrence rather than waiting for the consecutive-orphan threshold.
_HOST_TOOL_PREFIX = "sys_os_"


def _is_host_tool(tool_name: str) -> bool:
    """Return ``True`` for ``sys_os_*`` host-tool-bridge calls (bare or MCP-prefixed)."""
    return _strip_mcp_tool_prefix(tool_name).startswith(_HOST_TOOL_PREFIX)


class ExecutorAdapter(HarnessApp):
    """
    :class:`HarnessApp` subclass that drives any inner :class:`Executor`.

    Lazily constructs the executor on the first turn, then reuses it for the conversation lifetime.
    Handles per-turn message/event translation, tool/elicitation/policy bridging, cancellation,
    injection forwarding, and tracing. Per-harness wraps differ only in the ``executor_factory``.
    """

    def __init__(
        self,
        executor_factory: Callable[[], Executor],
        session_key: str | None = None,
        harness_label: str = "Claude",
    ) -> None:
        super().__init__()
        self._executor_factory = executor_factory
        self._session_key = session_key or uuid.uuid4().hex
        # Harness label used in elicitation messages, e.g. "Claude wants to call **Bash**".
        self._harness_label = harness_label
        # Lazily-constructed inner executor, reused across turns for the conversation lifetime.
        self._executor: Executor | None = None
        # Per-turn turn context. The stable tool/elicitation/policy bridges read this at call
        # time so MCP handlers that closure-captured the bridge on turn 1 dispatch into the
        # current turn's ctx, not turn 1's dead ctx.
        self._current_ctx: TurnContext | None = None
        self._current_agent: str | None = None
        # FIFO queue of inner-SDK tool-use ids from ToolCallRequest events. Drained by
        # _stable_tool_executor so each dispatch reuses the same call_id as the inline
        # observed event, letting BlockStream dedupe the two renders into one.
        self._pending_mcp_call_ids: deque[str] = deque()
        # Per-session tracing context; created lazily on first turn and reused across turns.
        self._tracing_ctx: TracingContext | None = None
        # Detached bounded background tasks (abnormal-exit _safe_interrupt). Strongly referenced
        # until completion; drained on shutdown. Never awaited inline to avoid blocking teardown.
        self._bg_tasks: set[asyncio.Task[None]] = set()
        # Consecutive orphaned-callback counter and reset guard for the Tier-1 SDK watchdog.
        self._orphan_callback_count = 0
        self._resyncing = False
        # Call ids round-tripped via _stable_tool_executor → dispatch_tool this turn.
        # dispatch_tool emits the function_call_output, so ToolCallComplete for these ids
        # must be suppressed in _translate_event to avoid duplicates.
        self._dispatched_call_ids: set[str] = set()

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        """Drive the inner executor for one turn, translating its events to Omnigent SSE.

        Lazily constructs the executor on the first call; subsequent calls reuse the cached
        instance. Installs stable tool/elicitation/policy bridges once on first use.
        """
        executor = self._ensure_executor()
        messages = _translate_input_to_messages(request.input)
        # Stamp session_key on every message so the inner executor keys its client consistently,
        # matching the key used by enqueue_session_message (steering delivery depends on this).
        for message in messages:
            message["session_id"] = self._session_key
        extra: dict[str, Any] = {}
        if request.reasoning is not None:
            effort = request.reasoning.get("effort")
            if effort:
                extra["reasoning_effort"] = effort
        if request.max_output_tokens is not None:
            extra["max_tokens"] = int(request.max_output_tokens)
        # model_override is the per-request override; takes precedence over the spec default.
        config = ExecutorConfig(model=request.model_override, extra=extra)
        tools = _normalize_tool_schemas(request.tools or [])
        system_prompt = request.instructions or ""
        # Install stable bridges once on first use — the SDK closure-captures them from the first
        # turn; per-turn rebinding would leave later tool calls dispatching into a dead ctx.
        if getattr(executor, "_tool_executor", None) is None:
            executor._tool_executor = self._stable_tool_executor  # type: ignore[attr-defined]
        if getattr(executor, "_elicitation_handler", None) is None:
            executor._elicitation_handler = self._stable_elicitation_handler  # type: ignore[attr-defined]
        if getattr(executor, "_policy_evaluator", None) is None:
            executor._policy_evaluator = self._stable_policy_evaluator  # type: ignore[attr-defined]
        self._current_ctx = ctx
        self._current_agent = request.model
        # Clear per-turn state so stale entries from an errored prior turn don't bleed through.
        self._pending_mcp_call_ids.clear()
        # Reset orphan counter: measures only post-bind orphans (no intervening clean turn).
        self._orphan_callback_count = 0
        # Set True before each genuine-completion return so the finally can schedule a
        # bounded interrupt only on abnormal exits (CancelledError, ExecutorError, etc.).
        clean_exit = False
        self._dispatched_call_ids.clear()

        tracing = is_tracing_enabled()
        from omnigent.runtime.telemetry import current_session_id, session_scope

        turn_session_id = current_session_id() or self._session_key
        if tracing and self._tracing_ctx is None:
            self._tracing_ctx = TracingContext(session_id=turn_session_id)
        tctx = self._tracing_ctx if tracing else None
        agent_span = None
        # Active tool span for correlating ToolCallRequest → ToolCallComplete.
        _active_tool_span = None
        _active_tool_parent = None

        user_message = _extract_last_user_message(request.input)

        # Watcher forwards mid-turn steering injections into the in-flight executor session.
        injection_watcher = asyncio.create_task(
            self._watch_injections(ctx, executor),
            name=f"executor-adapter-injection-watch:{ctx.response_id}",
        )
        try:
            trace_cm: contextlib.AbstractContextManager[None] = contextlib.nullcontext()
            if tctx:
                try:
                    from omnigent.runtime.telemetry import trace_context_for_response

                    trace_cm = trace_context_for_response(response_id=ctx.response_id)
                except Exception:
                    _logger.debug("trace_context_for_response unavailable", exc_info=True)
            with session_scope(turn_session_id), trace_cm:
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
                            tctx.end_agent_span(agent_span, response=None)
                            agent_span = None
                        # Mark clean_exit AFTER the interrupt completes: if it raises,
                        # the finally's _safe_interrupt fallback still fires.
                        await executor.interrupt_session(self._session_key)
                        clean_exit = True
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
                                    error=event.error,
                                    duration_ms=event.duration_ms,
                                    parent_span=_active_tool_parent,
                                )
                                _active_tool_span = None
                                _active_tool_parent = None
                        elif isinstance(event, TurnComplete):
                            response_text = event.response
                            if event.usage is not None and agent_span is not None:
                                from omnigent.runtime.telemetry import record_llm_usage

                                record_llm_usage(agent_span, event.usage)
                    # --- End tracing ---
                    self._translate_event(event, ctx)
                    if isinstance(event, TurnComplete):
                        if tctx is not None and agent_span is not None:
                            tctx.end_agent_span(agent_span, response=response_text)
                            agent_span = None
                        clean_exit = True
                        return
                    if isinstance(event, TurnCancelled):
                        ctx.cancelled.set()
                        if tctx is not None and agent_span is not None:
                            from omnigent.runtime.telemetry import record_cancellation

                            record_cancellation(agent_span)
                            tctx.end_agent_span(agent_span, response=None, error="cancelled")
                        # Inner executor wound down cleanly.
                        clean_exit = True
                        return
                    if isinstance(event, ExecutorError):
                        if tctx is not None and agent_span is not None:
                            tctx.end_agent_span(
                                agent_span,
                                response=None,
                                error=event.message,
                            )
                            agent_span = None
                        # Guard: empty message surfaces as "inner executor error: " with no detail.
                        detail = event.message or "no detail reported (see runner/harness logs)"
                        raise RuntimeError(f"inner executor error: {detail}")
        except ElicitationDeclinedError:
            # Fallback for non-SDK executors; SDK-based paths use ctx.cancelled.set() instead.
            _logger.info(
                "elicitation explicitly declined for response %s — aborting turn",
                ctx.response_id,
            )
            if tctx is not None and agent_span is not None:
                from omnigent.runtime.telemetry import record_cancellation

                record_cancellation(agent_span)
                tctx.end_agent_span(agent_span, response=None)
            ctx.cancelled.set()
            if self._executor is not None:
                await self._executor.interrupt_session(self._session_key)
        except BaseException:
            # Close the span so it doesn't leak on the OTel provider.
            if tctx is not None and agent_span is not None:
                tctx.end_agent_span(agent_span, response=None, error="unhandled exception")
                agent_span = None
            raise
        finally:
            # Cancel the injection watcher (long-polls a queue; cancellation is the only way out).
            injection_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await injection_watcher
            if tctx is not None:
                try:
                    from opentelemetry import trace as otel_trace

                    provider = otel_trace.get_tracer_provider()
                    if hasattr(provider, "force_flush"):
                        provider.force_flush(timeout_millis=5000)
                except Exception:
                    pass
            # Compare-and-clear: only null the slot when it still points at THIS turn's ctx
            # so a late-unwinding finally doesn't clobber a newer turn's binding.
            if self._current_ctx is ctx:
                self._current_ctx = None
                self._current_agent = None
            # On abnormal exit, detach and interrupt the executor synchronously before scheduling
            # the background reap — a continuation turn must not reuse the abandoned executor.
            if not clean_exit:
                abandoned_executor = self._executor
                self._executor = None
                interrupt_task = asyncio.create_task(
                    self._safe_interrupt(abandoned_executor, self._session_key)
                )
                self._bg_tasks.add(interrupt_task)
                interrupt_task.add_done_callback(self._bg_tasks.discard)

    async def _handle_interrupt_event(self) -> Response:
        """Cancel the turn and drop the inner executor session synchronously.

        Extends the base handler to also call ``interrupt_session`` — ensuring the in-flight
        generation stops even when the run loop is blocked awaiting the first token.
        """
        response = await super()._handle_interrupt_event()
        if self._executor is not None:
            await self._executor.interrupt_session(self._session_key)
        return response

    async def _safe_interrupt(self, executor: Executor | None, session_key: str) -> None:
        """Best-effort bounded interrupt + close of a detached abandoned executor.

        Scheduled (never awaited inline) from run_turn's finally on abnormal exit. The interrupt
        gets a short slice; the reap (close_session + close) always runs under its own budget so
        a wedged interrupt can never starve the subprocess reap.
        """
        if executor is None:
            return
        try:
            await asyncio.wait_for(
                executor.interrupt_session(session_key), timeout=_INTERRUPT_SLICE_S
            )
        except Exception:  # best-effort: a failed/timed-out interrupt is logged, not raised
            _logger.error(
                "abnormal-exit interrupt of inner session %s failed or timed out",
                session_key,
                exc_info=True,
            )

        async def _reap() -> None:
            with contextlib.suppress(Exception):
                await executor.close_session(session_key)
            with contextlib.suppress(Exception):
                await executor.close()

        with contextlib.suppress(Exception):
            await asyncio.wait_for(_reap(), timeout=INTERRUPT_TIMEOUT_S)

    async def _maybe_resync_on_orphan(self, *, force: bool = False) -> None:
        """Tier-1 SDK reset after repeated orphan callbacks (or immediately when ``force=True``).

        Drops the cached executor so the next turn rebuilds a fresh client. ``force=True`` is used
        for orphaned ``sys_os_*`` callbacks — the deterministic respawn-desync signature — to reset
        on the first one rather than waiting for the threshold.
        Idempotent: ``_resyncing`` ensures a burst of orphans triggers exactly one reset.
        """
        if not force and self._orphan_callback_count < _ORPHAN_RESYNC_THRESHOLD:
            return
        if self._resyncing:
            return
        self._resyncing = True
        try:
            async with self._lock:
                # Re-check under lock: a concurrent reset may have already zeroed the counter.
                if not force and self._orphan_callback_count < _ORPHAN_RESYNC_THRESHOLD:
                    return
                _logger.error(
                    "runner_turn_context_desync: %d consecutive orphan callbacks; "
                    "forcing Tier-1 SDK reset for session %s",
                    self._orphan_callback_count,
                    self._session_key,
                )
                executor = self._executor
                self._executor = None
                self._current_ctx = None
                self._current_agent = None
                # Do NOT touch _in_flight or _active_turn_ctx — those are scaffold-owned and
                # back pending tool futures; clearing them would strand or hang dispatches.
                self._orphan_callback_count = 0
            # Close outside the lock — close_session can block on subprocess teardown.
            if executor is not None:

                async def _close(exc: Executor = executor) -> None:
                    with contextlib.suppress(Exception):
                        await exc.close_session(self._session_key)
                    with contextlib.suppress(Exception):
                        await exc.close()

                with contextlib.suppress(Exception):
                    await asyncio.wait_for(_close(), timeout=INTERRUPT_TIMEOUT_S)
        finally:
            self._resyncing = False

    async def _watch_injections(self, ctx: TurnContext, executor: Executor) -> None:
        """Forward mid-turn steering injections into the in-flight executor session.

        Loops until cancelled by run_turn's finally. Best-effort — a failed or malformed
        injection logs and continues rather than raising.
        """
        while True:
            try:
                injection = await ctx.next_injection(timeout=None)
            except asyncio.CancelledError:
                return
            if injection is None:
                # None signals a sentinel or protocol change; bail to avoid spinning.
                return
            text = _extract_user_text(injection.input)
            if not text:
                _logger.warning(
                    "skipping in-band injection with no text payload: %r",
                    injection.input,
                )
                continue
            if ctx.cancelled.is_set():
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
            # Echo injection_id so the runner drops the buffered copy and doesn't re-deliver it.
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
        """Bridge installed once on the executor; dispatches tool calls into the current turn ctx.

        Reads ``_current_ctx`` at call time. Returns a structured error for orphaned callbacks.
        """
        ctx = self._current_ctx
        agent = self._current_agent
        if ctx is None or agent is None:
            # Orphaned callback — safe-fail to avoid parking a Future on a dead ctx.
            # Host-tool orphans are the deterministic respawn-desync signature, so force
            # Tier-1 reset immediately (only when the scaffold also has no live turn).
            self._orphan_callback_count += 1
            host_tool = _is_host_tool(tool_name) and self._active_turn_ctx is None
            _logger.error(
                "runner_turn_context_desync: tool callback fired with no active "
                "turn context (tool=%s, host_tool=%s, consecutive_orphans=%d); "
                "returning error",
                tool_name,
                host_tool,
                self._orphan_callback_count,
            )
            await self._maybe_resync_on_orphan(force=host_tool)
            return {
                "error": "no active turn context for tool dispatch",
                "code": "runner_turn_context_desync",
            }
        # Pop the queued tool_use_id so the dispatch reuses the observed event's call_id.
        # The callback receives bare names (MCP wrapper strips the prefix), so we pop
        # unconditionally — non-MCP paths don't populate the queue.
        correlated_call_id: str | None = None
        if self._pending_mcp_call_ids:
            correlated_call_id = self._pending_mcp_call_ids.popleft()
        # Allocate id here to record in _dispatched_call_ids; matching ToolCallComplete
        # is suppressed in _translate_event (dispatch_tool already emits its output).

        dispatch_call_id = correlated_call_id or f"call_{uuid.uuid4().hex[:12]}"
        self._dispatched_call_ids.add(dispatch_call_id)
        return await _bridge_one_dispatch(
            ctx,
            agent,
            tool_name,
            args,
            call_id=dispatch_call_id,
        )

    async def _stable_elicitation_handler(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> bool:
        """Stable bridge for tool-permission elicitation (``can_use_tool``).

        Reads ``_current_ctx`` at call time; denies by default when no turn is active.
        """
        ctx = self._current_ctx
        if ctx is None:
            # No active turn — deny by default (fail safe, never grant unreviewed permission).
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

        label = self._harness_label
        policy_name = f"{label.lower()}_sdk_permission"
        params = ElicitationRequestParams(
            mode="form",
            message=f"{label} wants to use **{tool_name}**",
            requestedSchema=None,
            url=None,
            phase="tool_call",
            policy_name=policy_name,
            content_preview=f"{tool_name}({preview})",
        )
        result = await ctx.elicit(elicitation_id, params)
        if result.action == "decline":
            # Signal via ctx.cancelled (the SDK swallows exceptions from control-request tasks).
            ctx.cancelled.set()
        return result.action == "accept"

    async def _stable_policy_evaluator(
        self,
        phase: str,
        data: dict[str, Any],
    ) -> PolicyVerdictPayload:
        """Stable bridge for policy evaluation; routes through ``ctx.evaluate_policy``.

        Fails closed (DENY) for tool-call phases when no turn context is active.
        """
        ctx = self._current_ctx
        if ctx is None:
            # Orphaned callback — fail closed for tool-call phases; ALLOW for advisory ones.
            fail_closed = phase in FAIL_CLOSED_PHASES
            action = "POLICY_ACTION_DENY" if fail_closed else "POLICY_ACTION_ALLOW"
            self._orphan_callback_count += 1
            _logger.error(
                "runner_turn_context_desync: policy evaluator fired with no active "
                "turn context (phase=%s, consecutive_orphans=%d); defaulting to %s",
                phase,
                self._orphan_callback_count,
                action,
            )
            await self._maybe_resync_on_orphan()
            return PolicyVerdictPayload(
                action=action,
                reason=(
                    f"No active turn context; failing closed for {phase}." if fail_closed else None
                ),
            )
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
        """Translate one inner ExecutorEvent into Omnigent SSE events via ``ctx.emit``."""
        if isinstance(event, TextChunk):
            ctx.emit(
                OutputTextDeltaEvent(
                    type="response.output_text.delta",
                    delta=event.text,
                )
            )
        elif isinstance(event, ReasoningChunk):
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
            # Emit observed function_call INLINE (interleaved with text). Queue the tool_use_id
            # so the post-stream dispatch reuses the same call_id for deduplication.
            # Emit bare names (strip MCP prefix) to match the Omnigent wire shape.
            tool_use_id = _call_id_from_metadata(event.metadata)
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
            # Suppress dispatched ids (dispatch_tool already emitted their output) and empty ids
            # (unpaiable — they'd render a ghost card). Internally-run tools (e.g. antigravity)
            # stamp real ids and are the sole output source; they must not be suppressed.
            call_id = _call_id_from_metadata(getattr(event, "metadata", None)) or ""
            if not call_id or call_id in self._dispatched_call_ids:
                return
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
            if event.response is not None:
                pass
            # Capture provider-reported usage for the response.completed payload.
            if event.usage is not None:
                ctx.provider_usage = event.usage
        elif isinstance(event, CompactionStarted):
            from omnigent.server.schemas import CompactionInProgressEvent

            ctx.emit(
                CompactionInProgressEvent(
                    type="response.compaction.in_progress",
                )
            )
        elif isinstance(event, CompactionComplete):
            from omnigent.server.schemas import CompactionCompletedEvent

            ctx.emit(
                CompactionCompletedEvent(
                    type="response.compaction.completed",
                    total_tokens=event.token_count,
                    summary=event.summary,
                    summary_model=event.model,
                    compacted_messages=event.compacted_messages,
                )
            )
        # ExecutorError handled by the caller (re-raises so the
        # scaffold can build a response.failed terminal event).

    def _build_error_detail(self, exception: BaseException) -> Any:
        """Map an exception to a semantic code the Omnigent retry allowlist recognizes.

        OmnigentError uses its own ``code``; others go through ``classify_inner_exception``.
        Unknown types fall back to base class (``type(exception).__name__``).
        """
        from omnigent.errors import OmnigentError
        from omnigent.server.schemas import ErrorDetail

        if isinstance(exception, OmnigentError):
            return ErrorDetail(code=exception.code, message=str(exception))

        code = classify_inner_exception(exception)
        if code is not None:
            return ErrorDetail(code=code, message=str(exception))

        return super()._build_error_detail(exception)

    async def on_shutdown(self) -> None:
        """Drain background interrupt tasks, then close the executor session and executor."""
        if self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)
            self._bg_tasks.clear()
        if self._executor is not None:
            await self._executor.close_session(self._session_key)
            await self._executor.close()
            self._executor = None


def _classify_openai_exception(exception: BaseException) -> str | None:
    """Map an OpenAI SDK exception to a semantic code. Returns ``None`` when not recognized."""
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
        return "server_error"
    from omnigent.llms.errors import is_context_length_exceeded

    if is_context_length_exceeded(exception):
        return "context_length_exceeded"
    return None


def _classify_claude_sdk_exception(exception: BaseException) -> str | None:
    """Map a ``claude_agent_sdk`` exception to a code; only ``CLIConnectionError`` is retryable."""
    try:
        import claude_agent_sdk
    except ImportError:
        return None

    # CLINotFoundError is a CLIConnectionError subclass — match first to keep it non-retryable.
    if isinstance(exception, claude_agent_sdk.CLINotFoundError):
        return None
    if isinstance(exception, claude_agent_sdk.CLIConnectionError):
        return "connection_error"
    return None


def _classify_httpx_exception(exception: BaseException) -> str | None:
    """Map a raw ``httpx`` exception to a semantic code for transports that don't wrap it."""
    try:
        import httpx
    except ImportError:
        return None

    if isinstance(exception, httpx.TimeoutException):
        return "timeout"
    if isinstance(exception, httpx.ConnectError):
        return "connection_error"
    # NetworkError covers ReadError/WriteError/CloseError — peer closed mid-stream, broken pipe.
    if isinstance(exception, httpx.NetworkError):
        return "connection_error"
    # RemoteProtocolError sits under ProtocolError, not NetworkError — the prior branch misses it.
    if isinstance(exception, httpx.RemoteProtocolError):
        return "connection_error"
    return None


def _classify_anthropic_exception(exception: BaseException) -> str | None:
    """Map an Anthropic SDK exception to a semantic code. Returns ``None`` when not recognized."""
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return None

    if isinstance(exception, anthropic.RateLimitError):
        return "rate_limit_exceeded"
    if isinstance(exception, anthropic.APITimeoutError):
        return "timeout"
    if isinstance(exception, anthropic.APIConnectionError):
        return "connection_error"
    if isinstance(exception, anthropic.InternalServerError):
        return "server_error"
    return None


def classify_inner_exception(exception: BaseException) -> str | None:
    """Fan out across per-SDK classifiers; first match wins. Returns ``None`` when unrecognized.

    New classifiers plug in here once. Order matters if SDK hierarchies ever overlap —
    more-specific classifiers should come first.
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
    from omnigent.llms.errors import is_context_length_exceeded

    if is_context_length_exceeded(exception):
        return "context_length_exceeded"
    return None


def _normalize_tool_schemas(
    schemas: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten Chat-Completions ``{"type":"function","function":{...}}`` to inner flat shape.

    Idempotent: schemas with ``"name"`` at top level pass through unchanged.
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
    """Round-trip one tool call through ``ctx.dispatch_tool``; returns an MCP-compatible dict.

    When ``call_id`` matches the inline observed event's id, BlockStream dedupes into one render.
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
    # Parse as JSON if possible; fall back to ``{"result": output}`` for non-JSON payloads.
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
    """Extract the last user message text for tracing (history shape or single-turn fallback)."""
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
    """Extract concatenated text from an injection input for ``enqueue_session_message``."""
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
    """Convert a CreateResponseRequest input to inner Message list.

    Accepts conversation-history shape (role-keyed messages; tool items dropped) and
    single-turn fallback shape (plain string or content blocks, collapsed to one user message).
    """
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]

    history_messages = _extract_role_keyed_messages(input_value)
    if history_messages:
        return history_messages

    content = _normalize_message_content(input_value)
    return [{"role": "user", "content": content}]


def _extract_role_keyed_messages(
    input_value: list[dict[str, Any]],
) -> list[Message]:
    """Extract role-keyed message items from an Omnigent input list.

    Tool-call items (function_call, function_call_output, etc.) are skipped — the inner SDK
    reconstructs them from its own Layer 1 state. Returns empty list for non-history inputs.
    """
    messages: list[Message] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or "role" not in item:
            continue
        role = item["role"]
        content = _normalize_message_content(item.get("content"))
        if not content:
            continue
        messages.append({"role": role, "content": content})
    return messages


_MULTIMODAL_BLOCK_TYPES: frozenset[str] = frozenset({"input_image", "input_file", "input_audio"})


def _normalize_message_content(
    content: Any,
) -> str | list[dict[str, Any]]:
    """Normalize message content: plain string for text-only; full block list for multimodal."""
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
    """JSON-encode a tool-call arguments dict."""
    import json

    encoded: str = json.dumps(args)
    return encoded


def _serialize_tool_result(event: ToolCallComplete) -> str:
    """Stringify a ToolCallComplete for function_call_output (result preferred, error fallback)."""
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
    """Coerce a result payload to string (str pass-through, content-block join, JSON fallback)."""
    import json

    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Anthropic-style content blocks: join text fields; skip non-text (image, tool_use, etc.).
        text_parts: list[str] = []
        for block in value:
            if not isinstance(block, dict):
                continue
            block_text = block.get("text")
            if isinstance(block_text, str):
                text_parts.append(block_text)
        if text_parts:
            return "".join(text_parts)
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)


def _call_id_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Extract the ``call_id`` string from a metadata dict, or ``None`` if absent."""
    if not metadata:
        return None
    value = metadata.get("call_id")
    if isinstance(value, str):
        return value
    return None
