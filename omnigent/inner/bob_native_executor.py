"""Executor that delivers Omnigent web-chat turns into the native Bob Shell TUI.

It does not launch ``bob`` — the runner already launched ``bob chat`` in the
session terminal. Each web-UI turn pastes the latest user message into that
pane (see :func:`omnigent.harnesses.bob_native.bridge.inject_user_message`).
Bob's output is terminal-only: the web UI embeds the pane, and no transcript
is mirrored into the chat.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

from omnigent.harnesses.bob_native.bridge import BRIDGE_DIR_ENV_VAR, inject_user_message
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


class BobNativeExecutor(Executor):
    """Harness-side executor for ``omnigent bob`` web-UI turns.

    :param bridge_dir: Optional bridge dir override; ``None`` reads
        :data:`BRIDGE_DIR_ENV_VAR` from the harness spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        # run_turn and enqueue_session_message share one pane; delivery is
        # multi-step (paste, confirm, Enter), so serialize it.
        self._inject_lock = asyncio.Lock()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — Bob's output is shown by the embedded terminal."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — Bob's composer accepts mid-turn steering."""
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """Deliver a live steering message into the Bob terminal."""
        del session_key
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        try:
            async with self._inject_lock:
                await asyncio.to_thread(inject_user_message, self._bridge_dir, content=text)
        except RuntimeError:
            return False
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Deliver the latest web-UI user message into the Bob TUI pane."""
        del tools, system_prompt, config
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="bob native turn had no user text to send")
            return
        try:
            async with self._inject_lock:
                await asyncio.to_thread(inject_user_message, self._bridge_dir, content=text)
        except RuntimeError as exc:
            yield ExecutorError(message=describe_exception(exc))
            return
        yield TurnComplete(response=None)


def _bridge_dir_from_env() -> Path:
    """Resolve the bob-native bridge dir from the harness spawn env."""
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for the bob-native harness")
    return Path(raw)


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """Return the latest user message's text (attachments materialized to disk)."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: EnqueuedContent, bridge_dir: Path) -> str:
    """Normalize executor content into the text Bob receives.

    Image/file blocks are written to the bridge dir and referenced by absolute
    path (``[Attached: <path>]``) so Bob can open them with its own tools.
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
