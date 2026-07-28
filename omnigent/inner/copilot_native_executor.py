"""Executor that bridges Omnigent messages into a native Copilot TUI thread."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.copilot_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    REQUEST_SESSION_ID_ENV_VAR,
    inject_interrupt,
    inject_model_command,
    inject_user_message,
    read_tmux_info,
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
from omnigent.inner.native_attachments import materialize_attachment


class CopilotNativeExecutor(Executor):
    """Harness-side executor for ``omnigent copilot`` web-UI turns."""

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        self._inject_lock = asyncio.Lock()

    def supports_streaming(self) -> bool:
        return False

    def supports_live_message_queue(self) -> bool:
        return True

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        del session_key
        if not _session_is_active(self._bridge_dir, self._request_session_id):
            return False
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        async with self._inject_lock:
            try:
                await asyncio.to_thread(inject_user_message, self._bridge_dir, content=text)
            except RuntimeError:
                return False
        return True

    async def interrupt_session(self, session_key: str) -> bool:
        del session_key
        if not _session_is_active(self._bridge_dir, self._request_session_id):
            return False
        try:
            await asyncio.to_thread(inject_interrupt, self._bridge_dir)
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
        del tools, system_prompt
        if not _session_is_active(self._bridge_dir, self._request_session_id):
            yield ExecutorError(message="Copilot native session is no longer active")
            return
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="Copilot native turn had no user text to send")
            return
        model = _model_from_config(config)
        async with self._inject_lock:
            if model:
                with contextlib.suppress(RuntimeError):
                    await asyncio.to_thread(inject_model_command, self._bridge_dir, model=model)
            try:
                await asyncio.to_thread(inject_user_message, self._bridge_dir, content=text)
            except RuntimeError as exc:
                yield ExecutorError(message=str(exc))
                return
        yield TurnComplete(response=None)


def _bridge_dir_from_env() -> Path:
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for copilot-native harness")
    return Path(raw)


def _request_session_id_from_env() -> str | None:
    raw = os.environ.get(REQUEST_SESSION_ID_ENV_VAR, "").strip()
    return raw or None


def _session_is_active(bridge_dir: Path, request_session_id: str | None) -> bool:
    if request_session_id is None:
        return True
    tmux_info = read_tmux_info(bridge_dir)
    return tmux_info is not None and tmux_info.get("session_id") == request_session_id


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: Any, bridge_dir: Path) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type in ("input_text", "text"):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif block_type in ("input_image", "input_file"):
                path = materialize_attachment(block, bridge_dir)
                if path is not None:
                    parts.append(f"[Attached: {path}]")
        return "\n\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _model_from_config(config: ExecutorConfig | None) -> str | None:
    if config is None:
        return None
    model = config.model
    return model if isinstance(model, str) and model else None
