"""Executor that bridges Omnigent web-chat turns into the native qwen TUI.

It does not launch ``qwen`` — the ``omnigent qwen`` wrapper already launched the
interactive ``qwen`` TUI in the session terminal (with ``--input-file`` /
``--json-file``). Each web-UI turn appends a ``{"type":"submit",...}`` line to
that input file, which qwen's ``RemoteInputWatcher`` routes through the same
``submitQuery`` path the keyboard uses, so the message appears in the running TUI
(and, since the web UI embeds the pane, in both surfaces). Output is
terminal-originated; the embedded terminal renders it live and
:mod:`omnigent.qwen_native_forwarder` mirrors the JSON event stream.

Unlike goose-/cursor-native (tmux ``send-keys``), injection here is an atomic
file append — no settle-detection, paste-commit polling, or draft-clearing. See
``docs/QWEN_NATIVE_DESIGN.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
)
from omnigent.qwen_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    submit_user_message,
    wait_for_ready,
)

logger = logging.getLogger(__name__)


class QwenNativeExecutor(Executor):
    """Harness-side executor for ``omnigent qwen`` web-UI turns.

    Appends each web-UI message as a ``submit`` command to the running qwen TUI's
    input file. Does not stream output (the embedded terminal shows it, and the
    forwarder mirrors the JSON event stream); accepts mid-turn steering.

    :param bridge_dir: Optional bridge dir override; ``None`` reads
        :data:`BRIDGE_DIR_ENV_VAR` from the harness spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        # Serializes appends to the shared input file: run_turn (initiating
        # message) and enqueue_session_message (steering) run concurrently
        # against one cached executor. Each append is a single line write, but
        # the lock keeps their ordering deterministic.
        self._inject_lock = asyncio.Lock()
        # Latched once qwen has booted its input watcher (see _ensure_ready).
        # Guards the boot-order race where the first turn fires while qwen is
        # still starting up and would otherwise be dropped.
        self._ready = False

    async def _ensure_ready(self) -> None:
        """Block (once) until qwen's input watcher is active before the first append.

        qwen takes the input file's size as its read offset when it starts
        watching, during boot. Appending before that drops the message. We wait
        for qwen's first ``system`` event on the events stream — emitted after the
        watcher is up — so the offset is taken on the still-empty input file.

        Only latches ``_ready`` on a confirmed-ready result, so a warm session
        never re-blocks but a *timeout* re-checks on the next turn (qwen is
        almost certainly up by then). On timeout we log and let the caller submit
        anyway — best-effort beats hanging the turn — but the warning makes the
        rare dropped-first-message failure diagnosable rather than silent.
        """
        if self._ready:
            return
        ready = await asyncio.to_thread(wait_for_ready, self._bridge_dir)
        if ready:
            self._ready = True
        else:
            logger.warning(
                "qwen-native readiness gate timed out for %s; submitting anyway — "
                "the first message may be dropped if qwen's input watcher is not up "
                "yet (will re-check next turn)",
                self._bridge_dir,
            )

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is shown by the embedded terminal, not this executor."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — messages can be injected mid-turn (steering)."""
        return True

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        """Append a live steering message to the qwen TUI input file."""
        del session_key
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        try:
            await self._ensure_ready()
            async with self._inject_lock:
                await asyncio.to_thread(submit_user_message, self._bridge_dir, content=text)
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
        """Append the latest web-UI user message to the qwen TUI input file."""
        del tools, system_prompt, config
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="qwen native turn had no user text to send")
            return
        try:
            await self._ensure_ready()
            async with self._inject_lock:
                await asyncio.to_thread(submit_user_message, self._bridge_dir, content=text)
        except RuntimeError as exc:
            yield ExecutorError(message=str(exc))
            return
        yield TurnComplete(response=None)


def _bridge_dir_from_env() -> Path:
    """Resolve the qwen-native bridge dir from the harness spawn env."""
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for the qwen-native harness")
    return Path(raw)


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """Return the latest user message's text (attachments materialized to disk)."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: Any, bridge_dir: Path) -> str:
    """Normalize executor content into text the qwen TUI receives.

    Text blocks are extracted directly. Image/file blocks carrying a base64 data
    URI are materialized to the bridge dir and referenced by absolute path
    (``[Attached: <path>]``) so qwen can open them with its tools — otherwise
    web-UI attachments are silently dropped. Mirrors goose-/cursor-native.
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
