"""Deliver Omnigent web/mobile turns into the runner-owned Antigravity TUI.

Web turns and mid-turn steering use the same tmux delivery path so they land
on the cascade visible in Terminal. Headless ``SendUserCascadeMessage`` can
address a separate cascade that the attended TUI never displays. Delivery uses
:func:`omnigent.harnesses.antigravity_native.bridge.inject_user_message_via_tui`
for draft clearing, bracketed paste, and footer-verified submission.

The reader owns assistant output and native turn completion (see
:mod:`omnigent.harnesses.antigravity_native.reader`). This executor yields
:class:`TurnComplete` with ``response=None`` once injection succeeds; that
acknowledges delivery, not completion of native generation.

The TUI owns its selected model and thinking budget. ``ExecutorConfig.model``
is unused; ``reasoning_effort`` is validated but does not override the TUI.

Attachments are materialized under the bridge directory and referenced by
absolute path (``[Attached: <path>]``), alongside the user's text, so agy can
open them with its Read tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from omnigent.harnesses.antigravity_native.bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR,
    ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    cascade_is_current,
    inject_user_message_via_tui,
    interrupt_turn_via_tui,
    is_placeholder_conversation_id,
    read_bridge_state,
    turn_is_idle_via_tui,
    wait_for_turn_idle_via_tui,
)
from omnigent.harnesses.antigravity_native.rpc import (
    cancel_cascade_steps,
    resolve_language_server_port,
)
from omnigent.harnesses.antigravity_native.stop_hook import record_stop_event
from omnigent.harnesses.antigravity_native.transcript import resolve_owned_transcript
from omnigent.inner.executor import (
    EnqueuedContent,
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
    describe_exception,
)
from omnigent.llms.errors import PermanentLLMError
from omnigent.util.reasoning_effort import ANTIGRAVITY_EFFORTS, validate_effort_or_llm_error

_logger = logging.getLogger(__name__)

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
        self._delivery_epoch = 0
        self._interrupt_task: asyncio.Task[bool] | None = None

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — assistant output is emitted by the native reader."""
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
        Interrupt the active native Antigravity turn through RPC or the TUI.

        Prefer ``CancelCascadeSteps`` for a validated RPC conversation. If the
        port is unavailable or rejects cancellation, send the TUI's visible
        Escape cancel key to the runner-owned active pane instead.

        .. note:: **Scope — RUNNING cascades only (live-verified, C3).**
           ``CancelCascadeSteps`` stops an in-flight (generating) cascade — the
           case this serves: the user hits stop during generation. It is a
           **NO-OP on a step that is WAITING for a user interaction**
           (ask-question / command-permission): agy returns HTTP 200 but the
           WAITING step does not transition. A WAITING step is unblocked by
           delivering a DENY through the interaction bridge
           (:mod:`omnigent.harnesses.antigravity_native.interactions`), NOT here — this
           method deliberately does not attempt to handle that case.

        :param session_key: Adapter session key. Unused; the native bridge is
            per conversation.
        :returns: ``True`` after native idle is confirmed; ``False`` when the
            bridge is inactive or cancellation cannot be confirmed.
        """
        del session_key
        self._delivery_epoch += 1
        if self._interrupt_task is None or self._interrupt_task.done():
            self._interrupt_task = asyncio.create_task(self._interrupt_under_send_lock())
        return await asyncio.shield(self._interrupt_task)

    async def _interrupt_under_send_lock(self) -> bool:
        async with self._send_lock:
            try:
                return await interrupt_bridge_turn(
                    self._bridge_dir, expected_session_id=self._request_session_id
                )
            except (OSError, RuntimeError):
                _logger.exception("Antigravity native cancellation failed")
                return False

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Deliver the latest web/mobile user message to the running agy TUI.

        The reader mirrors the native reply independently. Successful injection
        yields :class:`TurnComplete` without text; a delivery failure yields
        :class:`ExecutorError`.

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
        :func:`omnigent.harnesses.antigravity_native.bridge.inject_user_message_via_tui`)
        rather than delivered over headless ``SendUserCascadeMessage`` RPC.

        The TUI owns its cascade and selected model, so delivery needs no RPC
        discovery. The send lock serializes injection with native cancellation;
        the delivery epoch rejects messages admitted before that cancellation.

        :param text: User message text to deliver.
        :returns: ``None`` on success, or a human-readable error string when the
            turn could not be delivered to the TUI (e.g. the agy pane exited).
        """
        delivery_epoch = self._delivery_epoch
        async with self._send_lock:
            if delivery_epoch != self._delivery_epoch:
                return "Antigravity native delivery was cancelled before injection"
            # The runner seeds bridge state before launching the terminal, so a
            # missing file means broken wiring (not a first turn) and is surfaced
            # as such.
            state = await asyncio.to_thread(read_bridge_state, self._bridge_dir)
            if delivery_epoch != self._delivery_epoch:
                return "Antigravity native delivery was cancelled before injection"
            if state is None:
                return "Antigravity native bridge state is missing"
            if not _session_is_active(state.session_id, self._request_session_id):
                return "Antigravity native session is no longer active"
            try:
                injection = asyncio.create_task(
                    asyncio.to_thread(
                        inject_user_message_via_tui,
                        self._bridge_dir,
                        content=text,
                    )
                )
                try:
                    await asyncio.shield(injection)
                except asyncio.CancelledError:
                    await injection
                    raise
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


