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

.. note:: The now-unused RPC-delivery helpers below
   (``_resolve_ready_cascade_id`` / ``_resolve_plan_model`` / ``_wait_for_state``
   and the model-resolution module functions) are retained pending a focused
   follow-up cleanup; the live write path is :meth:`_deliver` → the TUI inject.

Because agy owns its own model loop and emits output via the read path, this
executor:

* does NOT stream (``supports_streaming() -> False``) — the read driver posts the
  assistant message;
* yields a single :class:`TurnComplete` with ``response=None`` on a successful
  send (fabricating text here would double the read driver's mirrored message);
* supports a live message queue (``supports_live_message_queue() -> True``) — a
  mid-turn web message is delivered over the same RPC, which is how web steering
  works.

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
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR,
    ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    AntigravityNativeBridgeState,
    inject_user_message_via_tui,
    is_placeholder_conversation_id,
    read_bridge_state,
)
from omnigent.antigravity_native_rpc import (
    cancel_cascade_steps,
    get_available_models,
    get_trajectory_steps,
    resolve_language_server_port,
)
from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
)
from omnigent.llms.errors import PermanentLLMError
from omnigent.reasoning_effort import ANTIGRAVITY_EFFORTS, validate_effort_or_llm_error

_logger = logging.getLogger(__name__)

# How long run_turn waits for the bridge state to carry agy's REAL conversation
# id on the first turn (the runner cold-starts agy + mints the conversation, then
# the read path persists the real id over the launcher's ``agy_conv_*``
# placeholder — see Task 11). Mirrors the codex executor's
# one-second-poll-up-to-60s contract.
_STATE_WAIT_ATTEMPTS = 60
_STATE_WAIT_INTERVAL_S = 1.0

