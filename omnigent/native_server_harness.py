"""Shared :class:`Executor` base for native-server harnesses.

The runner owns the native server + SSE/WS forwarder; this executor is the
harness-side seam that injects web turns over a
:class:`~omnigent.native_server_transport.NativeServerTransport`. It is
deliberately thin and transport-agnostic — the same orchestration drives
both codex-native (WS JSON-RPC) and opencode-native (HTTP + SSE):

- ``run_turn`` resolves the native session id from bridge state (briefly
  polling on first turn while the runner boots the server), builds a
  :class:`NativePrompt` from the latest user message, injects it via the
  transport, and yields ``TurnComplete`` — streaming is the forwarder's
  job, matching codex-native's injection/completion split.
- ``interrupt_session`` and ``enqueue_session_message`` route through the
  transport's ``abort`` / ``send_prompt``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

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
from omnigent.native_server_transport import NativePrompt, NativeServerTransport

_logger = logging.getLogger(__name__)

# Resolve the native session id from bridge state (``None`` until ready).
SessionResolver = Callable[[], Awaitable[str | None]]
# Build a :class:`NativePrompt` from message content.
PromptBuilder = Callable[[Any], NativePrompt | None]


class NativeServerHarness(Executor):
    """
    Transport-driven executor for native-server harnesses.

    :param harness_id: Canonical harness id, e.g. ``"opencode-native"`` (used
        in harness error messages).
    :param supports_enqueue: Whether the harness supports mid-turn enqueue
        (steer-or-queue); drives :meth:`supports_live_message_queue`.
    :param transport: The transport to inject turns over.
    :param resolve_session_id: Async callable returning the native session
        id (or ``None`` until the runner has booted the server).
    :param build_prompt: Callable turning message content into a
        :class:`NativePrompt` (``None`` when there is nothing to send).
    :param boot_poll_attempts: Times to poll for the session id on the
        first turn before giving up.
    :param boot_poll_delay: Seconds between boot polls.
    """

    def __init__(
        self,
        *,
        harness_id: str,
        supports_enqueue: bool,
        transport: NativeServerTransport,
        resolve_session_id: SessionResolver,
        build_prompt: PromptBuilder,
        boot_poll_attempts: int = 60,
        boot_poll_delay: float = 1.0,
    ) -> None:
        self._harness_id = harness_id
        self._supports_enqueue = supports_enqueue
        self.transport = transport
        self._resolve_session_id = resolve_session_id
        self._build_prompt = build_prompt
        self._boot_poll_attempts = boot_poll_attempts
        self._boot_poll_delay = boot_poll_delay
        # Serialize injection (run_turn vs enqueue) against the one cached
        # executor instance, mirroring the codex-native inject lock.
        self._inject_lock = asyncio.Lock()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — the runner-side forwarder emits output."""
        return False

    def handles_tools_internally(self) -> bool:
        """:returns: ``True`` — the native server runs its own tools."""
        return True

    def supports_live_message_queue(self) -> bool:
        """:returns: Whether the harness supports mid-turn enqueue."""
        return self._supports_enqueue

    async def _await_session_id(self) -> str | None:
        """
        Resolve the native session id, polling while the server boots.

        :returns: The native session id, or ``None`` if it never appears.
        """
        session_id = await self._resolve_session_id()
        if session_id is not None:
            return session_id
        for _ in range(self._boot_poll_attempts):
            await asyncio.sleep(self._boot_poll_delay)
            session_id = await self._resolve_session_id()
            if session_id is not None:
                return session_id
        return None

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Inject the latest user message into the native session.

        :param messages: Conversation history; the latest user message is
            delivered.
        :param tools: Omnigent tool schemas (ignored — native owns tools).
        :param system_prompt: Agent system prompt (ignored — set at
            session creation).
        :param config: Per-turn config (model override applied if present).
        :returns: Async iterator yielding one terminal event.
        """
        del tools, system_prompt
        prompt = _latest_user_prompt(messages, self._build_prompt)
        if prompt is None or prompt.is_empty():
            yield ExecutorError(message=f"{self._harness_id} turn had no user input to send")
            return
        if config is not None and config.model and not prompt.model:
            prompt = _with_model(prompt, config.model)
        error_msg: str | None = None
        async with self._inject_lock:
            session_id = await self._await_session_id()
            if session_id is None:
                error_msg = f"{self._harness_id} bridge state is missing"
            else:
                try:
                    await self.transport.send_prompt(session_id, prompt)
                except Exception as exc:  # noqa: BLE001 - converted to a harness error event.
                    error_msg = f"{self._harness_id} executor error: {exc}"
        if error_msg is not None:
            yield ExecutorError(message=error_msg)
        else:
            yield TurnComplete(response=None)

    async def interrupt_session(self, session_key: str) -> bool:
        """
        Abort the active native turn.

        :param session_key: Adapter session key (unused; bridge is
            per-conversation).
        :returns: ``True`` when an abort was issued.
        """
        del session_key
        session_id = await self._resolve_session_id()
        if session_id is None:
            return False
        try:
            return await self.transport.abort(session_id)
        except Exception:  # noqa: BLE001 - interruption is best effort.
            _logger.warning("%s abort failed", self._harness_id, exc_info=True)
            return False

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """
        Inject a mid-session message (steer-or-queue).

        OpenCode has no live-steer endpoint, so the message is admitted as
        a new prompt; the native server's own queue promotes it when the
        active turn finishes.

        :param session_key: Adapter session key (unused).
        :param content: User-supplied content.
        :returns: ``True`` when the message was admitted.
        """
        del session_key
        prompt = self._build_prompt(content)
        if prompt is None or prompt.is_empty():
            return False
        async with self._inject_lock:
            session_id = await self._resolve_session_id()
            if session_id is None:
                return False
            try:
                await self.transport.send_prompt(session_id, prompt)
            except Exception:  # noqa: BLE001 - enqueue is best effort.
                _logger.warning("%s enqueue failed", self._harness_id, exc_info=True)
                return False
        return True


def _latest_user_prompt(
    messages: list[Message], build_prompt: PromptBuilder
) -> NativePrompt | None:
    """
    Build a :class:`NativePrompt` from the latest user message.

    :param messages: Executor message list.
    :param build_prompt: Content → prompt builder.
    :returns: The prompt, or ``None`` when there is no user content.
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            return build_prompt(message.get("content"))
    return None


def _with_model(prompt: NativePrompt, model: str) -> NativePrompt:
    """
    Return a copy of *prompt* with *model* applied.

    :param prompt: The prompt to copy.
    :param model: Model id to pin.
    :returns: A new prompt carrying the model.
    """
    import dataclasses

    return dataclasses.replace(prompt, model=model)
