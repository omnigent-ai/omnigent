"""Executor that delivers Omnigent web/mobile turns into a native Antigravity agy.

``omnigent antigravity`` runs the Antigravity ``agy`` CLI in a runner-owned tmux
terminal and mirrors its transcript into the Omnigent session via the RPC read
driver (the read path). This executor is the **write path**: when a turn is
submitted from the Omnigent web/mobile UI it delivers the user's message by
TYPING IT INTO the agy TUI pane over tmux
(:func:`omnigent.antigravity_native_bridge.inject_user_message_via_tui`), exactly
like the **claude**/**codex** native bridges drive their vendor panes. agy then
runs a real model turn and its reply flows back through the read driver.

**Why typing into the TUI, not headless ``SendUserCascadeMessage`` RPC
(#1156/#1158).** Typing into the TUI gives true parity with claude/codex native:
the turn RENDERS in the agy TUI AND lands on the SAME cascade the TUI displays,
so the agy TUI and the Omnigent web mirror share ONE conversation in both
directions. The prior headless RPC path delivered onto a separate
``StartCascade`` cascade the TUI never showed — so the agy TUI never echoed web
turns (#1156) and TUI-typed turns never mirrored to the web (#1158). agy records
a TUI-typed turn as a real ``CORTEX_STEP_TYPE_USER_INPUT`` step (what the read
driver keys on); the careful inject (draft-clear + bracketed paste +
footer-verified submit) handles the attended-TUI race. RPC remains the
read/control transport only (``StreamAgentStateUpdates`` /
``GetAllCascadeTrajectories`` / ``CancelCascadeSteps`` /
``HandleCascadeUserInteraction``). The same path serves mid-turn steering.

Because agy owns its own model loop and emits output via the read path, this
executor:

* does NOT stream (``supports_streaming() -> False``) — the read driver posts the
  assistant message;
* yields ONE terminal event per turn, and only once agy has actually FINISHED the
  turn (see the completion gate below);
* supports a live message queue (``supports_live_message_queue() -> True``) — a
  mid-turn web message is delivered over the same RPC, which is how web steering
  works.

**The completion gate (the load-bearing correctness detail).** Delivery and
completion are two different events, and this executor used to conflate them: it
yielded ``TurnComplete`` the moment ``_deliver`` returned, i.e. as soon as the
text had been TYPED AND SUBMITTED in the TUI. Nothing waited for agy to do the
work. A caller that dispatches an implementation task — a polly orchestrator
running agy as a worker — therefore collected an immediate, empty success before
agy had run a single tool, and read "no changes" as "the task is done". Unlike
the claude/codex harnesses there is no headless per-turn subprocess whose exit
supplies that signal, so this executor supplies it itself: after a successful
delivery it polls agy's own RPC trajectory (``GetCascadeTrajectorySteps``, the
same surface the read driver mirrors) until the turn reaches a TERMINAL state,
classified by the shared, pure helpers in
:mod:`omnigent.antigravity_native_steps`:

* a DONE ``PLANNER_RESPONSE`` carrying assistant text → :class:`TurnComplete`,
  carrying agy's final text (the harness adapter does not re-emit it as a
  conversation item, so the read driver stays the sole mirror source and nothing
  double-renders);
* an ERROR ``PLANNER_RESPONSE`` → :class:`ExecutorError`, so a model / safety /
  rate-limit / provider failure is never collapsed into a success;
* a degenerate turn that closes without a text planner → reconciled against agy's
  OWN cascade run status (``GetAllCascadeTrajectories``), the same idle backstop
  the read driver uses, confirmed over consecutive checks;
* neither, within the budget → :class:`ExecutorError` naming the timeout.

The gate is bounded by two named budgets, :data:`_TURN_START_TIMEOUT_S` and
:data:`_TURN_COMPLETION_TIMEOUT_S`. Note that the bridge's tmux timeouts
(``_TMUX_SEND_TIMEOUT_S`` / ``_TMUX_READY_TIMEOUT_S`` / ``_PASTE_COMMIT_TIMEOUT_S``
/ ``_SUBMIT_VERIFY_TIMEOUT_S``) cover only local TUI delivery, and agy's own
``--print-timeout`` never applies because this path does not use ``--print`` — so
before the gate there was NO task-completion budget of any kind here.

Mid-turn steering (:meth:`AntigravityNativeExecutor.enqueue_session_message`) is
deliberately NOT gated: it is a delivery, not a turn, and the ``run_turn`` gate
that is already open is what waits for the steered work to finish.

**Per-turn model (the load-bearing detail).** ``SendUserCascadeMessage`` REQUIRES
a ``planModel`` enum per turn (omitting it errors "neither PlanModel nor
RequestedModel specified"), and the enum names are version-volatile so they are
NEVER hardcoded. The executor resolves the model at runtime in two tiers
(design §10.4): (1) echo agy's CURRENT model from the latest ``USER_INPUT`` step's
``userInput.userConfig.plannerConfig.planModel`` (a string on the live wire, with
the older ``requestedModel.model`` shape as a fallback) reflecting the user's TUI
``/model`` choice without new plumbing; (2) on a first turn / when no
prior model is observable, fall back to the ``recommended`` entry from
``GetAvailableModels``. The Omnigent ``ExecutorConfig.model``/``reasoning_effort``
stay informational on this write path — agy's own model selection determines the
turn's model and thinking budget and cannot be overridden from here.

Attachment note: the RPC turn text takes plain text, so an image/file attachment
on a web turn is materialized to a file under the bridge dir and referenced by
absolute path (``[Attached: <path>]``) so agy can open it with its Read tool —
mirroring cursor-native. Any prose the user typed is sent alongside the marker.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR,
    ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    inject_user_message_via_tui,
    is_placeholder_conversation_id,
    read_bridge_state,
)
from omnigent.antigravity_native_rpc import (
    cancel_cascade_steps,
    get_all_cascade_trajectories,
    get_trajectory_steps,
    resolve_language_server_port,
)
from omnigent.antigravity_native_steps import (
    cascade_is_idle,
    classify_turn_outcome,
    find_turn_start_index,
)
from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnCancelled,
    TurnComplete,
    describe_exception,
)
from omnigent.llms.errors import PermanentLLMError
from omnigent.reasoning_effort import ANTIGRAVITY_EFFORTS, validate_effort_or_llm_error

_logger = logging.getLogger(__name__)

# agy step type for a committed user turn; its ``userConfig`` carries the model
# the user was on for that turn (the tier-1 model-echo source, design §10.4).
_USER_INPUT_STEP_TYPE = "CORTEX_STEP_TYPE_USER_INPUT"

# --- Completion-gate budgets (see the module docstring) --------------------
#
# These are the ONLY task-completion budgets on this path. The bridge's tmux
# timeouts bound local TUI delivery, not the work; agy's ``--print-timeout``
# never applies because this path does not use ``--print``.

# Seconds between trajectory polls while waiting for the turn to finish. agy's
# steps finalize at DONE (no token streaming on this surface) and the gate only
# needs the terminal edge, not a live mirror — that is the read driver's job at
# its own much faster cadence — so this is deliberately slow enough to be free
# next to a multi-minute agent turn.
_TURN_POLL_INTERVAL_S = 2.0

# Seconds after a SUCCESSFUL delivery in which agy must record the turn — a
# USER_INPUT step for our text must appear on a reachable cascade. The submit was
# already footer-verified by the injector, so this only has to absorb agy minting
# its cascade on a first turn plus RPC-port discovery. Blowing this budget means
# the paste landed somewhere that is not a turn, which is a failure, not a slow
# agent: it is reported as one rather than waiting out the full completion
# budget.
_TURN_START_TIMEOUT_S = 180.0

# Total seconds an OPEN turn may run before the gate gives up. Sized for
# long-running implementation work, which is the case that motivated the gate: a
# dispatched worker editing several files, running a build and a test suite is
# routinely tens of minutes, so anything in the low minutes (agy's own 5-minute
# ``--print-timeout`` default, say) would abort real work and be worse than the
# bug. One hour is long enough that a legitimate implementation turn finishes
# inside it, and short enough that a wedged agy — one parked forever on a
# permission gate, or hung mid-tool — frees the caller's slot the same day
# instead of never. A turn that hits this yields an ExecutorError naming the
# budget; it never degrades into a success.
_TURN_COMPLETION_TIMEOUT_S = 3600.0

# The idle backstop. A turn can close without a text-carrying planner step (the
# degenerate shape the read driver documents), which no step-based rule can tell
# apart from a dispatch. agy's own cascade run status settles it, but a single
# reading can catch the gap between the turn being recorded and agy starting it,
# so the gate requires consecutive idle readings, spaced by whole poll cycles,
# and consults it only AFTER the turn is confirmed open.
_IDLE_CHECK_EVERY_N_POLLS = 5
_IDLE_CONFIRM_CHECKS = 2


@dataclass(frozen=True)
class _GateResult:
    """
    Outcome of the post-delivery completion gate for one turn.

    :param completed: ``True`` when agy reached a terminal DONE state for this
        turn. ``False`` for every non-success — an agy ERROR, a timeout, or a
        turn that never became observable — which the caller surfaces as an
        :class:`ExecutorError`.
    :param text: agy's final assistant text for the turn, when the closing step
        carried any.
    :param error: Human-readable, diagnosable failure description. Always set
        when ``completed`` is ``False``.
    :param retryable: ``True`` when the failure is one a retry might survive
        (agy reported a model/provider ERROR), ``False`` for structural failures
        (the turn never started, or it exhausted its budget — retrying either
        would just burn the budget again).
    :param cancelled: ``True`` when the gate stopped because the turn was
        interrupted. Neither a success nor a failure of the turn's work.
    """

    completed: bool
    text: str | None = None
    error: str | None = None
    retryable: bool = False
    cancelled: bool = False


async def _sleep(seconds: float) -> None:
    """
    Stubbable indirection for the completion gate's poll delay.

    Exists so tests can drive the gate without real delays without patching
    ``asyncio.sleep`` globally (mirrors the read driver's ``_sleep``).

    :param seconds: Delay in seconds.
    :returns: None after the sleep completes.
    """
    await asyncio.sleep(seconds)


class AntigravityNativeExecutor(Executor):
    """
    Harness-side executor for ``omnigent antigravity`` web UI turns.

    Delivers the latest web/mobile user message to the running agy over its
    connect-RPC ``SendUserCascadeMessage``; agy's reply is mirrored back by the
    RPC read driver.

    :param bridge_dir: Optional bridge directory override. ``None``
        reads :data:`ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR`.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        # Serializes _deliver so a concurrent run_turn (initiating message) and
        # enqueue_session_message (mid-turn steer, live message queue) don't send
        # to agy at once or deliver out of order.
        self._send_lock = asyncio.Lock()
        # Completion-gate RPC target cache. Port discovery scans processes, so it
        # is resolved once per cascade and re-resolved only when a read fails or
        # the bridge rebinds to a different cascade (a TUI ``/clear``).
        self._rpc_port: int | None = None
        self._rpc_cascade_id: str | None = None
        # Set by interrupt_session so the completion gate stops waiting as soon
        # as the user hits stop. Without it a cancelled turn would sit in the gate
        # until agy's own trajectory reflected the cancel.
        self._interrupted = asyncio.Event()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — assistant output is emitted by the RPC read driver."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — a mid-turn web message is delivered over the same turn-send RPC."""
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """
        Steer an active native Antigravity turn by delivering another message.

        Mid-turn web steering uses the exact same delivery path as
        :meth:`run_turn`, so the two need no special-casing.

        This deliberately does NOT wait for completion. A steer is a delivery
        into a turn that is already open, and the ``run_turn`` completion gate
        holding that turn is what waits for the steered work to finish; gating
        here too would block the caller that is trying to steer.

        :param session_key: Adapter session key. Unused; the native bridge is
            per conversation.
        :param content: User-supplied content (string or content blocks).
        :returns: ``True`` when agy accepted the steering message, ``False``
            when there was no text to send or delivery failed.
        """
        del session_key
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        outcome = await self._deliver(text)
        return outcome is None

    async def interrupt_session(self, session_key: str) -> bool:
        """
        Interrupt the active native Antigravity turn via ``CancelCascadeSteps``.

        Resolves the cascade id from bridge state (the cascade id IS the
        conversation id), discovers agy's connect-RPC port, and asks agy to
        cancel the running cascade
        (:func:`omnigent.antigravity_native_rpc.cancel_cascade_steps`).

        .. note:: **Scope — RUNNING cascades only (live-verified, C3).**
           ``CancelCascadeSteps`` stops an in-flight (generating) cascade — the
           case this serves: the user hits stop during generation. It is a
           **NO-OP on a step that is WAITING for a user interaction**
           (ask-question / command-permission): agy returns HTTP 200 but the
           WAITING step does not transition. A WAITING step is unblocked by
           delivering a DENY through the interaction bridge
           (:mod:`omnigent.antigravity_native_interactions`), NOT here — this
           method deliberately does not attempt to handle that case.

        :param session_key: Adapter session key. Unused; the native bridge is
            per conversation.
        :returns: ``True`` when agy accepted the cancel; ``False`` when there is
            no real conversation yet (placeholder / missing or inactive bridge
            state), no agy connect-RPC port could be resolved, or the cancel RPC
            failed.
        """
        del session_key
        # Release the completion gate FIRST and unconditionally: the user asked
        # to stop, so the turn must stop being waited on whether or not agy
        # accepts the cancel below.
        self._interrupted.set()
        state = await asyncio.to_thread(read_bridge_state, self._bridge_dir)
        if state is None or not _session_is_active(state.session_id, self._request_session_id):
            return False
        cascade_id = state.conversation_id
        # No live cascade exists before agy mints its real id, so never RPC the
        # ``agy_conv_*`` placeholder.
        if is_placeholder_conversation_id(cascade_id):
            return False
        port = await asyncio.to_thread(resolve_language_server_port, cascade_id)
        if port is None:
            _logger.warning(
                "antigravity native interrupt: no connect-RPC port for conversation=%s",
                cascade_id,
            )
            return False
        cancelled = await asyncio.to_thread(cancel_cascade_steps, port, cascade_id)
        _logger.info(
            "antigravity native interrupt via CancelCascadeSteps: conversation=%s accepted=%s",
            cascade_id,
            cancelled,
        )
        return cancelled

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Deliver the latest web/mobile user message to the running agy over RPC.

        Delivers the latest user message by typing it into the agy TUI, then
        WAITS for agy to finish the turn before reporting anything
        (:meth:`_await_turn_completion`). Successful delivery is not completion:
        reporting on delivery alone is what let a dispatched implementation task
        return an immediate empty success (see the module docstring). Exactly one
        terminal event is yielded — :class:`TurnComplete` carrying agy's final
        text once the turn reaches a terminal DONE state, or
        :class:`ExecutorError` when delivery fails, when agy's turn ends in ERROR,
        or when the turn exhausts its budget.

        :param messages: Conversation history in executor message shape; the
            latest user message is delivered.
        :param tools: Tool schemas from Omnigent. Ignored; native agy owns its
            own tool surface.
        :param system_prompt: System prompt from the agent spec. Ignored; the
            native conversation was created by the wrapper.
        :param config: Per-turn executor config. Only ``reasoning_effort`` is
            read; it is validated against :data:`ANTIGRAVITY_EFFORTS` and an
            unsupported value surfaces as a non-retryable error. The validated
            effort is informational — agy's model selection determines the actual
            model + thinking budget on the agy side and cannot be overridden from
            this write path (see the module docstring).
        :returns: Async iterator yielding one terminal event.
        """
        del tools, system_prompt
        if config is not None:
            effort = (config.extra or {}).get("reasoning_effort")
            try:
                validate_effort_or_llm_error(effort, "antigravity", ANTIGRAVITY_EFFORTS)
            except PermanentLLMError as exc:
                yield ExecutorError(message=describe_exception(exc))
                return
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="Antigravity native turn had no user text to send")
            return
        self._interrupted.clear()
        # Snapshot BEFORE delivering: the step count is what tells this turn's
        # USER_INPUT step apart from the previous turn's, whose closing planner
        # would otherwise satisfy the gate instantly.
        baseline = await self._snapshot_step_count()
        failure = await self._deliver(text)
        if failure is not None:
            yield ExecutorError(message=failure)
            return
        result = await self._await_turn_completion(delivered_text=text, baseline=baseline)
        if result.cancelled:
            yield TurnCancelled()
        elif result.completed:
            yield TurnComplete(response=result.text)
        else:
            yield ExecutorError(
                message=result.error or "Antigravity native turn did not complete",
                retryable=result.retryable,
            )

    async def _deliver(self, text: str) -> str | None:
        """
        Deliver one message to agy by typing it into the agy TUI.

        Shared by :meth:`run_turn` (initiating message) and
        :meth:`enqueue_session_message` (mid-turn steering). The turn is injected
        into the agy TUI pane over tmux (bracketed paste + Enter — see
        :func:`omnigent.antigravity_native_bridge.inject_user_message_via_tui`)
        rather than delivered over headless ``SendUserCascadeMessage`` RPC.

        Typing into the TUI is what gives antigravity-native true parity with
        claude/codex native (#1156/#1158): the turn renders in the agy TUI AND
        lands on the SAME cascade the TUI displays, so the read driver mirrors a
        single, unified conversation in both directions. The headless RPC path,
        by contrast, delivered onto a separate ``StartCascade`` cascade the TUI
        never showed — splitting the TUI and the web mirror into two cascades
        (the agy TUI never echoed web turns; TUI-typed turns never mirrored).
        agy records a TUI-typed turn as a real ``CORTEX_STEP_TYPE_USER_INPUT``
        step, exactly what the read driver keys on; the careful inject
        (draft-clear + bracketed paste + footer-verified submit) handles the
        attended-TUI race. RPC remains the read/control transport
        (``StreamAgentStateUpdates`` / ``GetAllCascadeTrajectories`` /
        ``CancelCascadeSteps`` / ``HandleCascadeUserInteraction``).

        Unlike the RPC path, this needs no cascade id, port, or per-turn model
        resolution up front: the TUI owns its cascade and its selected model, and
        agy mints the cascade on the first typed turn (which the read driver then
        discovers/binds — see :mod:`omnigent.antigravity_native_reader`).

        :param text: User message text to deliver.
        :returns: ``None`` on success, or a human-readable error string when the
            turn could not be delivered to the TUI (e.g. the agy pane exited).
        """
        async with self._send_lock:
            # The runner seeds bridge state before launching the terminal, so a
            # missing file means broken wiring (not a first turn) and is surfaced
            # as such.
            state = await asyncio.to_thread(read_bridge_state, self._bridge_dir)
            if state is None:
                return "Antigravity native bridge state is missing"
            if not _session_is_active(state.session_id, self._request_session_id):
                return "Antigravity native session is no longer active"
            try:
                await asyncio.to_thread(
                    inject_user_message_via_tui,
                    self._bridge_dir,
                    content=text,
                )
            except RuntimeError as exc:
                # The TUI pane is gone / never advertised / the submit never
                # started a turn. Surface it so the UI can prompt a restart
                # rather than reporting a fake success the mirror never fills.
                return f"Could not deliver the turn to the agy TUI: {exc}"
            _logger.info(
                "antigravity native delivered turn via TUI injection (session=%s)",
                state.session_id,
            )
            return None

    async def _await_turn_completion(
        self, *, delivered_text: str, baseline: int | None
    ) -> _GateResult:
        """
        Block until agy finishes the delivered turn, or a budget runs out.

        The completion gate. Polls ``GetCascadeTrajectorySteps`` — the same
        surface the read driver mirrors — and classifies the turn with the shared
        pure helpers in :mod:`omnigent.antigravity_native_steps`, so the gate and
        the mirrored session status can never disagree about whether a turn ended.

        Two phases, each with its own budget:

        #. **Start** (:data:`_TURN_START_TIMEOUT_S`): agy must record the delivery
           as a USER_INPUT step (:func:`find_turn_start_index`). Until that step
           exists there is no turn to wait on, and a delivery that never becomes
           one is a failure worth reporting promptly rather than waiting out the
           full completion budget.
        #. **Completion** (:data:`_TURN_COMPLETION_TIMEOUT_S`, measured from
           delivery): the open turn must reach a terminal state — a DONE planner
           with text, an ERROR planner, or agy's own cascade status settling to
           idle across :data:`_IDLE_CONFIRM_CHECKS` consecutive checks for the
           degenerate close.

        A trajectory read that fails (transport, non-2xx, non-JSON body, no
        resolvable RPC port yet) is transient: it is logged, the cached port is
        dropped so the next pass re-resolves, and the loop retries until a budget
        expires. It never short-circuits into a success.

        :param delivered_text: The exact text delivered, used to identify this
            turn's USER_INPUT step when the pre-delivery snapshot was unavailable.
        :param baseline: Step count observed before delivery, or ``None`` when it
            could not be read (see :meth:`_snapshot_step_count`).
        :returns: The turn's :class:`_GateResult`.
        """
        started_at = time.monotonic()
        start_index: int | None = None
        # Counted from the poll that first saw the turn OPEN, not from gate
        # entry, so the idle backstop can never fire on the very reading that
        # discovered the turn — the one reading agy may not have started yet.
        polls_since_open = -1
        idle_checks = 0
        latest_text: str | None = None
        waiting_on_user = False
        while True:
            if self._interrupted.is_set():
                _logger.info("antigravity native completion gate released by interrupt")
                return _GateResult(completed=False, text=latest_text, cancelled=True)
            steps = await self._read_trajectory()
            if steps is not None:
                if start_index is None:
                    start_index = find_turn_start_index(
                        steps,
                        baseline_step_count=baseline,
                        delivered_text=delivered_text,
                    )
                if start_index is not None:
                    polls_since_open += 1
                    outcome = classify_turn_outcome(steps, start_index=start_index)
                    latest_text = outcome.text or latest_text
                    waiting_on_user = outcome.waiting_on_user
                    if outcome.state == "done":
                        _logger.info(
                            "antigravity native turn completed (DONE planner) after %.1fs",
                            time.monotonic() - started_at,
                        )
                        return _GateResult(completed=True, text=outcome.text)
                    if outcome.state == "error":
                        detail = f": {outcome.error}" if outcome.error else ""
                        return _GateResult(
                            completed=False,
                            text=outcome.text,
                            error=(
                                "Antigravity native turn ended in an agy ERROR state"
                                f"{detail} (the model did not complete the turn)"
                            ),
                            # A model / safety / rate-limit / provider-overload
                            # failure is exactly the transient class the workflow
                            # may retry; a structural gate failure below is not.
                            retryable=True,
                        )
                    # Still running. The degenerate close (a turn that ends with
                    # no text-carrying planner) is only visible in agy's own
                    # cascade status, and only after enough consecutive idle
                    # readings that a not-yet-started turn cannot masquerade as a
                    # finished one.
                    if polls_since_open > 0 and polls_since_open % _IDLE_CHECK_EVERY_N_POLLS == 0:
                        if await self._cascade_reports_idle():
                            idle_checks += 1
                            if idle_checks >= _IDLE_CONFIRM_CHECKS:
                                _logger.info(
                                    "antigravity native turn completed (cascade idle) after %.1fs",
                                    time.monotonic() - started_at,
                                )
                                return _GateResult(completed=True, text=latest_text)
                        else:
                            idle_checks = 0

            elapsed = time.monotonic() - started_at
            if start_index is None and elapsed >= _TURN_START_TIMEOUT_S:
                return _GateResult(
                    completed=False,
                    error=(
                        "Antigravity native turn was delivered to the agy TUI but agy never "
                        f"recorded it as a turn within {_TURN_START_TIMEOUT_S:.0f}s "
                        "(no matching USER_INPUT step; the agy connect-RPC may be unreachable "
                        "or the submitted text never opened a cascade turn)"
                    ),
                )
            if elapsed >= _TURN_COMPLETION_TIMEOUT_S:
                blocked = (
                    " — agy is parked on a WAITING interaction (an ask-question or "
                    "permission gate nobody answered)"
                    if waiting_on_user
                    else ""
                )
                return _GateResult(
                    completed=False,
                    text=latest_text,
                    error=(
                        "Antigravity native turn did not reach a terminal state within "
                        f"{_TURN_COMPLETION_TIMEOUT_S:.0f}s{blocked}. The turn was delivered "
                        "and may still be running in the agy TUI; its work is NOT confirmed"
                    ),
                )
            await _sleep(_TURN_POLL_INTERVAL_S)

    async def _snapshot_step_count(self) -> int | None:
        """
        Read the cascade's step count before delivering, for turn identification.

        The gate needs to tell THIS turn's USER_INPUT step from the previous
        turn's; the pre-delivery step count does that positionally.

        :returns: ``0`` when agy has not minted the conversation yet (a first
            turn has no earlier step to be confused with), the current step count
            when the trajectory reads cleanly, or ``None`` when it could not be
            read — in which case the gate identifies the turn by its text instead
            (see :func:`find_turn_start_index`).
        """
        state = await asyncio.to_thread(read_bridge_state, self._bridge_dir)
        if state is None:
            return None
        if is_placeholder_conversation_id(state.conversation_id):
            return 0
        steps = await self._read_trajectory()
        return None if steps is None else len(steps)

    async def _resolve_rpc_target(self) -> tuple[int, str] | None:
        """
        Resolve the ``(port, cascade_id)`` the gate should query, using a cache.

        Re-reads bridge state every call so a cascade rotation (a TUI ``/clear``
        rebinding the bridge) invalidates the cached port rather than polling a
        stale conversation.

        :returns: The connect-RPC port and cascade id, or ``None`` when there is
            no real cascade yet (missing state / an ``agy_conv_*`` placeholder) or
            no agy port could be discovered.
        """
        state = await asyncio.to_thread(read_bridge_state, self._bridge_dir)
        if state is None:
            return None
        cascade_id = state.conversation_id
        if is_placeholder_conversation_id(cascade_id):
            return None
        if self._rpc_port is not None and self._rpc_cascade_id == cascade_id:
            return self._rpc_port, cascade_id
        port = await asyncio.to_thread(resolve_language_server_port, cascade_id)
        if port is None:
            return None
        self._rpc_port, self._rpc_cascade_id = port, cascade_id
        return port, cascade_id

    async def _read_trajectory(self) -> list[dict[str, object]] | None:
        """
        Read the bound cascade's trajectory steps, or ``None`` when unavailable.

        :returns: The ordered step list, or ``None`` when no cascade/port is
            resolvable yet or the read failed. ``None`` is "unknown", never
            "finished" — the caller keeps waiting on it.
        """
        target = await self._resolve_rpc_target()
        if target is None:
            return None
        port, cascade_id = target
        try:
            return await asyncio.to_thread(get_trajectory_steps, port, cascade_id)
        except (httpx.HTTPError, ValueError) as exc:
            # Transport / non-2xx / non-JSON 200. Drop the cached port so the
            # next pass rediscovers agy (it may have restarted on a new port).
            self._rpc_port = None
            _logger.warning(
                "antigravity native completion gate trajectory read failed; retrying: "
                "cascade=%s port=%s error=%r",
                cascade_id,
                port,
                exc,
            )
            return None

    async def _cascade_reports_idle(self) -> bool:
        """
        Whether agy's own run status says the bound cascade is no longer working.

        The idle backstop for a turn that closes without a text-carrying planner
        step. Fails closed: any read failure reports "not idle", so an unreadable
        agy can never be mistaken for a finished turn.

        :returns: ``True`` only on an explicit idle status for the bound cascade.
        """
        target = await self._resolve_rpc_target()
        if target is None:
            return False
        port, cascade_id = target
        try:
            body = await asyncio.to_thread(get_all_cascade_trajectories, port)
        except (httpx.HTTPError, ValueError) as exc:
            self._rpc_port = None
            _logger.warning(
                "antigravity native completion gate idle check failed: cascade=%s error=%r",
                cascade_id,
                exc,
            )
            return False
        summaries = body.get("trajectorySummaries")
        if not isinstance(summaries, dict):
            return False
        return cascade_is_idle(summaries, cascade_id)


def _bridge_dir_from_env() -> Path:
    """
    Resolve the native Antigravity bridge directory from harness spawn env.

    :returns: Bridge directory path.
    :raises RuntimeError: If the env var is missing.
    """
    raw = os.environ.get(ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR} is required")
    return Path(raw)


def _request_session_id_from_env() -> str | None:
    """
    Resolve the Omnigent session id that requested this harness process.

    :returns: Omnigent session id, e.g. ``"conv_abc123"``, or ``None``.
    """
    raw = os.environ.get(ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR, "").strip()
    return raw or None


def _session_is_active(session_id: str, request_session_id: str | None) -> bool:
    """
    Return whether this harness may deliver into the native conversation.

    :param session_id: Session id from bridge state.
    :param request_session_id: Session id from harness spawn env.
    :returns: ``True`` when delivery is allowed.
    """
    return request_session_id is None or request_session_id == session_id


def _latest_requested_model(steps: list[dict[str, object]]) -> str | None:
    """
    Return the model from the latest ``USER_INPUT`` step, echoing agy's choice.

    Tier-1 of the per-turn model resolution (design §10.4): scans the trajectory
    steps from newest to oldest for the most recent ``CORTEX_STEP_TYPE_USER_INPUT``
    step and returns its model enum. The live wire (agy 1.0.10) carries the enum
    as a STRING at ``userInput.userConfig.plannerConfig.planModel`` — the same
    field :func:`omnigent.antigravity_native_rpc.send_user_cascade_message` sends
    as ``cascadeConfig.plannerConfig.planModel``. A TUI-origin step using the
    older ``requestedModel.model`` (dict) shape is supported as a fallback.
    Newest-first because a later ``/model`` switch must win over an earlier turn's
    model. Fails closed (``None``) on any missing/unexpected shape, so the caller
    falls back to the recommended catalog entry.

    :param steps: Trajectory steps as returned by
        :func:`omnigent.antigravity_native_rpc.get_trajectory_steps`.
    :returns: The agy model enum string from the latest USER_INPUT step, or
        ``None`` when no USER_INPUT step carries one (e.g. a first turn).
    """
    for step in reversed(steps):
        if not isinstance(step, dict) or step.get("type") != _USER_INPUT_STEP_TYPE:
            continue
        plan_model = _dig(step, "userInput", "userConfig", "plannerConfig", "planModel")
        if isinstance(plan_model, str) and plan_model:
            return plan_model
        legacy = _dig(step, "userInput", "userConfig", "plannerConfig", "requestedModel", "model")
        if isinstance(legacy, str) and legacy:
            return legacy
    return None


def _recommended_model(catalog: dict[str, object]) -> str | None:
    """
    Return the ``recommended`` model enum from an agy model catalog.

    Tier-2 of the per-turn model resolution (design §10.4): picks the entry agy
    marks ``recommended`` from a ``GetAvailableModels`` catalog
    (``{"models": {<key>: {"model", "recommended", ...}}}``) so a first turn uses
    agy's own default. Fails closed (``None``) when no entry is recommended or the
    shape is unexpected, so the caller surfaces a clear error rather than guessing
    a model.

    :param catalog: The parsed ``GetAvailableModels`` response as returned by
        :func:`omnigent.antigravity_native_rpc.get_available_models`.
    :returns: The agy model enum string of the recommended entry, or ``None``.
    """
    models = catalog.get("models")
    if not isinstance(models, dict):
        return None
    for entry in models.values():
        if not isinstance(entry, dict) or not entry.get("recommended"):
            continue
        model = entry.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def _dig(obj: object, *keys: str) -> object:
    """
    Walk nested dicts by ``keys``, returning ``None`` on any missing/non-dict hop.

    A small typed accessor for the deeply-nested agy step shapes so the
    model-echo path stays readable without a ladder of ``isinstance`` checks.

    :param obj: The root object (expected to be a nested dict).
    :param keys: The ordered keys to traverse.
    :returns: The value at the nested path, or ``None`` if any intermediate value
        is missing or not a dict.
    """
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """
    Extract the latest user message's text from the executor message list.

    :param messages: Executor message list.
    :param bridge_dir: Bridge directory; image/file attachments are
        materialized underneath it and referenced by path.
    :returns: The user's text (string + content-block shapes flattened), or
        ``""`` when there is no user text to send.
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: EnqueuedContent, bridge_dir: Path) -> str:
    """
    Flatten executor message content into plain text for the agy turn-send.

    The RPC turn text carries only text. A plain string passes through. A list
    of content blocks contributes every ``input_text`` / ``text`` block;
    ``input_image`` / ``input_file`` blocks carrying a base64 data URI are
    materialized to the bridge dir and referenced by absolute path
    (``[Attached: <path>]``) so agy can open them with its Read tool — otherwise
    web-UI attachments are silently dropped. Mirrors cursor-native.

    :param content: Message content — a string, a list of content blocks like
        ``{"type": "input_text", "text": "..."}``, or other.
    :param bridge_dir: Bridge directory; attachments are materialized underneath
        it and referenced by path.
    :returns: The flattened text, stripped of leading/trailing whitespace, or
        ``""`` when no text is present.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        from omnigent.inner.native_attachments import attachment_reference_line

        attachment_lines: list[str] = []
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type in ("input_text", "text"):
                text = block.get("text")
                if isinstance(text, str) and text:
                    text_parts.append(text)
            elif block_type in ("input_image", "input_file"):
                attachment_lines.append(attachment_reference_line(block, bridge_dir))
        return "\n".join(attachment_lines + text_parts).strip()
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=True)