async def interrupt_bridge_turn(
    bridge_dir: Path, *, expected_session_id: str | None = None
) -> bool:
    """Cancel one active bridge turn using its validated RPC or visible TUI pane."""
    state = await asyncio.to_thread(read_bridge_state, bridge_dir)
    if state is None or not _session_is_active(state.session_id, expected_session_id):
        return False
    expected_session_id = state.session_id
    expected_cascade_id = state.conversation_id
    if await asyncio.to_thread(turn_is_idle_via_tui, bridge_dir):
        return await _record_confirmed_interruption(
            bridge_dir,
            expected_session_id=expected_session_id,
            expected_cascade_id=expected_cascade_id,
        )
    if not is_placeholder_conversation_id(expected_cascade_id):
        port = await asyncio.to_thread(resolve_language_server_port, expected_cascade_id)
        if port is not None:
            cancelled = await asyncio.to_thread(cancel_cascade_steps, port, expected_cascade_id)
            if cancelled:
                _logger.info(
                    "antigravity native interrupt via CancelCascadeSteps: conversation=%s",
                    expected_cascade_id,
                )
                return await _record_confirmed_interruption(
                    bridge_dir,
                    expected_session_id=expected_session_id,
                    expected_cascade_id=expected_cascade_id,
                )
    try:
        cancelled = await asyncio.to_thread(
            interrupt_turn_via_tui,
            bridge_dir,
            expected_session_id=expected_session_id,
            expected_cascade_id=expected_cascade_id,
        )
    except RuntimeError as exc:
        _logger.warning("antigravity native TUI interrupt failed: %s", exc)
        return False
    if cancelled:
        _logger.info("antigravity native interrupt via TUI Escape: session=%s", state.session_id)
        return await _record_confirmed_interruption(
            bridge_dir,
            expected_session_id=expected_session_id,
            expected_cascade_id=expected_cascade_id,
        )
    return False


async def _record_confirmed_interruption(
    bridge_dir: Path, *, expected_session_id: str, expected_cascade_id: str
) -> bool:
    """Close a canceled transcript turn after agy's TUI confirms it is idle."""
    try:
        idle = await asyncio.to_thread(wait_for_turn_idle_via_tui, bridge_dir)
        if not idle:
            return False
        return await asyncio.to_thread(
            _record_confirmed_interruption_if_current,
            bridge_dir,
            expected_session_id=expected_session_id,
            expected_cascade_id=expected_cascade_id,
        )
    except (OSError, RuntimeError) as exc:
        _logger.warning("antigravity native interrupt completion not recorded: %s", exc)
        return False


def _record_confirmed_interruption_if_current(
    bridge_dir: Path, *, expected_session_id: str, expected_cascade_id: str
) -> bool:
    state = read_bridge_state(bridge_dir)
    if (
        state is None
        or state.session_id != expected_session_id
        or not cascade_is_current(expected_cascade_id, state.conversation_id)
    ):
        return False
    binding = resolve_owned_transcript(bridge_dir)
    if binding is None:
        return True
    state = read_bridge_state(bridge_dir)
    if (
        state is None
        or state.session_id != expected_session_id
        or not cascade_is_current(expected_cascade_id, state.conversation_id)
        or not cascade_is_current(state.conversation_id, binding.conversation_id)
    ):
        return False
    return record_stop_event(
        bridge_dir,
        {
            "conversationId": binding.conversation_id,
            "fullyIdle": True,
            "terminationReason": "USER_CANCELED",
        },
    )


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
    field :func:`omnigent.harnesses.antigravity_native.rpc.send_user_cascade_message` sends
    as ``cascadeConfig.plannerConfig.planModel``. A TUI-origin step using the
    older ``requestedModel.model`` (dict) shape is supported as a fallback.
    Newest-first because a later ``/model`` switch must win over an earlier turn's
    model. Fails closed (``None``) on any missing/unexpected shape, so the caller
    falls back to the recommended catalog entry.

    :param steps: Trajectory steps as returned by
        :func:`omnigent.harnesses.antigravity_native.rpc.get_trajectory_steps`.
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
        :func:`omnigent.harnesses.antigravity_native.rpc.get_available_models`.
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
