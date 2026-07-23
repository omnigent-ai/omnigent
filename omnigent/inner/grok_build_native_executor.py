"""Executor that bridges Omnigent web-chat turns into the native Grok Build TUI.

It does not launch ``grok`` — the ``omnigent grok-build`` wrapper already launched
the interactive ``grok`` TUI in the session terminal. Each web-UI turn injects
the latest user message into that same tmux pane (bracketed paste + Enter), so the
message appears in the running Grok Build TUI (and, since the web UI embeds the
pane, in both surfaces). Output is terminal-originated; the embedded terminal
renders it live. Mirrors
:class:`omnigent.inner.goose_native_executor.GooseNativeExecutor`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.grok_build_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    inject_interrupt,
    inject_user_message,
)
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


class GrokBuildNativeExecutor(Executor):
    """Harness-side executor for ``omnigent grok-build`` web-UI turns.

    Injects each web-UI message into the running Grok Build TUI's tmux pane. Does
    not stream output (the embedded terminal shows it); accepts mid-turn steering.

    :param bridge_dir: Optional bridge dir override; ``None`` reads
        :data:`BRIDGE_DIR_ENV_VAR` from the harness spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._inject_lock = asyncio.Lock()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is shown by the embedded terminal, not this executor."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — messages can be injected mid-turn (steering)."""
        return True

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        """Inject a live steering message into the Grok Build terminal."""
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
        """Inject the latest web-UI user message into the Grok Build TUI pane."""
        del tools, system_prompt, config
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="grok build native turn had no user text to send")
            return
        try:
            async with self._inject_lock:
                await asyncio.to_thread(inject_user_message, self._bridge_dir, content=text)
        except RuntimeError as exc:
            yield ExecutorError(message=str(exc))
            return
        yield TurnComplete(response=None)

    async def interrupt_session(self, session_key: str) -> bool:
        """Cancel the in-flight Grok Build turn by sending Escape to the TUI pane."""
        del session_key
        try:
            await asyncio.to_thread(inject_interrupt, self._bridge_dir)
        except RuntimeError:
            return False
        return True


def _bridge_dir_from_env() -> Path:
    """Resolve the grok-build-native bridge dir from the harness spawn env."""
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for the grok-build-native harness")
    return Path(raw)


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """Return the latest user message's text (attachments materialized to disk)."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: Any, bridge_dir: Path) -> str:
    """Normalize executor content into text the Grok Build TUI receives.

    Text blocks are extracted directly. Image/file blocks carrying a base64 data
    URI are materialized to the bridge dir and referenced by absolute path
    (``[Attached: <path>]``) so Grok Build can open them with its tools.
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