# agy step type for a committed user turn; its ``userConfig`` carries the model
# the user was on for that turn (the tier-1 model-echo source, design §10.4).
_USER_INPUT_STEP_TYPE = "CORTEX_STEP_TYPE_USER_INPUT"


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

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — assistant output is emitted by the RPC read driver."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — a mid-turn web message is delivered over the same turn-send RPC."""
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """
        Steer an active native Antigravity turn by delivering another message.

        Mid-turn web steering uses the exact same RPC turn-send path as
        :meth:`run_turn` (``SendUserCascadeMessage``), so the two need no
        special-casing.

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

        Resolves agy's conversation/cascade id (waiting briefly for the runner to
        mint it on the first turn), discovers the connect-RPC port, resolves the
        per-turn model, and delivers the message via ``SendUserCascadeMessage``
        (:func:`omnigent.antigravity_native_rpc.send_user_cascade_message`), which
        agy records as a real ``USER_INPUT`` turn. The assistant reply is mirrored
        back by the RPC read driver, so this yields a single :class:`TurnComplete`
        with no text on success (never a fabricated reply). On any failure it
        yields one :class:`ExecutorError`.

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
                yield ExecutorError(message=str(exc))
                return
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="Antigravity native turn had no user text to send")
            return
        outcome = await self._deliver(text)
        if outcome is not None:
            yield ExecutorError(message=outcome)
        else:
            yield TurnComplete(response=None)

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

    async def _resolve_ready_cascade_id(self, state: AntigravityNativeBridgeState) -> str | None:
        """
        Return agy's real conversation/cascade id, waiting on a fresh session.

        On a settled session bridge state already carries agy's real id and this
        returns it immediately. On a fresh session it still holds the launcher's
        ``agy_conv_*`` placeholder until the runner cold-starts agy, mints the
        conversation, and the read path persists the real id; this polls bridge
        state (:meth:`_wait_for_state`) until that real id appears. The caller
        holds :attr:`_send_lock`, so a later turn cannot race ahead of this wait.

        :param state: The already-read bridge state for this turn.
        :returns: agy's real (non-placeholder) conversation id, or ``None`` when a
            fresh session's real id never appeared within the wait window.
        """
        if not is_placeholder_conversation_id(state.conversation_id):
            return state.conversation_id
        confirmed = await self._wait_for_state()
        if confirmed is None or is_placeholder_conversation_id(confirmed.conversation_id):
            return None
        _logger.info(
            "antigravity native first turn: conversation registered as %s",
            confirmed.conversation_id,
        )
        return confirmed.conversation_id

    async def _resolve_plan_model(self, port: int, cascade_id: str) -> str | None:
        """
        Resolve the per-turn agy ``planModel`` enum (two-tier; design §10.4).

        ``SendUserCascadeMessage`` requires a ``planModel`` per turn and the enum
        names are version-volatile, so the model is resolved at runtime:

        1. **Echo agy's current model** — read the latest ``USER_INPUT`` step's
           ``userInput.userConfig.plannerConfig.planModel`` (a string on the live
           wire, with the older ``requestedModel.model`` shape as a fallback) from
           :func:`omnigent.antigravity_native_rpc.get_trajectory_steps`. This
           reflects the user's TUI ``/model`` choice without new plumbing.
        2. **Recommended fallback** — when no prior model is observable (a first
           turn), pick the ``recommended`` entry from
           :func:`omnigent.antigravity_native_rpc.get_available_models`.

        Both RPC reads are best-effort: a transport/parse failure on either is
        logged and treated as "no model from this tier", so a flaky read of the
        trajectory still falls through to the catalog rather than aborting.

        :param port: Validated agy connect-RPC port.
        :param cascade_id: agy cascade id (equal to the conversation id).
        :returns: An agy model enum string, or ``None`` when neither tier yields
            one (the caller surfaces a clear error — a turn cannot omit the
            model).
        """
        # Both RPC reads raise httpx.HTTPError (transport / non-2xx) or ValueError
        # (a non-JSON 200) per their contracts; either is best-effort here, so a
        # tier-1 failure falls through to the catalog and a tier-2 failure returns
        # None (the caller then surfaces a clear "no model" error).
        try:
            steps = await asyncio.to_thread(get_trajectory_steps, port, cascade_id)
        except (httpx.HTTPError, ValueError):
            _logger.debug(
                "antigravity native model echo: trajectory read failed for conversation=%s",
                cascade_id,
                exc_info=True,
            )
            steps = []
        echoed = _latest_requested_model(steps)
        if echoed is not None:
            return echoed
        try:
            catalog = await asyncio.to_thread(get_available_models, port)
        except (httpx.HTTPError, ValueError):
            _logger.debug(
                "antigravity native model fallback: catalog read failed for conversation=%s",
                cascade_id,
                exc_info=True,
            )
            return None
        return _recommended_model(catalog)

    async def _wait_for_state(self) -> AntigravityNativeBridgeState | None:
        """
        Read bridge state, polling until agy's REAL conversation id is known.

        Called by :meth:`_resolve_ready_cascade_id` when the bridge state still
        holds the launcher's ``agy_conv_*`` placeholder on a fresh session: the
        runner cold-starts agy + mints the conversation (Task 11), then the read
        path overwrites the placeholder with agy's real id. This polls until that
        real id appears. Settled turns read the real id immediately (no
        placeholder), so this is not on their path.

        :returns: Bridge state carrying a real (non-placeholder) conversation id;
            the last-read state (possibly a placeholder, or ``None``) when the
            real id never appeared within the wait window.
        """
        state: AntigravityNativeBridgeState | None = None
        for attempt in range(_STATE_WAIT_ATTEMPTS + 1):
            state = await asyncio.to_thread(read_bridge_state, self._bridge_dir)
            if state is not None and not is_placeholder_conversation_id(state.conversation_id):
                return state
            if attempt < _STATE_WAIT_ATTEMPTS:
                await asyncio.sleep(_STATE_WAIT_INTERVAL_S)
        return state


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
        from omnigent.inner.native_attachments import materialize_attachment

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
                path = materialize_attachment(block, bridge_dir)
                if path is not None:
                    attachment_lines.append(f"[Attached: {path}]")
        return "\n".join(attachment_lines + text_parts).strip()
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=True)
