"""Executor that bridges Omnigent web-chat turns into the native Command Code TUI.

It does not launch ``cmd`` — the ``omnigent commandcode`` wrapper already
launched the interactive TUI in the session terminal. Each web-UI turn injects
the latest user message into that same tmux pane (bracketed paste + Enter), so
the message appears in the running Command Code TUI (and, since the web UI
embeds the pane, in both surfaces). Output is terminal-originated; the embedded
terminal renders it live.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from omnigent.harnesses.commandcode_native.bridge import (
    BRIDGE_DIR_ENV_VAR,
    CommandCodeApprovalPendingError,
    inject_user_message,
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
    describe_exception,
)
from omnigent.llms.errors import PermanentLLMError, RetryableLLMError

logger = logging.getLogger(__name__)
_STEERING_READY_TIMEOUT_S = 30.0
_MAX_APPROVAL_PENDING_RETRIES = 3


class CommandCodeNativeExecutor(Executor):
    """Harness-side executor for ``omnigent commandcode`` web-UI turns.

    Injects each web-UI message into the running Command Code TUI's tmux pane.
    Does not stream output (the embedded terminal shows it); accepts mid-turn
    steering.

    :param bridge_dir: Optional bridge dir override; ``None`` reads
        :data:`BRIDGE_DIR_ENV_VAR` from the harness spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        # Serializes writes to the shared tmux pane: run_turn (initiating
        # message) and enqueue_session_message (steering) run concurrently
        # against one cached executor, and injection is multi-step
        # (paste + Enter) — without the lock their keystrokes interleave.
        self._inject_lock = asyncio.Lock()
        self._approval_pending_retries = 0

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is shown by the embedded terminal."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — messages can be injected mid-turn (steering)."""
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """Inject a live steering message into the Command Code terminal."""
        del session_key
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        try:
            await self._inject_message(
                text, timeout_s=_STEERING_READY_TIMEOUT_S, turn_streaming=True
            )
        except RuntimeError as exc:
            logger.warning("Command Code native steering message was not delivered: %s", exc)
            return False
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Inject the latest web-UI user message into the Command Code TUI pane."""
        del tools, system_prompt, config
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="commandcode native turn had no user text to send")
            return
        try:
            await self._inject_message(text)
        except CommandCodeApprovalPendingError as exc:
            self._approval_pending_retries += 1
            if self._approval_pending_retries > _MAX_APPROVAL_PENDING_RETRIES:
                raise PermanentLLMError(str(exc), code="commandcode_approval_pending") from exc
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
    """Resolve the commandcode-native bridge dir from the harness spawn env."""
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for the commandcode-native harness")
    return Path(raw)


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """Return the latest user message's text (attachments materialized to disk)."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: EnqueuedContent, bridge_dir: Path) -> str:
    """Normalize executor content into text the Command Code TUI receives."""
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
