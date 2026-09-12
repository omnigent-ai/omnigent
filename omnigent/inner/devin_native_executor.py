"""Executor that bridges Omnigent web-chat turns into the native Devin TUI."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

from omnigent.harnesses.devin_native.bridge import (
    DEVIN_NATIVE_BRIDGE_DIR_ENV_VAR,
    inject_model_command,
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


class DevinNativeExecutor(Executor):
    """Harness-side executor for ``omnigent devin`` web-UI turns."""

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._inject_lock = asyncio.Lock()
        # The model the pane is currently on, so a routed turn only types
        # ``/model`` when the model actually changes. ``None`` = not yet known;
        # the launch ``--model`` already put the pane on the spec's model.
        self._applied_model: str | None = None

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is shown by the embedded terminal."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` — Devin accepts steering input mid-turn.

        Devin's composer stays writable while a turn runs (its placeholder
        changes to "Guide Devin while it works"), so a queued message steers the
        running turn rather than waiting for it to finish.
        """
        return True

    async def enqueue_session_message(self, session_key: str, content: EnqueuedContent) -> bool:
        """Inject a live steering message into the Devin terminal."""
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
        """Inject the latest web-UI user message into the Devin TUI pane.

        When intelligent routing picks a model for this turn it arrives as
        ``config.model``; the ``/model`` switch and the message injection are
        applied under one lock so the pane cannot interleave them.

        :param config: Per-turn executor config. Only ``config.model`` is used.
        """
        del tools, system_prompt
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="devin native turn had no user text to send")
            return
        wanted_model = config.model if config is not None else None
        try:
            async with self._inject_lock:
                if wanted_model and wanted_model != self._applied_model:
                    await asyncio.to_thread(
                        inject_model_command, self._bridge_dir, model=wanted_model
                    )
                    self._applied_model = wanted_model
                await asyncio.to_thread(inject_user_message, self._bridge_dir, content=text)
        except RuntimeError as exc:
            yield ExecutorError(message=describe_exception(exc))
            return
        yield TurnComplete(response=None)


def _bridge_dir_from_env() -> Path:
    """Resolve the devin-native bridge dir from the harness spawn env."""
    raw = os.environ.get(DEVIN_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(
            f"{DEVIN_NATIVE_BRIDGE_DIR_ENV_VAR} is required for the devin-native harness"
        )
    return Path(raw)


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """Return the latest user message's text."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: EnqueuedContent, bridge_dir: Path) -> str:
    """Normalize executor content into text the Devin TUI receives.

    Images and files become ``@``-style path references — Devin resolves a path
    in the prompt against the workspace and attaches it, which is how the TUI's
    own file-attach affordance works.
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
