"""Executor that bridges Omnigent web-chat turns into the native Kimi TUI.

It does not launch ``kimi`` — the ``omnigent kimi`` wrapper already
launched the interactive TUI in the session terminal. Each web-UI turn injects
the latest user message into that same tmux pane (bracketed paste + Enter), so
the message appears in the running Kimi TUI (and, since the web UI embeds the
pane, in both surfaces). Output is terminal-originated; the embedded terminal
renders it live.

This is a DIFFERENT executor from the headless :class:`~omnigent.inner.
kimi_executor.KimiExecutor`, which shells ``kimi -p … --output-format
stream-json`` per turn. This one types into a resident ``kimi`` TUI.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path

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
from omnigent.kimi_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    KimiApprovalPendingError,
    inject_user_message,
)
from omnigent.llms.errors import PermanentLLMError, RetryableLLMError

logger = logging.getLogger(__name__)
_STEERING_READY_TIMEOUT_S = 30.0
_MAX_APPROVAL_PENDING_RETRIES = 3


class KimiNativeExecutor(Executor):
    """Harness-side executor for ``omnigent kimi`` web-UI turns.

    Injects each web-UI message into the running Kimi TUI's tmux pane. Does not
    stream output (the embedded terminal shows it); accepts mid-turn steering.

    :param bridge_dir: Optional bridge dir override; ``None`` reads
        :data:`BRIDGE_DIR_ENV_VAR` from the harness spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        # Serializes writes to the shared tmux pane: run_turn (initiating
        # message) and enqueue_session_message (steering) run concurrently
        # against one cached executor, and injection is multi-step (clear +
        # paste + Enter) — without the lock their keystrokes interleave.
        self._inject_lock = asyncio.Lock()
        self._approval_pending_retries = 0

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is shown by the embedded terminal, not this executor."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — messages can be injected mid-turn (steering)."""
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """Inject a live steering message into the Kimi terminal."""
        del session_key
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        try:
            await self._inject_message(
                text, timeout_s=_STEERING_READY_TIMEOUT_S, turn_streaming=True
            )
        except RuntimeError as exc:
            logger.warning("Kimi native steering message was not delivered: %s", exc)
            return False
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Inject the latest web-UI user message into the Kimi TUI pane."""
        del tools, system_prompt, config
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="kimi native turn had no user text to send")
            return
        try:
            await self._inject_message(text)
        except KimiApprovalPendingError as exc:
            self._approval_pending_retries += 1
            if self._approval_pending_retries > _MAX_APPROVAL_PENDING_RETRIES:
                raise PermanentLLMError(str(exc), code="kimi_approval_pending") from exc
            # Semantic Omnigent errors preserve retry classification through the adapter.
            raise RetryableLLMError(str(exc), code="connection_error") from exc
        except RuntimeError as exc:
            yield ExecutorError(message=describe_exception(exc))
            return
        self._approval_pending_retries = 0
        yield TurnComplete(response=None)

    async def _inject_message(
        self,
        text: str,
        *,
        timeout_s: float | None = None,
        turn_streaming: bool = False,
    ) -> None:
        cancel_event = threading.Event()
        async with self._inject_lock:
            injection = asyncio.create_task(
                asyncio.to_thread(
                    inject_user_message,
                    self._bridge_dir,
                    content=text,
                    cancel_event=cancel_event,
                    turn_streaming=turn_streaming,
                    **({"timeout_s": timeout_s} if timeout_s is not None else {}),
                )
            )
            try:
                await asyncio.shield(injection)
            except asyncio.CancelledError:
                cancel_event.set()
                with contextlib.suppress(BaseException):
                    await injection
                raise


def _bridge_dir_from_env() -> Path:
    """Resolve the kimi-native bridge dir from the harness spawn env."""
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for the kimi-native harness")
    return Path(raw)


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """Return the latest user message's text (attachments materialized to disk)."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: EnqueuedContent, bridge_dir: Path) -> str:
    """Normalize executor content into text the Kimi TUI receives.

    Text blocks are extracted directly. Image/file blocks carrying a base64
    data URI are materialized to the bridge dir and referenced by absolute path
    (``[Attached: <path>]``) so kimi can open them with its Read tool —
    otherwise web-UI attachments are silently dropped. Mirrors claude-native.
    """
    if isinstance(content, str):
        return content
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
                if isinstance(text, str):
                    text_parts.append(text)
            elif block_type in ("input_image", "input_file"):
                attachment_lines.append(attachment_reference_line(block, bridge_dir))
        return "\n\n".join(attachment_lines + text_parts)
    return ""
