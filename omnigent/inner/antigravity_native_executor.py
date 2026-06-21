"""Executor that injects Omnigent web/mobile turns into a native Antigravity TUI.

``omnigent antigravity`` runs the Antigravity ``agy`` CLI in a runner-owned tmux
terminal and mirrors its transcript into the Omnigent session via
:mod:`omnigent.antigravity_native_forwarder` (the read path). This executor is
the **write path**: when a turn is submitted from the Omnigent web/mobile UI it
types the user's message into the running agy TUI over tmux
(:func:`omnigent.antigravity_native_bridge.inject_user_message_via_tui`). agy
then runs a real model turn and its reply flows back through the transcript
forwarder — exactly like the **claude** native bridge, which also delivers every
web turn (and every mid-turn steer) via tmux send-keys.

**Why the TUI, not connect-RPC.** agy exposes a connect-RPC ``SendAgentMessage``,
but a turn delivered that way is recorded in agy's transcript as a
``SYSTEM_MESSAGE`` ("not actually sent by the user"), NOT a ``USER_INPUT`` step —
so the forwarder (which mirrors user turns from ``USER_INPUT``) never commits the
user's message, leaving it a stuck optimistic bubble while its reply renders
above it (verified against agy 1.0.10). Typing into the TUI creates a real
``USER_INPUT`` step that the forwarder mirrors in order, matching claude/codex
native (both commit the user message before its assistant reply). The same path
serves mid-turn steering: agy accepts a send-keys paste while a turn is running
and queues it as the next ``USER_INPUT`` (also verified).

Because agy owns its own model loop and emits output via the transcript, this
executor:

* does NOT stream (``supports_streaming() -> False``) — the forwarder posts the
  assistant message;
* yields a single :class:`TurnComplete` with ``response=None`` on a successful
  injection (fabricating text here would double the forwarder's mirrored
  message);
* supports a live message queue (``supports_live_message_queue() -> True``) —
  a send-keys paste mid-turn is queued by agy as the next message, which is how
  web steering works.

Attachment note: tmux input takes plain text, so an image/file attachment on a
web turn is reduced to its text part (any prose the user typed). Inline image/
file bytes are not forwarded to agy through this path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from omnigent.antigravity_native_bridge import (
    ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR,
    ANTIGRAVITY_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    AntigravityNativeBridgeState,
    inject_user_message_via_tui,
    is_placeholder_conversation_id,
    read_bridge_state,
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

# How long run_turn waits for the bridge state file to appear on the first turn
# (agy cold-starts under tmux and the forwarder seeds/updates state). Mirrors the
# codex executor's one-second-poll-up-to-60s contract.
_STATE_WAIT_ATTEMPTS = 60
_STATE_WAIT_INTERVAL_S = 1.0


class AntigravityNativeExecutor(Executor):
    """
    Harness-side executor for ``omnigent antigravity`` web UI turns.

    Types the latest web/mobile user message into the running agy TUI over tmux;
    agy's reply is mirrored back by the transcript forwarder.

    :param bridge_dir: Optional bridge directory override. ``None``
        reads :data:`ANTIGRAVITY_NATIVE_BRIDGE_DIR_ENV_VAR`.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        # Serializes _inject so a concurrent run_turn (initiating message) and
        # enqueue_session_message (mid-turn steer, live message queue) don't type
        # into the agy TUI at once or deliver out of order. Mirrors the codex
        # native executor's _inject_lock.
        self._inject_lock = asyncio.Lock()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — assistant output is emitted by the transcript forwarder."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — a mid-turn send-keys paste is queued by agy as the next message."""
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """
        Steer an active native Antigravity turn by injecting another message.

        agy queues a send-keys paste delivered while a turn is running as the next
        user turn in the same conversation, so mid-turn web steering uses the exact
        same TUI injection path as :meth:`run_turn` (verified against agy 1.0.10).

        :param session_key: Adapter session key. Unused; the native bridge is
            per conversation.
        :param content: User-supplied content (string or content blocks).
        :returns: ``True`` when agy accepted the steering message, ``False``
            when there was no text to send or injection failed.
        """
        del session_key
        text = _content_to_text(content)
        if not text:
            return False
        outcome = await self._inject(text)
        return outcome is None

    async def interrupt_session(self, session_key: str) -> bool:
        """
        Interrupt the active native Antigravity turn.

        :param session_key: Adapter session key. Unused; the native bridge is
            per conversation.
        :returns: ``False`` — no agy connect-RPC cancel/interrupt method is
            verified yet.
        """
        # TODO(antigravity interrupt): agy's LanguageServerService has no
        # confirmed cancel/interrupt RPC (the spike validated Heartbeat,
        # SendAgentMessage, GetConversationMetadata; a cancel method was not
        # cracked). Wire a real interrupt once such an RPC is identified rather
        # than faking success. Returning False keeps the contract honest.
        del session_key
        return False

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Type the latest web/mobile user message into the running agy TUI.

        Delivers the message via tmux send-keys
        (:func:`omnigent.antigravity_native_bridge.inject_user_message_via_tui`),
        which agy records as a real ``USER_INPUT`` turn; on the first turn it then
        waits briefly for the forwarder to discover agy's freshly-minted
        conversation id. The assistant reply is mirrored back by the transcript
        forwarder, so this yields a single :class:`TurnComplete` with no text on
        success (never a fabricated reply). On any failure it yields one
        :class:`ExecutorError`.

        :param messages: Conversation history in executor message shape; the
            latest user message is injected.
        :param tools: Tool schemas from Omnigent. Ignored; native agy owns its
            own tool surface.
        :param system_prompt: System prompt from the agent spec. Ignored; the
            native conversation was created by the wrapper.
        :param config: Per-turn executor config. Only ``reasoning_effort`` is
            read; it is validated against :data:`ANTIGRAVITY_EFFORTS` and an
            unsupported value surfaces as a non-retryable error. The validated
            effort is informational — agy's model selection determines the
            actual thinking budget on the agy side and cannot be overridden
            from this TUI write path.
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
        text = _latest_user_text(messages)
        if not text:
            yield ExecutorError(message="Antigravity native turn had no user text to send")
            return
        outcome = await self._inject(text)
        if outcome is not None:
            yield ExecutorError(message=outcome)
        else:
            yield TurnComplete(response=None)

    async def _inject(self, text: str) -> str | None:
        """
        Type one message into the agy TUI; confirm the conversation on turn one.

        Shared by :meth:`run_turn` (initiating message) and
        :meth:`enqueue_session_message` (mid-turn steering): agy records either as
        a ``USER_INPUT`` turn when delivered through the TUI, so the two need no
        special-casing. Delivery is always tmux send-keys
        (:func:`omnigent.antigravity_native_bridge.inject_user_message_via_tui`) —
        never connect-RPC, which agy logs as a ``SYSTEM_MESSAGE`` the forwarder
        would not mirror (see the module docstring).

        :param text: User message text to deliver.
        :returns: ``None`` on success, or a human-readable error string
            describing why the message could not be delivered.
        """
        async with self._inject_lock:
            # The runner seeds bridge state before launching the terminal, so a
            # missing file means broken wiring (not a first turn) and is surfaced
            # as such.
            state = await asyncio.to_thread(read_bridge_state, self._bridge_dir)
            if state is None:
                return "Antigravity native bridge state is missing"
            if not _session_is_active(state.session_id, self._request_session_id):
                return "Antigravity native session is no longer active"
            # Deliver every turn by typing into the agy TUI (idle or mid-turn).
            try:
                await asyncio.to_thread(
                    inject_user_message_via_tui,
                    self._bridge_dir,
                    content=text,
                )
            except RuntimeError as exc:
                return f"Could not deliver the message to the agy terminal: {exc}"
            # On a fresh session the bridge still holds the launcher's
            # ``agy_conv_*`` placeholder: agy mints its real conversation id only
            # after processing this first turn. Wait for the forwarder to discover
            # and persist it — both to confirm the turn registered and to prime a
            # later resume. The caller holds ``_inject_lock``, so the next turn
            # cannot race ahead of this. Later turns already have the real id and
            # skip the wait.
            if is_placeholder_conversation_id(state.conversation_id):
                confirmed = await self._wait_for_state()
                if confirmed is None or is_placeholder_conversation_id(confirmed.conversation_id):
                    return (
                        "agy did not register a conversation after the first message "
                        "was delivered to its terminal (is the agy terminal attached "
                        "and running?)"
                    )
                _logger.info(
                    "antigravity native bootstrapped first turn via TUI: conversation=%s",
                    confirmed.conversation_id,
                )
            else:
                _logger.info(
                    "antigravity native delivered message via TUI: conversation=%s",
                    state.conversation_id,
                )
            return None

    async def _wait_for_state(self) -> AntigravityNativeBridgeState | None:
        """
        Read bridge state, polling until agy's REAL conversation id is known.

        Called by :meth:`_inject` after the FIRST turn is typed into the agy TUI
        (the bridge state still held the launcher's ``agy_conv_*`` placeholder):
        agy creates its conversation, then the forwarder discovers agy's real id
        and overwrites the placeholder in bridge state. This polls until that real
        id appears — confirming agy accepted the turn — so it doubles as the
        first-turn success signal. Subsequent turns read the real id immediately
        (no placeholder), so this is not on their path.

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
    Return whether this harness may inject into the native conversation.

    :param session_id: Session id from bridge state.
    :param request_session_id: Session id from harness spawn env.
    :returns: ``True`` when injection is allowed.
    """
    return request_session_id is None or request_session_id == session_id


def _latest_user_text(messages: list[Message]) -> str:
    """
    Extract the latest user message's text from the executor message list.

    :param messages: Executor message list.
    :returns: The user's text (string + content-block shapes flattened), or
        ``""`` when there is no user text to send.
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"))
    return ""


def _content_to_text(content: EnqueuedContent) -> str:
    """
    Flatten executor message content into plain text for the agy TUI.

    tmux input carries only text, so this extracts the textual parts and drops
    attachments. A plain string passes through. A list of content blocks
    contributes every ``input_text`` / ``text`` block, joined by newlines;
    ``input_image`` / ``input_file`` blocks are skipped (their bytes cannot be
    typed into the TUI — at minimum the typed text is sent).

    :param content: Message content — a string, a list of content blocks like
        ``{"type": "input_text", "text": "..."}``, or other.
    :returns: The flattened text, stripped of leading/trailing whitespace, or
        ``""`` when no text is present.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"input_text", "text"}:
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts).strip()
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=True)
