"""Executor that bridges Omnigent web-chat turns into Claude Code."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from omnigent.claude_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    REQUEST_SESSION_ID_ENV_VAR,
    inject_user_message,
    read_active_session_id,
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

_logger = logging.getLogger(__name__)


class ClaudeNativeExecutor(Executor):
    """
    Harness-side executor for ``omnigent claude`` web UI turns.

    It does not launch Claude itself. The native wrapper has already
    launched Claude Code in the session terminal with the Omnigent
    bridge MCP server and hooks enabled. Each executor turn only
    injects the latest web UI user message into the same tmux pane
    Claude is attached to (via ``tmux send-keys``). User-visible
    transcript items are mirrored by the always-on transcript
    forwarder, so every rendered chat item has terminal provenance.

    :param bridge_dir: Optional bridge directory override. ``None``
        reads :data:`BRIDGE_DIR_ENV_VAR` from the harness spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()
        # Serializes every write to the shared tmux pane. ``run_turn``
        # (the initiating message) and ``enqueue_session_message``
        # (mid-turn steering) run as concurrent tasks against this one
        # cached instance (the adapter keeps one executor per
        # conversation), and ``inject_user_message`` is not atomic — it
        # issues several ``tmux send-keys`` calls. Without this lock the
        # two paths interleave their keystrokes and combine messages
        # (e.g. "1" and "2" land as a single "12" prompt). See
        # designs/NATIVE_INJECTION_SERIALIZATION.md. Relies on the
        # adapter caching one executor per conversation; per-turn
        # construction would regress the lock to per-turn scope.
        self._inject_lock = asyncio.Lock()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` because output is emitted by the transcript forwarder."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` because messages can be injected mid-turn."""
        return True

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        """
        Inject a live steering message into the Claude terminal.

        :param session_key: Adapter session key. The native bridge is
            per conversation, so this value is not used to route the
            keystrokes (one terminal per conversation).
        :param content: User-supplied content, usually a string.
        :returns: ``True`` when tmux accepted the keystrokes.
        """
        del session_key
        if not _session_is_active(self._bridge_dir, self._request_session_id):
            return False
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        try:
            async with self._inject_lock:
                await asyncio.to_thread(
                    inject_user_message,
                    self._bridge_dir,
                    content=text,
                )
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
        """
        Send the latest user message to Claude's terminal.

        :param messages: Conversation history in executor message
            shape. The latest user message is delivered to Claude;
            prior history already lives in Claude Code's own session.
        :param tools: Tool schemas from Omnigent. Ignored here;
            Claude-native output/tool activity is terminal-originated
            and mirrored from Claude's transcript.
        :param system_prompt: System prompt from the agent spec. The
            native Claude Code terminal controls its own prompt/settings,
            so this is ignored.
        :param config: Per-turn executor config. Unused by this
            terminal-backed executor.
        :yields: :class:`TurnComplete` after the input was injected,
            or :class:`ExecutorError` on bridge failure.
        """
        del tools, system_prompt, config
        if not _session_is_active(self._bridge_dir, self._request_session_id):
            yield ExecutorError(
                message=(
                    "Claude native session is no longer active after /clear; "
                    "open the latest conversation and retry."
                )
            )
            return
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="Claude native turn had no user text to send")
            return
        from omnigent.runtime import telemetry

        # Span the tmux send-keys inject — the input half of the decoupled
        # native path. session.id is applied generically by the span processor
        # (the executor adapter binds session_scope for the turn), so there's
        # no per-call-site stamping here.
        try:
            with telemetry.span("claude_native.inject"):
                async with self._inject_lock:
                    await asyncio.to_thread(
                        inject_user_message,
                        self._bridge_dir,
                        content=text,
                    )
        except RuntimeError as exc:
            yield ExecutorError(message=str(exc))
            return
        yield TurnComplete(response=None)


def _bridge_dir_from_env() -> Path:
    """
    Resolve the native bridge directory from harness spawn env.

    :returns: Bridge directory path.
    :raises RuntimeError: If :data:`BRIDGE_DIR_ENV_VAR` is missing.
    """
    raw = os.environ.get(BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{BRIDGE_DIR_ENV_VAR} is required for claude-native harness")
    return Path(raw)


def _request_session_id_from_env() -> str | None:
    """
    Resolve the Omnigent session id that requested this harness process.

    :returns: Omnigent session id, e.g. ``"conv_abc123"``, or ``None`` when
        the spawn env predates active-session validation.
    """
    raw = os.environ.get(REQUEST_SESSION_ID_ENV_VAR, "").strip()
    return raw or None


def _session_is_active(bridge_dir: Path, request_session_id: str | None) -> bool:
    """
    Return whether a request may inject into the shared Claude pane.

    :param bridge_dir: Native bridge directory.
    :param request_session_id: Omnigent session id from
        :data:`REQUEST_SESSION_ID_ENV_VAR`, e.g. ``"conv_abc123"``.
        ``None`` preserves old harness spawns that lack the guard env.
    :returns: ``True`` when injection is allowed.
    """
    if request_session_id is None:
        return True
    active_session_id = read_active_session_id(bridge_dir)
    return active_session_id is None or active_session_id == request_session_id


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """
    Return the latest user text from executor messages.

    Multimodal content blocks (images, files) are materialized to the
    bridge directory and referenced by path in the returned text so
    Claude Code can read them via its Read tool.

    :param messages: Conversation history in executor message shape.
    :param bridge_dir: Bridge directory path for writing attachment
        files, e.g. ``Path("/tmp/omnigent/claude-native/<digest>")``.
    :returns: Concatenated latest user message text, or ``""`` when
        no user text is present.
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: Any, bridge_dir: Path) -> str:
    """
    Normalize executor content into plain text.

    Text blocks are extracted directly. Multimodal blocks
    (``input_image``, ``input_file``) that carry resolved base64 data
    URIs are decoded to files in the bridge directory and referenced
    by path so Claude Code can view them with its Read tool.

    :param content: Message content, e.g. a string or a list of
        ``{"type": "input_text", "text": "..."}`` blocks. May also
        contain ``input_image`` blocks with an ``image_url`` data URI
        or ``input_file`` blocks with a ``file_data`` data URI.
    :param bridge_dir: Bridge directory path for writing attachment
        files, e.g. ``Path("/tmp/omnigent/claude-native/<digest>")``.
    :returns: Plain text content with file-path references prepended
        for any materialized attachments.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        attachment_lines: list[str] = []
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")
            if block_type == "input_text":
                text = block.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
            elif block_type in ("input_image", "input_file"):
                path = materialize_attachment(block, bridge_dir)
                if path is not None:
                    # Marker format is load-bearing: the transcript mirrors
                    # this text back as the durable user message, and title
                    # seeding strips lines matching _ATTACHMENT_MARKER_RE in
                    # omnigent/entities/conversation.py. Keep in sync.
                    attachment_lines.append(f"[Attached: {path}]")
        parts = attachment_lines + text_parts
        return "\n\n".join(parts)
    return ""
