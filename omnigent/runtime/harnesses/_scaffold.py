"""
:class:`HarnessApp` scaffold — base class every per-harness wrap
inherits.

The scaffold owns the boilerplate every harness needs:

- A FastAPI app exposing the harness API subset
  (POST /v1/sessions/{conversation_id}/events for the discriminated
  downward-event surface, GET /health). See
  ``designs/SERVER_HARNESS_CONTRACT.md`` §The Harness API Subset
  and ``designs/session_rearchitecture.md`` §3.
- Per-turn in-memory state: dicts of Futures the event handlers
  resolve when a ``tool_result`` event (tool output) or
  ``approval`` event (elicitation reply) arrives. Layer 2
  (per-turn) and Layer 3 (per-tool-dispatch) bookkeeping per
  §Harness in-memory state.
- Heartbeat emitter at :data:`_HEARTBEAT_INTERVAL_S` cadence.
- Cancellation event set by an ``interrupt`` event — the
  per-harness ``run_turn`` should poll it before each LLM
  iteration / native tool call.
- In-band routing for steering: a ``message`` event with
  ``previous_response_id`` matching the in-flight turn becomes
  an injection rather than starting a new turn (also covers
  async function-call completions).
- Graceful SIGTERM / SIGINT handling: cancel the in-flight turn,
  drain pending tool results for a short grace window, then shut
  down the FastAPI app.

A subclass implements just two things: an ``async def run_turn(
request, ctx) -> None`` method that runs the LLM-tool loop using
``ctx`` to interact with upstream, and a ``create_app()``-style
free function that instantiates the subclass and returns its
FastAPI app for the runner to serve.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.tool_output import cap_tool_output
from omnigent.server.schemas import (
    CompletedEvent,
    CreatedEvent,
    CreateResponseRequest,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    ElicitationResult,
    FailedEvent,
    HarnessStreamEvent,
    HeartbeatEvent,
    InProgressEvent,
    OutputItemDoneEvent,
    PolicyEvaluationRequestEvent,
    ResponseObject,
    ServerStreamEvent,
    Usage,
)

_logger = logging.getLogger(__name__)

# Cadence for ``response.heartbeat`` SSE events while a turn is
# streaming. Matches the existing Omnigent cadence at
# ``omnigent/runtime/workflow.py:3732`` — 15 seconds is short
# enough that ~3 missed intervals (45s) is a reasonable
# dead-detection threshold for AP, long enough that the
# per-process emit overhead is negligible.
_HEARTBEAT_INTERVAL_S = 15.0

# Grace period between SIGTERM / SIGINT arrival and the FastAPI
# app exiting. Long enough to let a well-behaved harness flush
# pending responses + close the SDK session cleanly; short enough
# that a wedged subprocess doesn't block process-manager release
# (which has its own SIGKILL escalation at
# ``_RELEASE_GRACE_S = 5.0`` in process_manager.py).
_SHUTDOWN_GRACE_S = 4.5

# Timeout for the policy evaluation round-trip (harness → runner →
# Omnigent server → runner → harness). Fail-open on expiry so a stalled
# round-trip doesn't hang the executor indefinitely. Must be ≥
# DEFAULT_POLICY_CLASSIFIER_TIMEOUT (30 s) since PromptPolicy
# classifiers make their own LLM call on the Omnigent server side.
_POLICY_EVAL_TIMEOUT_S = 35.0

# Per-turn IDLE watchdog: max gap WITHOUT progress before a wedged
# ``run_turn`` becomes ``response.failed`` (vs heartbeating forever).
# Every non-heartbeat ``ctx.emit`` resets the deadline (see
# ``_guarded_run_turn``), so a long-but-active turn is never killed.
# Env var name kept for the ops knob; ``<= 0`` disables.
_TURN_IDLE_TIMEOUT_S = float(os.environ.get("HARNESS_TURN_TIMEOUT_S", "240"))

# Absolute per-turn ceiling: a hard cap on TOTAL turn duration, backstop
# to the idle watchdog above. The idle watchdog never trips a turn that
# keeps emitting, so a runaway-but-active loop (e.g. an infinite tool
# loop emitting steadily) needs this. Generous so it never clips a real
# long turn. ``<= 0`` disables. Whichever of (idle, absolute) trips first.
_TURN_ABSOLUTE_TIMEOUT_S = float(os.environ.get("HARNESS_TURN_ABSOLUTE_TIMEOUT_S", "3600"))


@dataclass(frozen=True)
class PolicyVerdictPayload:
    """
    Result of a policy evaluation round-trip.

    Returned by :meth:`TurnContext.evaluate_policy` after the runner
    evaluates the policy on the Omnigent server and sends the verdict back
    as a ``policy_verdict`` inbound event.

    :param action: Proto-style verdict string, e.g.
        ``"POLICY_ACTION_ALLOW"`` or ``"POLICY_ACTION_DENY"``.
    :param reason: Human-readable reason from the policy engine.
        ``None`` on ALLOW.
    :param data: Optional data dict from content-rewriting
        policies. ``None`` when the policy does not rewrite.
    """

    action: str
    reason: str | None = None
    data: dict[str, Any] | None = None


# ── Inbound event schemas (POST /v1/sessions/{id}/events) ─────────
#
# The session-keyed surface collapses the four turn-keyed routes
# (start turn / cancel / PATCH tool_results / elicitation reply)
# into a single ``POST /v1/sessions/{conversation_id}/events``
# endpoint discriminated by the body's ``type`` field. Wire shapes
# match ``designs/session_rearchitecture.md`` §3 "Event types and
# direction" — the downward (client → server → runner → harness)
# block. Carrying these as a Pydantic discriminated union gives
# fail-loud rejection of unknown types (422) and per-variant field
# validation in one place.


class MessageEvent(BaseModel):
    """
    Downward ``message`` event — start a new turn or steer an in-flight one.

    A ``message`` arriving while no turn is in flight starts a fresh
    turn (the scaffold allocates a ``response_id`` and runs
    ``run_turn``). A ``message`` arriving while a turn is in flight
    is enqueued onto that turn's injection queue (the harness
    delivers it to ``run_turn`` via :meth:`TurnContext.next_injection`).
    The harness has at most one in-flight turn per conversation, so
    no explicit turn id is needed on the wire.

    Extra fields are accepted (``model_config = extra="allow"``) so
    the runner can pass through optional knobs that
    :class:`CreateResponseRequest` understands (``instructions``,
    ``conversation``, ``reasoning``, ``context_management``,
    ``tools``, ignored-but-tolerated controls like ``temperature``)
    without forcing this schema to mirror every field individually.

    :param type: Event discriminator; always ``"message"``.
    :param role: Always ``"user"`` for downward messages — assistant
        messages flow upward via the runner→server persistence
        channel, not through this endpoint. Constrained at the
        type level so a stray ``role: "assistant"`` POST fails 422
        rather than silently starting a turn with bad provenance.
    :param content: The message body. Either a plain string
        shorthand (converted to a single ``input_text`` block on
        the way to ``run_turn``) or a list of content blocks,
        e.g. ``[{"type": "input_text", "text": "Hello"}]``.
    :param model: Agent name to invoke, e.g. ``"research-agent"``.
        Threaded into the synthesized :class:`CreateResponseRequest`
        so the initial / terminal :class:`ResponseObject` carries
        a real model identifier (no empty-string sentinel per the
        project's anti-pattern checklist).
    :param previous_response_id: Optional id of the prior response
        in the conversation thread. The scaffold today uses a
        match against the in-flight ``response_id`` as the
        injection signal; passing it here keeps that path open
        during the migration window. Spec long-term: any
        ``message`` while a turn is in flight is steering, no
        explicit correlation id needed — once all callers stop
        sending it, this field can be dropped.
    """

    type: Literal["message"]
    role: Literal["user"]
    content: str | list[dict[str, Any]]
    model: str
    previous_response_id: str | None = None
    # Allow runner-side passthrough of CreateResponseRequest fields
    # (instructions, conversation, reasoning, context_management,
    # tools, plus the ignored-but-tolerated controls). Forwarded
    # verbatim into the synthesized CreateResponseRequest by
    # :meth:`MessageEvent.to_create_request`.
    model_config = ConfigDict(extra="allow")

    def to_create_request(self) -> CreateResponseRequest:
        """
        Synthesize a :class:`CreateResponseRequest` from this event.

        Maps ``content`` → ``input`` and forwards every extra field
        verbatim. Used by the scaffold's ``/events`` handler to
        adapt the wire shape onto :meth:`HarnessApp._start_or_inject_turn`.

        :returns: A populated :class:`CreateResponseRequest` ready
            to hand off to :meth:`HarnessApp._start_or_inject_turn`.
        """
        # ``model_dump`` includes extras; rename ``content`` →
        # ``input`` and drop the discriminator + role fields the
        # legacy schema doesn't carry.
        payload = self.model_dump()
        payload.pop("type", None)
        payload.pop("role", None)
        payload["input"] = payload.pop("content")
        return CreateResponseRequest(**payload)


class InterruptEvent(BaseModel):
    """
    Downward ``interrupt`` event — cancel the in-flight turn.

    The harness has at most one in-flight turn per conversation
    so no turn id is needed on the wire — the scaffold cancels
    every entry in :attr:`HarnessApp._in_flight`. If no turn is
    in flight, the route 404s — fail loud rather than silently
    accept a no-op cancel so a stray interrupt after a turn ended
    surfaces as an obvious operator error.

    :param type: Event discriminator; always ``"interrupt"``.
    """

    type: Literal["interrupt"]


class ToolResultEvent(BaseModel):
    """
    Downward ``tool_result`` event — deliver a server-dispatched
    tool's output back to the parked turn.

    Resolves the parked Future on the in-flight turn whose
    ``call_id`` matches. Stale ids silently no-op — submit
    results, apply what you can — so benign races (turn
    cancelled mid-event, Future already resolved on a different
    path) don't surface as hard failures upstream.

    :param type: Event discriminator; always ``"tool_result"``.
    :param call_id: The tool call id that this result corresponds
        to, e.g. ``"call_abc123"``. Must match an outstanding
        :meth:`TurnContext.dispatch_tool` Future.
    :param output: The tool's stringified output, e.g.
        ``'["paper1.pdf"]'``.
    """

    type: Literal["tool_result"]
    call_id: str
    output: str


class ApprovalEvent(BaseModel):
    """
    Downward ``approval`` event — reply to an outstanding
    elicitation.

    Resolves the parked Future on whichever in-flight turn
    registered the elicitation. Single-shot — an unknown
    ``elicitation_id`` is a hard 404 because the URL path's
    correlation token must match an outstanding elicitation.

    Wire shape mirrors MCP's ``elicitation/create`` response per
    Principle 8 — same ``action`` + ``content`` fields as
    :class:`ElicitationResult`.

    :param type: Event discriminator; always ``"approval"``.
    :param elicitation_id: Correlation id from the prior
        ``approval_required`` upward event,
        e.g. ``"elicit_abc123"``.
    :param action: User action per MCP semantics.
        ``"accept"`` = approved, ``"decline"`` = explicit refusal,
        ``"cancel"`` = dismissed.
    :param content: Optional form data when ``action == "accept"``
        and the prompt requested fields. Values restricted to JSON
        scalars and string lists, mirroring MCP.
    """

    type: Literal["approval"]
    elicitation_id: str
    action: Literal["accept", "decline", "cancel"]
    content: dict[str, str | int | float | bool | list[str] | None] | None = None

    def to_elicitation_result(self) -> ElicitationResult:
        """
        Adapt this event onto the legacy :class:`ElicitationResult`.

        :returns: An :class:`ElicitationResult` with the same
            ``action`` / ``content`` so
            :meth:`HarnessApp._resolve_elicitation` can resolve
            the parked Future.
        """
        return ElicitationResult(action=self.action, content=self.content)


class PolicyVerdictEvent(BaseModel):
    """
    Downward ``policy_verdict`` event — deliver a policy evaluation
    result back to the parked turn.

    Resolves the parked Future on whichever in-flight turn has a
    matching ``evaluation_id``. Stale ids silently no-op — same
    semantics as :class:`ToolResultEvent`.

    :param type: Event discriminator; always ``"policy_verdict"``.
    :param evaluation_id: Correlation id from the prior
        ``policy_evaluation.requested`` upward event,
        e.g. ``"poleval_abc123"``.
    :param action: Proto-style verdict string, e.g.
        ``"POLICY_ACTION_ALLOW"`` or ``"POLICY_ACTION_DENY"``.
    :param reason: Human-readable reason from the policy engine,
        e.g. ``"Denied by cost-limit policy"``. ``None`` on ALLOW.
    :param data: Optional data dict from content-rewriting
        policies. ``None`` when the policy does not rewrite.
    """

    type: Literal["policy_verdict"]
    evaluation_id: str
    action: str
    reason: str | None = None
    data: dict[str, Any] | None = None


# Discriminated union of every downward event the harness accepts on
# ``POST /v1/sessions/{conversation_id}/events``. FastAPI / Pydantic
# v2 dispatches by the ``type`` field at request-validation time;
# unknown values raise 422 (fail-loud per
# ``designs/DESIGN_PRINCIPLES.md``).
InboundEventRequest = Annotated[
    MessageEvent | InterruptEvent | ToolResultEvent | ApprovalEvent | PolicyVerdictEvent,
    Field(discriminator="type"),
]


class TurnContext:
    """
    Per-turn interaction surface the scaffold hands to ``run_turn``.

    The subclass uses ``ctx`` to:

    - Push SSE events upstream (``ctx.emit(event)``).
    - Park on a tool dispatch (``await ctx.dispatch_tool(...)``).
    - Park on an elicitation (``await ctx.elicit(...)``).
    - Poll for cancellation (``ctx.cancelled.is_set()``).
    - Receive in-band steering / async-completion injections
      (``await ctx.next_injection(timeout=...)``).

    All state is per-turn — a fresh ``TurnContext`` is constructed
    for each fresh ``message`` event that starts a turn. Cross-turn
    state belongs on the harness subclass instance (Layer 1 in the
    in-memory state hierarchy).

    :param response_id: Server-allocated id for this turn,
        e.g. ``"resp_abc123"``. Surfaced on the SSE
        ``response.created`` envelope so Omnigent can correlate
        replays / heartbeat-event-seq tracking.
    :param event_queue: The :class:`asyncio.Queue` the SSE
        streaming response reads from. ``ctx.emit`` puts
        events onto this queue; the streaming response
        formats and yields them.
    :param cancelled: Set by the cancel route handler. The
        subclass should poll this between LLM iterations / native
        tool calls so a cancel arriving mid-LLM-call interrupts
        promptly.
    """

    def __init__(
        self,
        response_id: str,
        event_queue: asyncio.Queue[HarnessStreamEvent | None],
        cancelled: asyncio.Event,
    ) -> None:
        self.response_id = response_id
        self._event_queue = event_queue
        self.cancelled = cancelled
        # Layer 3 per-tool-dispatch state: ``call_id`` →
        # Future[ToolResult-output-string]. Populated by
        # ``dispatch_tool``; resolved by the ``tool_result``
        # /events handler.
        self._pending_tool_calls: dict[str, asyncio.Future[str]] = {}
        # Per-turn elicitation state: ``elicitation_id`` →
        # Future[ElicitationResult]. Populated by ``elicit``;
        # resolved by the ``approval`` /events handler.
        self._pending_elicitations: dict[str, asyncio.Future[ElicitationResult]] = {}
        # Layer 3 per-policy-evaluation state:
        # ``evaluation_id`` → Future[PolicyVerdictPayload].
        # Populated by ``evaluate_policy``; resolved by the
        # ``policy_verdict`` /events handler.
        self._pending_policy_evaluations: dict[str, asyncio.Future[PolicyVerdictPayload]] = {}
        # In-band injections (steering, async completions) the
        # scaffold's ``message`` /events handler pushes here when
        # ``previous_response_id`` matches this turn. Subclass
        # consumes via ``next_injection``.
        self._injection_queue: asyncio.Queue[CreateResponseRequest] = asyncio.Queue()
        # Provider-reported token usage for this turn, set by the
        # subclass (e.g. ExecutorAdapter) when it observes a
        # TurnComplete with usage data. Read by _build_terminal_event
        # to populate the usage field on the response.completed SSE.
        # ``None`` when the inner executor does not report usage.
        self.provider_usage: dict[str, Any] | None = None
        # Idle-watchdog reset hook. ``_guarded_run_turn`` sets this to
        # push the per-turn idle deadline forward on each real progress
        # event so a long-but-active turn isn't killed mid-turn.
        # ``None`` disables it (watchdog off, or outside a guarded run).
        self._reset_idle_watchdog: Callable[[], None] | None = None

    def emit(self, event: HarnessStreamEvent) -> None:
        """
        Push an SSE event upstream.

        Non-blocking. The event lands on the per-turn queue the
        streaming response is consuming. Producers leave
        ``sequence_number`` unset; the streaming wrapper
        assigns it monotonically.

        :param event: A typed event from
            :data:`omnigent.server.schemas.ServerStreamEvent`,
            e.g.
            ``OutputTextDeltaEvent(type="response.output_text.delta",
            delta="hi")``.
        """
        # Treat any non-heartbeat event as progress and push the idle
        # watchdog deadline forward. Heartbeats are keep-alive, NOT
        # progress — letting them reset the deadline would defeat the
        # watchdog (a wedged turn's 15s heartbeats would keep it alive
        # forever).
        if self._reset_idle_watchdog is not None and not isinstance(event, HeartbeatEvent):
            self._reset_idle_watchdog()
        self._event_queue.put_nowait(event)

    async def dispatch_tool(self, call_id: str, name: str, arguments: str, agent: str) -> str:
        """
        Emit a server-dispatched tool call and park until the result.

        Surfaces a ``response.output_item.done`` carrying a
        ``function_call`` item with ``status: "action_required"``;
        the scaffold's ``tool_result`` /events handler resolves
        the parked Future when Omnigent delivers an event carrying the
        matching ``call_id``. See §How Omnigent resolves action_required
        tool calls in the design doc.

        :param call_id: Unique identifier for this tool call,
            e.g. ``"call_abc123"``. Omnigent echoes it back in the
            ``tool_result`` event so the scaffold can route the
            result.
        :param name: Tool name the LLM called, e.g.
            ``"search.web"``.
        :param arguments: JSON-encoded argument string, e.g.
            ``'{"q": "foo"}'``.
        :param agent: Agent name that invoked the tool — required
            on the function_call item per
            :class:`omnigent.entities.conversation.FunctionCallData`.
        :returns: The tool's output string from
            :class:`omnigent.server.schemas.ToolResult`.
        :raises asyncio.CancelledError: If the turn is cancelled
            (an ``interrupt`` event cancels every pending Future
            on the in-flight :class:`TurnContext`).
        """
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_tool_calls[call_id] = future
        # Build the function_call item shape that AP's resolver
        # expects (per omnigent/entities/conversation.py:FunctionCallData
        # plus the API-shape common fields ``id``, ``type``,
        # ``status``).
        item: dict[str, Any] = {
            "id": f"fc_{uuid.uuid4().hex[:12]}",
            "type": "function_call",
            "status": "action_required",
            "name": name,
            "arguments": arguments,
            "call_id": call_id,
            "agent": agent,
        }
        self.emit(OutputItemDoneEvent(type="response.output_item.done", item=item))
        try:
            result = await future
            item["status"] = "completed"
            self.emit(OutputItemDoneEvent(type="response.output_item.done", item=item))
            self.emit(
                OutputItemDoneEvent(
                    type="response.output_item.done",
                    item={
                        "id": f"fco_{uuid.uuid4().hex[:12]}",
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": cap_tool_output(result),
                        "arguments": arguments,
                        "status": "completed",
                    },
                )
            )
            return result
        finally:
            # Always remove — whether the Future resolved cleanly
            # or was cancelled, holding it in the dict would
            # leak memory across turns.
            self._pending_tool_calls.pop(call_id, None)

    async def elicit(
        self, elicitation_id: str, params: ElicitationRequestParams
    ) -> ElicitationResult:
        """
        Emit an elicitation request and park until the reply.

        Wire shape adopts MCP's ``elicitation/create`` per
        Principle 8. The scaffold's ``approval`` /events handler
        resolves the parked Future when upstream sends the reply.
        See §Elicitation in the design doc.

        :param elicitation_id: Unique correlation id for this
            elicitation, e.g. ``"elicit_abc123"``. Must appear
            in the URL of the upstream reply.
        :param params: MCP-shaped params block (mode, message,
            requestedSchema / url, plus AP-specific extensions).
        :returns: The MCP-shaped
            :class:`ElicitationResult` reply.
        :raises asyncio.CancelledError: If the turn is cancelled.
        """
        future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
        self._pending_elicitations[elicitation_id] = future
        self.emit(
            ElicitationRequestEvent(
                type="response.elicitation_request",
                elicitation_id=elicitation_id,
                params=params,
            )
        )
        try:
            return await future
        finally:
            self._pending_elicitations.pop(elicitation_id, None)

    async def next_injection(self, timeout: float | None = None) -> CreateResponseRequest | None:
        """
        Wait for an in-band steering / async-completion injection.

        Blocks until either a ``message`` event with
        ``previous_response_id == self.response_id`` arrives (the
        scaffold's /events handler enqueues it here) or the timeout
        elapses. Subclasses that want to react to mid-turn
        injections (e.g., a sub-agent OOB return delivered as a
        ``function_call_output`` item) call this in a poll loop.

        :param timeout: Max seconds to wait, or ``None`` to wait
            indefinitely. Subclasses commonly use a short timeout
            (~1s) inside the LLM-tool loop so injections get
            picked up between iterations without blocking.
        :returns: The injected request body (whose ``input`` field
            carries the new items to fold into the running turn),
            or ``None`` on timeout.
        """
        try:
            if timeout is None:
                return await self._injection_queue.get()
            return await asyncio.wait_for(self._injection_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def _complete_tool(self, call_id: str, output: str) -> bool:
        """
        Internal: resolve a pending tool-call Future from a
        ``tool_result`` event.

        :param call_id: The call id from the event body.
        :param output: The tool's result output string.
        :returns: ``True`` if a pending call was resolved, ``False``
            if the id didn't match anything outstanding. The
            ``tool_result`` handler ignores the False case (stale
            id = silent no-op).
        """
        future = self._pending_tool_calls.get(call_id)
        if future is None or future.done():
            return False
        future.set_result(output)
        return True

    def _complete_elicitation(self, elicitation_id: str, result: ElicitationResult) -> bool:
        """
        Internal: resolve a pending elicitation Future.

        :param elicitation_id: The id from the URL path.
        :param result: The MCP-shaped reply body.
        :returns: ``True`` if a pending elicitation was resolved,
            ``False`` if the id didn't match anything outstanding
            (the route returns 404 in that case).
        """
        future = self._pending_elicitations.get(elicitation_id)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    async def evaluate_policy(
        self, evaluation_id: str, phase: str, data: dict[str, Any]
    ) -> PolicyVerdictPayload:
        """
        Emit a policy evaluation request and park until the verdict.

        Surfaces a ``policy_evaluation.requested`` SSE event; the
        runner intercepts it, evaluates the policy via the AP
        server's ``POST /sessions/{id}/policies/evaluate``, and
        posts the verdict back as a ``policy_verdict`` inbound
        event. The scaffold's event handler resolves the parked
        Future when the verdict arrives.

        :param evaluation_id: Unique correlation id, e.g.
            ``"poleval_abc123"``. The runner echoes it back in the
            verdict event.
        :param phase: Proto-style phase string, e.g.
            ``"PHASE_LLM_REQUEST"`` or ``"PHASE_LLM_RESPONSE"``.
        :param data: Event data dict for the policy engine, e.g.
            ``{"model": "gpt-4o", "messages_count": 42}``.
        :returns: The verdict payload from the Omnigent server.
        :raises asyncio.CancelledError: If the turn is cancelled.
        """
        future: asyncio.Future[PolicyVerdictPayload] = asyncio.get_running_loop().create_future()
        self._pending_policy_evaluations[evaluation_id] = future
        self.emit(
            PolicyEvaluationRequestEvent(
                type="policy_evaluation.requested",
                evaluation_id=evaluation_id,
                phase=phase,
                data=data,
            )
        )
        try:
            return await asyncio.wait_for(future, timeout=_POLICY_EVAL_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Phase-aware default: advisory LLM phases fail OPEN so a missing
            # verdict never hangs the turn, but TOOL_CALL / TOOL_RESULT is the
            # authoritative gate for connector-native tools and fails CLOSED.
            _fail_closed = phase in ("PHASE_TOOL_CALL", "PHASE_TOOL_RESULT")
            _action = "POLICY_ACTION_DENY" if _fail_closed else "POLICY_ACTION_ALLOW"
            _logger.warning(
                "Policy evaluation %s timed out after %ds; defaulting to %s",
                evaluation_id,
                _POLICY_EVAL_TIMEOUT_S,
                _action,
            )
            return PolicyVerdictPayload(
                action=_action,
                reason=(
                    f"Policy evaluation timed out; failing closed for {phase}."
                    if _fail_closed
                    else None
                ),
            )
        finally:
            self._pending_policy_evaluations.pop(evaluation_id, None)

    def _complete_policy_evaluation(
        self, evaluation_id: str, verdict: PolicyVerdictPayload
    ) -> bool:
        """
        Internal: resolve a pending policy-evaluation Future.

        :param evaluation_id: Correlation id from the verdict event.
        :param verdict: The policy verdict payload.
        :returns: ``True`` if a pending evaluation was resolved,
            ``False`` if the id didn't match (stale = silent no-op).
        """
        future = self._pending_policy_evaluations.get(evaluation_id)
        if future is None or future.done():
            return False
        future.set_result(verdict)
        return True

    def _push_injection(self, request: CreateResponseRequest) -> None:
        """
        Internal: push an injection request onto the queue.

        :param request: The decoded ``message`` /events body whose
            ``previous_response_id`` matched this turn.
        """
        self._injection_queue.put_nowait(request)

    def _cancel_pending(self) -> None:
        """
        Internal: cancel every pending tool / elicitation Future.

        Called by the ``interrupt`` handler so subclass coroutines
        parked on ``dispatch_tool`` / ``elicit`` unblock with
        :class:`asyncio.CancelledError`. The subclass's
        ``run_turn`` then unwinds to its termination path.
        """
        for tool_future in self._pending_tool_calls.values():
            if not tool_future.done():
                tool_future.cancel()
        for elicitation_future in self._pending_elicitations.values():
            if not elicitation_future.done():
                elicitation_future.cancel()
        for policy_future in self._pending_policy_evaluations.values():
            if not policy_future.done():
                policy_future.cancel()


class HarnessApp:
    """
    Base class for harness wraps. Subclasses implement
    :meth:`run_turn`.

    Use the class as a context manager around a harness wrap's
    ``create_app()`` factory:

    .. code-block:: python

        class MyHarnessApp(HarnessApp):
            async def run_turn(
                self, request: CreateResponseRequest, ctx: TurnContext
            ) -> None:
                ctx.emit(OutputTextDeltaEvent(
                    type="response.output_text.delta", delta="hi"
                ))

        def create_app() -> FastAPI:
            return MyHarnessApp().build()

    The scaffold owns:

    - The FastAPI app (built by :meth:`build`).
    - Per-turn :class:`TurnContext` allocation + lifecycle.
    - SSE streaming of events the subclass emits.
    - Heartbeat emission at :data:`_HEARTBEAT_INTERVAL_S` cadence.
    - Discriminated /events handler that resolves the right
      pending Futures on the right :class:`TurnContext` for
      ``tool_result``, ``approval``, and ``interrupt`` variants.
    - In-band injection routing: a ``message`` event with
      ``previous_response_id`` matching the in-flight turn
      enqueues onto that turn's injection queue rather than
      starting a new turn.
    - Graceful SIGTERM / SIGINT shutdown.

    The subclass owns:

    - :meth:`run_turn` — the LLM-tool loop, using ``ctx`` to
      interact upstream.
    - Layer 1 in-memory state (SDK / CLI process handle, last-known
      history for reconciliation) on the subclass instance.
    """

    def __init__(self) -> None:
        # In-flight turns keyed by ``response_id``. Single-entry in
        # practice (AP enforces one in-flight turn per conversation
        # at its REST boundary), but stored as a dict so the route
        # handlers can look up by id without assuming uniqueness.
        self._in_flight: dict[str, TurnContext] = {}
        # The currently active turn context, or ``None`` when idle.
        # Set under ``_lock`` when a turn starts, cleared
        # synchronously when the streaming generator finishes
        # (before async teardown). Used by sessions-native
        # injection: a message arriving while this is set is
        # steering, not a new turn.
        self._active_turn_ctx: TurnContext | None = None
        # Lock for ``_in_flight`` / ``_active_turn_ctx`` mutations —
        # route handlers run concurrently in FastAPI's event loop.
        self._lock = asyncio.Lock()
        # Set by graceful-shutdown signal handlers; checked by the
        # ``message`` /events handler to refuse new turns once
        # shutdown started.
        self._shutting_down = asyncio.Event()

    async def on_shutdown(self) -> None:
        """
        Subclass hook invoked during lifespan teardown.

        Called from ``_lifespan``'s ``finally`` block after
        in-flight turns have been cancelled but before the
        drain grace period. Subclasses (e.g.
        :class:`ExecutorAdapter`) override this to close inner
        executor resources, SDK sessions, and child processes
        that would otherwise leak on process exit.

        The base implementation is a no-op.
        """

    async def run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        """
        Per-harness turn execution.

        Subclasses MUST override. The scaffold has already wired
        up the SSE response, allocated ``response_id``, and
        constructed ``ctx``. The subclass's job is to run the
        LLM-tool loop using ``ctx.emit`` for events,
        ``ctx.dispatch_tool`` / ``ctx.elicit`` for round-trips,
        ``ctx.cancelled`` for cancellation polling, and
        ``ctx.next_injection`` for steering / async completions.

        On normal completion, the scaffold emits a
        :class:`CompletedEvent` automatically once the subclass
        returns. Subclasses do NOT need to emit the terminal
        event themselves.

        :param request: The decoded
            :class:`CreateResponseRequest` body.
        :param ctx: Per-turn :class:`TurnContext` providing the
            interaction surface.
        """
        raise NotImplementedError

    def _build_error_detail(self, exception: BaseException) -> Any:
        """
        Translate a ``run_turn`` exception into an :class:`ErrorDetail`.

        Default implementation: uses the exception class name as
        the error code (e.g. ``"RuntimeError"``, ``"ValueError"``)
        and ``str(exception)`` as the message. AP's retryable-error
        allowlist at
        :data:`omnigent.runtime.harnesses._client_executor._RETRYABLE_HARNESS_ERROR_CODES`
        uses semantic names (``"rate_limit_exceeded"``,
        ``"timeout"``, etc.), so the default mapping makes every
        failure non-retryable — safe but coarse.

        Per-harness subclasses (e.g.
        :class:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter`)
        override this to translate SDK-specific exception types
        onto the semantic allowlist so the retry-classification
        promise from §Error envelopes / step 5j actually fires
        for known retryable failures.

        :param exception: The exception :meth:`run_turn` raised.
        :returns: An :class:`ErrorDetail` whose ``code`` field
            ideally matches one of the contract-recognized codes
            so AP-side retry decisions can act on it.
        """
        from omnigent.server.schemas import ErrorDetail

        return ErrorDetail(code=type(exception).__name__, message=str(exception))

    def build(self) -> FastAPI:
        """
        Build the FastAPI app exposing the harness API subset.

        Mounts:

        - ``GET /health`` (liveness probe — at root, NOT /v1).
        - ``POST /v1/sessions/{conversation_id}/events`` —
          discriminated downward-event endpoint per
          ``designs/session_rearchitecture.md`` §3. The body's
          ``type`` field selects the variant (``message`` /
          ``interrupt`` / ``tool_result`` / ``approval``); FastAPI
          / Pydantic rejects unknown values with 422.

        Harness-specific shutdown runs from the FastAPI lifespan
        teardown path after uvicorn receives SIGTERM/SIGINT, so
        in-flight turns are cancelled gracefully without competing
        with uvicorn's process-level signal handlers.

        :returns: A configured :class:`FastAPI` instance ready to
            be served by ``omnigent.runtime.harnesses._runner``.
        """
        app = FastAPI(title="omnigent-harness", lifespan=self._lifespan)
        # FastAPI / Starlette types add_exception_handler narrowly
        # (Callable[[Request, Exception], ...]) — the OmnigentError
        # subclass annotation is what we actually want at runtime, but
        # the static type doesn't accept it. Cast at the boundary.
        app.add_exception_handler(OmnigentError, _handle_omnigent_error)  # type: ignore[arg-type]
        # Health probe lives at root (matches AP's app.py:125).
        app.add_api_route("/health", _health, methods=["GET"])
        app.include_router(self._build_v1_router(), prefix="/v1")
        return app

    @contextlib.asynccontextmanager
    async def _lifespan(self, _app: FastAPI) -> AsyncIterator[None]:
        """
        FastAPI lifespan: cancel, close, and drain on exit.

        Uvicorn owns process-level SIGTERM/SIGINT handling. The
        scaffold intentionally does not install competing signal
        handlers here: doing so can prevent uvicorn from setting its
        own shutdown flag, leaving the harness runner alive after a
        plain ``SIGTERM``. Instead, uvicorn enters lifespan teardown
        and this ``finally`` block performs harness-specific cleanup:
        cancel in-flight Futures, invoke subclass shutdown hooks, and
        drain briefly for graceful completion.

        :param _app: The FastAPI app (unused — required by the
            lifespan protocol).
        """
        try:
            yield
        finally:
            self._on_shutdown_signal()
            await self.on_shutdown()
            await self._drain_for_shutdown()

    def _on_shutdown_signal(self) -> None:
        """
        Signal handler: kick off graceful shutdown.

        Marks the scaffold as shutting down (so new ``message``
        events get refused) and cancels every
        in-flight turn's pending Futures. The actual app exit
        happens after :meth:`_drain_for_shutdown` runs.
        """
        if self._shutting_down.is_set():
            # Second signal — escalate immediately. uvicorn's
            # default SIGTERM handler will take over.
            return
        self._shutting_down.set()
        for ctx in self._in_flight.values():
            ctx.cancelled.set()
            ctx._cancel_pending()

    async def _drain_for_shutdown(self) -> None:
        """
        Wait briefly for in-flight turns to finalize on shutdown.

        Bounded by :data:`_SHUTDOWN_GRACE_S`; if turns haven't
        cleared by then the process exits and AP's workflow
        handles the disconnect via the standard
        ``executor_died`` retry path.
        """
        deadline = time.monotonic() + _SHUTDOWN_GRACE_S
        while self._in_flight and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    def _build_v1_router(self) -> APIRouter:
        """
        Build the ``/v1`` router with the route handlers.

        The router is bound to a bound method on ``self`` so the
        handler closes over the scaffold instance — no FastAPI
        dependency-injection plumbing needed.

        Mounts the session-keyed surface per
        ``designs/session_rearchitecture.md`` §3 "Endpoints":

        - ``POST /v1/sessions/{conversation_id}/events`` —
          downward events (``message``, ``interrupt``,
          ``tool_result``, ``approval``). The body's ``type``
          field discriminates the variant; FastAPI / Pydantic
          rejects unknown values with 422.

        The handler validates the path's ``conversation_id``
        against :attr:`fastapi.applications.FastAPI.state.conversation_id`
        (stashed by the runner per ``_runner.py``) and 404s on
        mismatch.

        :returns: A configured :class:`APIRouter`.
        """
        router = APIRouter()
        router.add_api_route(
            "/sessions/{conversation_id}/events",
            self._post_session_event,
            methods=["POST"],
            # MessageEvent variants stream a turn back as SSE
            # (StreamingResponse) while the other variants return
            # 204; declaring response_model=None disables OpenAPI
            # introspection that would otherwise reject the
            # heterogeneous return type.
            response_model=None,
        )
        return router

    def _check_conversation_id(self, request: Request, conversation_id: str) -> None:
        """
        Validate that a session-keyed URL targets this scaffold's
        conversation.

        The harness scaffold is per-conversation: a single
        subprocess serves exactly one ``conversation_id`` (set on
        ``app.state.conversation_id`` by the runner — see
        ``omnigent/runtime/harnesses/_runner.py``). Any
        session-keyed request whose path does not match is
        addressed at the wrong subprocess; failing with 404 is the
        same shape the Omnigent side uses for unknown conversations
        (per ``omnigent/server/routes/sessions.py``).

        Boot ordering: ``app.state.conversation_id`` is set in
        ``_runner._load_harness_app`` AFTER the FastAPI app is
        constructed but BEFORE uvicorn starts serving requests, so
        any handler that runs has guaranteed access to it. A
        missing attribute means the scaffold is being driven by a
        custom embedder that skipped that step — fail loud with
        500 rather than silently accept any conversation_id.

        :param request: The FastAPI request, used to read
            ``app.state.conversation_id``.
        :param conversation_id: The id from the URL path.
        :raises OmnigentError: 500 if ``app.state.conversation_id``
            is unset (misconfiguration); 404 if the path id does
            not match the scaffold's bound id.
        """
        bound = getattr(request.app.state, "conversation_id", None)
        if bound is None:
            raise OmnigentError(
                "harness scaffold has no conversation_id bound on app.state — "
                "the runner must set app.state.conversation_id before serving",
                code=ErrorCode.INTERNAL_ERROR,
            )
        if bound != conversation_id:
            raise OmnigentError(
                f"conversation {conversation_id!r} not served by this harness "
                f"(bound to {bound!r})",
                code=ErrorCode.NOT_FOUND,
            )

    async def _post_session_event(
        self,
        conversation_id: str,
        body: InboundEventRequest,
        request: Request,
    ) -> StreamingResponse | Response:
        """
        Handle ``POST /v1/sessions/{conversation_id}/events``.

        Single discriminated entry point for every downward event
        (``message``, ``interrupt``, ``tool_result``, ``approval``)
        per ``designs/session_rearchitecture.md`` §3. After
        validating the path's ``conversation_id`` matches the
        scaffold's bound conversation, dispatches by ``body.type``
        onto the matching per-action implementation.

        Dispatch table:

        - :class:`MessageEvent` → :meth:`_start_or_inject_turn`
          after adapting to :class:`CreateResponseRequest`
          (streaming SSE for new turns, 204 for in-band
          injections).
        - :class:`InterruptEvent` → cancel every in-flight turn
          (single-entry in practice; 404 if none in flight).
        - :class:`ToolResultEvent` → resolve the parked Future
          on whichever in-flight turn has the matching
          ``call_id``; stale ids silently no-op.
        - :class:`ApprovalEvent` → :meth:`_resolve_elicitation`
          after adapting to :class:`ElicitationResult` (404 on
          unknown elicitation_id — single-shot correlation).

        Unknown ``type`` values fail at request validation with
        422 (Pydantic discriminator), per ``designs/DESIGN_PRINCIPLES.md``
        fail-loud.

        :param conversation_id: AP-allocated conversation id from
            the URL, e.g. ``"conv_abc123"``. Must match
            ``app.state.conversation_id``.
        :param body: Decoded discriminated-union event body.
        :param request: The FastAPI request, used to validate
            ``conversation_id``.
        :returns: A :class:`StreamingResponse` for a fresh
            ``message`` event that starts a turn, or a 204
            :class:`Response` for every other case (injection,
            interrupt, tool_result, approval).
        :raises OmnigentError: 404 on conversation_id mismatch,
            unknown elicitation_id, or interrupt with no
            in-flight turn; 503 on shutdown for fresh turns.
        """
        self._check_conversation_id(request, conversation_id)
        if isinstance(body, MessageEvent):
            return await self._start_or_inject_turn(body.to_create_request())
        if isinstance(body, InterruptEvent):
            return await self._handle_interrupt_event()
        if isinstance(body, ToolResultEvent):
            return await self._handle_tool_result_event(body)
        if isinstance(body, ApprovalEvent):
            return await self._resolve_elicitation(
                body.elicitation_id, body.to_elicitation_result()
            )
        if isinstance(body, PolicyVerdictEvent):
            return await self._handle_policy_verdict_event(body)
        # Pydantic's discriminated-union validator should reject
        # unknown variants before we reach this branch; if it ever
        # falls through, fail loud rather than silently no-op.
        raise OmnigentError(
            f"unsupported inbound event type {type(body).__name__!r}",
            code=ErrorCode.INVALID_INPUT,
        )

    async def _handle_interrupt_event(self) -> Response:
        """
        Apply an :class:`InterruptEvent` to the in-flight turn.

        The harness has at most one in-flight turn per conversation
        in practice, but the registry is a dict so this iterates
        defensively. If no turn is in flight, 404s — fail loud
        rather than silently no-op so a stray interrupt arriving
        after a turn ended surfaces as an obvious operator error.

        :returns: 204 on success.
        :raises OmnigentError: 404 if no turn is in flight.
        """
        if not self._in_flight:
            raise OmnigentError(
                "no in-flight turn to interrupt",
                code=ErrorCode.NOT_FOUND,
            )
        for ctx in self._in_flight.values():
            ctx.cancelled.set()
            ctx._cancel_pending()
        # Drop the cancelled turn as the inject target so the next message
        # starts a fresh turn (rebuilds with the marker), not into the dying one
        # — otherwise it resumes the abandoned turn and the agent runs one behind.
        async with self._lock:
            self._active_turn_ctx = None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def _handle_tool_result_event(self, body: ToolResultEvent) -> Response:
        """
        Apply a :class:`ToolResultEvent` to whichever in-flight
        turn has a matching parked tool-call Future.

        Loose-by-default semantics — stale ``call_id`` entries
        silently no-op. Benign races (turn cancelled mid-event,
        Future already resolved on a different path) MUST NOT
        surface as hard failures upstream; the streaming response
        is the source of truth for whether the result actually
        landed.

        :param body: The decoded :class:`ToolResultEvent`.
        :returns: 204 No Content.
        """
        for ctx in self._in_flight.values():
            if ctx._complete_tool(body.call_id, body.output):
                break
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def _handle_policy_verdict_event(self, body: PolicyVerdictEvent) -> Response:
        """
        Apply a :class:`PolicyVerdictEvent` to whichever in-flight
        turn has a matching parked policy-evaluation Future.

        Loose semantics — stale ``evaluation_id`` entries silently
        no-op, same as :meth:`_handle_tool_result_event`.

        :param body: The decoded :class:`PolicyVerdictEvent`.
        :returns: 204 No Content.
        """
        verdict = PolicyVerdictPayload(
            action=body.action,
            reason=body.reason,
            data=body.data,
        )
        for ctx in self._in_flight.values():
            if ctx._complete_policy_evaluation(body.evaluation_id, verdict):
                break
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def _start_or_inject_turn(
        self, request: CreateResponseRequest
    ) -> StreamingResponse | Response:
        """
        Start a new turn or inject into the in-flight one.

        Invoked by :meth:`_post_session_event` for ``message``
        events. Three cases:

        1. ``previous_response_id`` matches an in-flight turn →
           in-band injection: enqueue the request body on that
           turn's injection queue, return 204.
        2. Scaffold is shutting down → refuse with 503.
        3. Otherwise → start a new turn: allocate ``response_id``,
           build a :class:`TurnContext`, register it in
           ``_in_flight``, and return a streaming SSE response
           that runs ``run_turn`` to completion.

        :param request: The decoded request body.
        :returns: Either a :class:`StreamingResponse` for the new
            turn or a 204 :class:`Response` for an in-band
            injection.
        :raises OmnigentError: 503 on shutdown.
        """
        if (
            request.previous_response_id is not None
            and request.previous_response_id in self._in_flight
            and not self._in_flight[request.previous_response_id].cancelled.is_set()
        ):
            ctx = self._in_flight[request.previous_response_id]
            ctx._push_injection(request)
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        async with self._lock:
            # Sessions-native steering: no previous_response_id on
            # the wire, but a turn is actively streaming. Inject.
            # Serialized under _lock so two concurrent requests
            # can't both pass the guard before either sets
            # _active_turn_ctx.
            if (
                request.previous_response_id is None
                and self._active_turn_ctx is not None
                and not self._active_turn_ctx.cancelled.is_set()
            ):
                self._active_turn_ctx._push_injection(request)
                return Response(status_code=status.HTTP_204_NO_CONTENT)

            if self._shutting_down.is_set():
                raise OmnigentError(
                    "harness is shutting down; refusing new turn",
                    code=ErrorCode.CONFLICT,
                )

            response_id = f"resp_{uuid.uuid4().hex[:24]}"
            event_queue: asyncio.Queue[HarnessStreamEvent | None] = asyncio.Queue()
            cancelled = asyncio.Event()
            ctx = TurnContext(
                response_id=response_id,
                event_queue=event_queue,
                cancelled=cancelled,
            )
            self._in_flight[response_id] = ctx
            self._active_turn_ctx = ctx

        return StreamingResponse(
            self._stream_turn(request, ctx),
            media_type="text/event-stream",
        )

    async def _stream_turn(
        self, request: CreateResponseRequest, ctx: TurnContext
    ) -> AsyncIterator[bytes]:
        """
        Drive ``run_turn`` and yield SSE-formatted events.

        Three phases:

        1. Emit the initial response.created + response.in_progress
           frames carrying the freshly-allocated response_id.
        2. Spawn ``run_turn`` + heartbeat as concurrent tasks;
           consume the event queue, assigning monotonic sequence
           numbers, and yield each event as an SSE frame.
        3. On run_turn termination (sentinel from
           ``_guarded_run_turn``), emit the terminal event
           (completed / failed / cancelled) and tear down both
           tasks + the in-flight registry entry.

        :param request: The request body to pass to ``run_turn``.
        :param ctx: The per-turn :class:`TurnContext`.
        :yields: SSE-formatted ``bytes`` ready to ship over the
            HTTP response.
        """
        sequence = 0
        for initial_event in self._initial_envelope_events(
            ctx, model=request.model, start_seq=sequence
        ):
            yield _format_sse_event(initial_event)
            sequence += 1

        run_task = asyncio.create_task(
            self._guarded_run_turn(request, ctx),
            name=f"harness-run-turn:{ctx.response_id}",
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(ctx),
            name=f"harness-heartbeat:{ctx.response_id}",
        )
        # Track the sequence_number of the last non-heartbeat
        # event emitted on this stream so we can stamp the
        # heartbeat's ``last_event_seq`` field at yield time.
        # Per the contract (§Heartbeats), heartbeats carry the
        # *previous* user-visible event's sequence number so
        # consumers can detect dropped events. ``None`` until
        # the first non-heartbeat event lands; the initial
        # response.created / response.in_progress envelope
        # events count, so this populates immediately.
        last_event_seq: int | None = sequence - 1 if sequence > 0 else None
        try:
            while True:
                event = await ctx._event_queue.get()
                if event is None:
                    # Sentinel pushed by ``_guarded_run_turn`` when
                    # ``run_turn`` returns; emit terminal event
                    # below.
                    break
                event.sequence_number = sequence
                if isinstance(event, HeartbeatEvent):
                    # Stamp timing metadata at emit time, not
                    # construction time, so ``server_time`` reflects
                    # when the event actually leaves the queue and
                    # ``last_event_seq`` reflects the actual stream
                    # state at that moment.
                    event.server_time = _utc_now_iso()
                    event.last_event_seq = last_event_seq
                else:
                    last_event_seq = sequence
                sequence += 1
                yield _format_sse_event(event)
            terminal = await self._build_terminal_event(
                ctx, model=request.model, run_task=run_task, sequence=sequence
            )
            # Clear before yielding the terminal event so the next
            # request (continuation turn) sees _active_turn_ctx as
            # None and starts a new turn instead of injecting into
            # this completed one.
            async with self._lock:
                if self._active_turn_ctx is ctx:
                    self._active_turn_ctx = None
            yield _format_sse_event(terminal)
        finally:
            await self._teardown_turn(ctx, run_task, heartbeat_task)

    def _initial_envelope_events(
        self, ctx: TurnContext, model: str, start_seq: int
    ) -> list[ServerStreamEvent]:
        """
        Build the response.created + response.in_progress events
        emitted at the start of every turn.

        Matches AP's existing streaming contract — both events
        carry the freshly-allocated :class:`ResponseObject` so
        consumers can correlate subsequent PATCH / cancel /
        elicitation calls back to this turn via the response_id.

        :param ctx: The per-turn context (provides ``response_id``).
        :param model: The agent name from the incoming
            :class:`CreateResponseRequest.model`, e.g.
            ``"research-agent"``. Required because
            :class:`ResponseObject` requires a real ``model`` value
            (no empty-string sentinels per the project's
            anti-pattern checklist).
        :param start_seq: Sequence number to assign to the first
            event; the second is ``start_seq + 1``.
        :returns: A two-event list:
            ``[CreatedEvent, InProgressEvent]``.
        """
        initial_response = ResponseObject(
            id=ctx.response_id,
            status="queued",
            model=model,
            created_at=int(time.time()),
        )
        in_progress_response = initial_response.model_copy(update={"status": "in_progress"})
        return [
            CreatedEvent(
                type="response.created",
                response=initial_response,
                sequence_number=start_seq,
            ),
            InProgressEvent(
                type="response.in_progress",
                response=in_progress_response,
                sequence_number=start_seq + 1,
            ),
        ]

    async def _teardown_turn(
        self,
        ctx: TurnContext,
        run_task: asyncio.Task[None],
        heartbeat_task: asyncio.Task[None],
    ) -> None:
        """
        Cancel the heartbeat + (defensively) the run task, then
        unregister the turn from ``_in_flight``.

        Always called from ``_stream_turn``'s finally block —
        guarantees no asyncio task leaks per turn even on
        cancellation / exception paths.

        :param ctx: The per-turn context (used for the response_id
            registry key).
        :param run_task: The completed (or to-be-cancelled)
            run_turn task.
        :param heartbeat_task: The heartbeat emitter task; always
            still running at this point (it loops forever) and
            needs explicit cancellation.
        """
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        if not run_task.done():
            # Defensive: run_turn might still be parked on a
            # Future the cleanup didn't release. Cancel here as a
            # last resort so the task doesn't leak.
            # ``CancelledError`` is the expected exit; don't
            # blanket-suppress unrelated exceptions because a
            # crashed run_turn at this stage indicates a real bug
            # worth surfacing in the logs.
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
        async with self._lock:
            self._in_flight.pop(ctx.response_id, None)
            if self._active_turn_ctx is ctx:
                self._active_turn_ctx = None

    async def _guarded_run_turn(self, request: CreateResponseRequest, ctx: TurnContext) -> None:
        """
        Wrap ``run_turn`` so its termination always pushes the
        sentinel onto the event queue.

        Without this, an exception or early return in ``run_turn``
        would leave the streaming loop blocked on
        ``event_queue.get()`` forever.

        Enforces two per-turn watchdogs, whichever trips first:

        - IDLE (:data:`_TURN_IDLE_TIMEOUT_S`): each non-heartbeat
          ``ctx.emit`` reschedules its deadline via the
          ``_reset_idle_watchdog`` hook, so a turn that keeps making
          progress (an orchestrator running tests + build + many tool
          calls) is never killed — only one that emits nothing for the
          whole window. This replaces the prior fixed *cumulative* cap,
          which guillotined long-but-healthy turns mid-stream.
        - ABSOLUTE (:data:`_TURN_ABSOLUTE_TIMEOUT_S`): a hard ceiling on
          total duration, never rescheduled. Backstops the idle watchdog
          against a runaway-but-active loop the idle one never sees as
          stuck.

        Either expiry surfaces a wedged/runaway ``run_turn`` as
        ``response.failed``.

        :param request: Forwarded to ``run_turn``.
        :param ctx: Forwarded to ``run_turn``.
        """
        idle_timeout = _TURN_IDLE_TIMEOUT_S
        absolute_timeout = _TURN_ABSOLUTE_TIMEOUT_S
        # ``asyncio.timeout(None)`` is a no-op, so ``<= 0`` disables each.
        idle_wd = asyncio.timeout(idle_timeout if idle_timeout > 0 else None)
        absolute_wd = asyncio.timeout(absolute_timeout if absolute_timeout > 0 else None)
        if idle_timeout > 0:
            loop = asyncio.get_running_loop()

            def _reset() -> None:
                # Push ONLY the idle deadline ``idle_timeout`` s past now
                # (the absolute ceiling is never rescheduled). Called from
                # ``ctx.emit`` during ``run_turn`` (inside the active
                # context), so the reschedule is always valid.
                idle_wd.reschedule(loop.time() + idle_timeout)

            ctx._reset_idle_watchdog = _reset
        try:
            # Absolute outer, idle inner: ``.expired()`` on each tells which
            # ceiling tripped so the error message is accurate.
            async with absolute_wd, idle_wd:
                await self.run_turn(request, ctx)
        except TimeoutError as exc:
            if idle_wd.expired():
                _logger.warning(
                    "run_turn for %s made no progress for %.0fs (idle turn watchdog); "
                    "marking the turn failed",
                    ctx.response_id,
                    idle_timeout,
                )
                raise RuntimeError(
                    f"turn exceeded the {idle_timeout:.0f}s harness idle watchdog "
                    f"(run_turn emitted no events for {idle_timeout:.0f}s; "
                    f"likely a wedged LLM or tool call)"
                ) from exc
            if absolute_wd.expired():
                _logger.warning(
                    "run_turn for %s exceeded the %.0fs absolute turn ceiling; "
                    "marking the turn failed",
                    ctx.response_id,
                    absolute_timeout,
                )
                raise RuntimeError(
                    f"turn exceeded the {absolute_timeout:.0f}s harness absolute watchdog "
                    f"(total turn duration cap; the turn kept emitting but never finished)"
                ) from exc
            # Neither ceiling tripped — an inner ``run_turn`` TimeoutError;
            # pass it through unchanged.
            raise
        except asyncio.CancelledError:
            # Re-raised so the streaming-side ``run_task.exception()``
            # check sees it; the terminal event handling
            # downgrades a cancelled run_turn to a
            # ``response.cancelled`` event.
            raise
        finally:
            # Detach the reset hook before the timeout context unwinds so
            # a stray late ``emit`` can't reschedule a finished timeout.
            ctx._reset_idle_watchdog = None
            # Sentinel that tells ``_stream_turn`` to stop reading
            # the queue and emit the terminal event.
            ctx._event_queue.put_nowait(None)

    async def _heartbeat_loop(self, ctx: TurnContext) -> None:
        """
        Emit ``response.heartbeat`` on the queue every
        :data:`_HEARTBEAT_INTERVAL_S`.

        Cancellation is the normal exit path — the streaming
        wrapper cancels this task when the turn finalizes.

        :param ctx: The per-turn context whose queue receives
            heartbeats.
        """
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            ctx.emit(HeartbeatEvent(type="response.heartbeat"))

    async def _build_terminal_event(
        self,
        ctx: TurnContext,
        model: str,
        run_task: asyncio.Task[None],
        sequence: int,
    ) -> ServerStreamEvent:
        """
        Construct the terminal SSE event after ``run_turn`` returns.

        :param ctx: The per-turn context (used to read
            ``response_id``).
        :param model: The agent name from the incoming
            :class:`CreateResponseRequest.model`. Required so the
            terminal :class:`ResponseObject` carries a real model
            value (no empty-string sentinels per the project's
            anti-pattern checklist).
        :param run_task: The completed (or failed) ``run_turn``
            task whose exception state determines the event type.
        :param sequence: The sequence number to assign to the
            terminal event.
        :returns: A :class:`CompletedEvent`, :class:`FailedEvent`,
            or cancelled-event variant carrying a synthesized
            :class:`ResponseObject`. The synthesized object is
            minimal — AP's persistence path constructs the
            authoritative ResponseObject.
        """
        # ``run_task.exception()`` is ``None`` on clean return, an
        # exception instance otherwise.
        exception = run_task.exception() if run_task.done() and not run_task.cancelled() else None
        cancelled = run_task.cancelled() or ctx.cancelled.is_set()
        if cancelled:
            status_value = "cancelled"
        elif exception is not None:
            status_value = "failed"
        else:
            status_value = "completed"

        # Convert the inner executor's usage dict to a ResponseObject
        # Usage instance so the harness client can read it from the
        # serialized response.completed SSE payload.
        usage: Usage | None = None
        if ctx.provider_usage is not None:
            u = ctx.provider_usage
            usage = Usage(
                input_tokens=u.get("input_tokens") or 0,
                output_tokens=u.get("output_tokens") or 0,
                total_tokens=u.get("total_tokens") or 0,
                context_tokens=u.get("context_tokens"),
                # Carry the cache breakdown through to response.completed
                # so the server-side cost path can price cache reads /
                # writes at their own rates. Anthropic-style executors
                # (e.g. claude-sdk) report these; others omit them (0).
                cache_read_input_tokens=u.get("cache_read_input_tokens") or 0,
                cache_creation_input_tokens=u.get("cache_creation_input_tokens") or 0,
                # Harness-reported model for cost pricing (ResponseObject.model is the agent name).
                model=u.get("model"),
            )
        response = ResponseObject(
            id=ctx.response_id,
            status=status_value,
            model=model,
            created_at=int(time.time()),
            error=None if exception is None else self._build_error_detail(exception),
            usage=usage,
        )
        terminal: ServerStreamEvent
        if status_value == "completed":
            terminal = CompletedEvent(
                type="response.completed", response=response, sequence_number=sequence
            )
        elif status_value == "failed":
            terminal = FailedEvent(
                type="response.failed", response=response, sequence_number=sequence
            )
        else:
            from omnigent.server.schemas import CancelledEvent

            terminal = CancelledEvent(
                type="response.cancelled",
                response=response,
                sequence_number=sequence,
            )
        return terminal

    async def _resolve_elicitation(
        self,
        elicitation_id: str,
        body: ElicitationResult,
    ) -> Response:
        """
        Resolve a parked elicitation Future from an
        :class:`ApprovalEvent`.

        Invoked by :meth:`_post_session_event` for ``approval``
        events. The id MUST belong to an outstanding elicitation
        on one of the in-flight turns; otherwise raises 404.

        :param elicitation_id: Correlation id from the event body.
        :param body: MCP-shape :class:`ElicitationResult` adapted
            from the :class:`ApprovalEvent`.
        :returns: 204 on success.
        :raises OmnigentError: 404 if no pending elicitation
            matches the id (across all in-flight turns).
        """
        for ctx in self._in_flight.values():
            if ctx._complete_elicitation(elicitation_id, body):
                return Response(status_code=status.HTTP_204_NO_CONTENT)
        raise OmnigentError(
            f"no outstanding elicitation {elicitation_id!r}",
            code=ErrorCode.NOT_FOUND,
        )


def _format_sse_event(event: HarnessStreamEvent) -> bytes:
    """
    Serialize a typed event to an SSE wire frame.

    Wire shape: ``event: <name>\\ndata: <json>\\n\\n``.

    :param event: Any variant from
        :data:`omnigent.server.schemas.ServerStreamEvent`.
    :returns: UTF-8-encoded bytes ready to ship over the HTTP
        response.
    """
    payload = event.model_dump_json(exclude_none=True)
    return f"event: {event.type}\ndata: {payload}\n\n".encode()


def _utc_now_iso() -> str:
    """
    Return the current UTC wall-clock as an ISO 8601 string.

    Used for ``HeartbeatEvent.server_time`` so consumers can
    detect clock drift between producer and consumer. Format
    matches the contract example: ``"2026-04-27T15:30:00Z"``
    (trailing ``Z`` for UTC, microseconds elided to keep the
    wire compact).

    :returns: An ISO 8601 timestamp like
        ``"2026-04-27T15:30:00Z"``.
    """
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Note: ``HarnessApp._build_error_detail`` is the canonical
# implementation. Per-harness wraps that need to map SDK-specific
# exceptions (e.g. ``anthropic.RateLimitError``) onto the
# AP-side retryable allowlist override the method on their
# subclass — see
# :meth:`omnigent.runtime.harnesses._executor_adapter.ExecutorAdapter._build_error_detail`.


async def _health() -> dict[str, str]:
    """
    Liveness probe — matches AP's ``/health`` shape.

    :returns: ``{"status": "ok"}``.
    """
    return {"status": "ok"}


async def _handle_omnigent_error(_request: Request, exc: OmnigentError) -> Response:
    """
    Convert :class:`OmnigentError` into a JSON response with the
    correct HTTP status.

    Mirrors AP's exception handler at
    ``omnigent/server/app.py``.

    :param _request: Unused (FastAPI signature requirement).
    :param exc: The application error to render.
    :returns: A JSON response with the error code + message.
    """
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.http_status,
        content={"error": {"code": exc.code, "message": exc.message}},
    )
