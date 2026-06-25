"""Executor that bridges Omnigent web-chat turns into the native Goose TUI.

It does not launch ``goose`` — the ``omnigent goose`` wrapper already launched
the interactive ``goose session`` TUI in the session terminal. Each web-UI turn
injects the latest user message into that same tmux pane (bracketed paste +
Enter), so the message appears in the running Goose TUI (and, since the web UI
embeds the pane, in both surfaces). Output is terminal-originated; the embedded
terminal renders it live and the forwarder mirrors the transcript.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.cursor_native_bridge import (
    clear_fork_preamble,
    read_fork_preamble,
    wrap_fork_preamble,
)
from omnigent.goose_native_bridge import BRIDGE_DIR_ENV_VAR, inject_user_message
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
)

logger = logging.getLogger(__name__)


class GooseNativeExecutor(Executor):
    """Harness-side executor for ``omnigent goose`` web-UI turns.

    Injects each web-UI message into the running Goose TUI's tmux pane. Does not
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

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is shown by the embedded terminal, not this executor."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — messages can be injected mid-turn (steering)."""
        return True

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        """Inject a live steering message into the Goose terminal."""
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
        """Inject the latest web-UI user message into the Goose TUI pane."""
        del tools, system_prompt, config
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="goose native turn had no user text to send")
            return
        # A fork into goose carries history as a text preamble: a fresh
        # ``goose session --name <fork>`` has no prior history of its own, so the
        # runner stashed the copied turns and we prepend them to the FIRST
        # injected message. READ it here but only CLEAR after a successful inject
        # (a failed/retried turn must keep the history). The forwarder strips the
        # sentinel block when mirroring this turn back, so it isn't duplicated.
        preamble = read_fork_preamble(self._bridge_dir)
        if preamble:
            text = wrap_fork_preamble(preamble, text)
        try:
            async with self._inject_lock:
                await asyncio.to_thread(inject_user_message, self._bridge_dir, content=text)
        except RuntimeError as exc:
            yield ExecutorError(message=str(exc))
            return
        if preamble:
            clear_fork_preamble(self._bridge_dir)
        yield TurnComplete(response=None)


def _bridge_dir_from_env() -> Path:
    """Resolve the goose-native bridge dir from the harness spawn env."""
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for the goose-native harness")
    return Path(raw)


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """Return the latest user message's text (attachments materialized to disk)."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: Any, bridge_dir: Path) -> str:
    """Normalize executor content into text the Goose TUI receives.

    Text blocks are extracted directly. Image/file blocks carrying a base64 data
    URI are materialized to the bridge dir and referenced by absolute path
    (``[Attached: <path>]``) so Goose can open them with its tools — otherwise
    web-UI attachments are silently dropped. Mirrors cursor-/claude-native.
    """
    if isinstance(content, str):
        return content
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
                if isinstance(text, str):
                    text_parts.append(text)
            elif block_type in ("input_image", "input_file"):
                path = materialize_attachment(block, bridge_dir)
                if path is not None:
                    attachment_lines.append(f"[Attached: {path}]")
        return "\n\n".join(attachment_lines + text_parts)
    return ""
