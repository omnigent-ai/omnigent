"""Executor that bridges Omnigent messages into a native Pi TUI."""

from __future__ import annotations

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
from omnigent.inner.native_attachments import materialize_attachment
from omnigent.pi_native_bridge import (
    PI_NATIVE_BRIDGE_DIR_ENV_VAR,
    PI_NATIVE_REQUEST_SESSION_ID_ENV_VAR,
    enqueue_user_message,
    refresh_config_auth_headers,
)


class PiNativeExecutor(Executor):
    """
    Harness-side executor for ``omnigent pi`` web UI turns.

    The native Pi process is already running in the session terminal with
    the Omnigent Pi extension loaded. Each turn queues the latest user
    message into the bridge inbox; the extension consumes it and calls
    ``pi.sendUserMessage`` inside the TUI process.

    Policy enforcement for native Pi tool calls is handled entirely by the
    extension: on each ``tool_call`` event the extension POSTs to
    ``POST /v1/sessions/{sessionId}/policies/evaluate`` using the server URL
    and auth headers from its config file. This bypasses the turn-scoped
    ``_policy_evaluator`` round-trip (which would fail because
    ``_current_ctx`` is cleared before Pi ever processes the message) and
    instead routes directly through the same session-level HTTP endpoint that
    the Claude Code native hook uses.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or _bridge_dir_from_env()
        self._request_session_id = _request_session_id_from_env()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` because output is emitted by the Pi extension."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``True`` because messages can be queued for the extension."""
        return True

    async def enqueue_session_message(self, session_key: str, content: Any) -> bool:
        """
        Queue a live steering message for the resident Pi extension.

        :param session_key: Adapter session key. Unused; the bridge is
            per conversation.
        :param content: User-supplied content.
        :returns: ``True`` when the message was queued.
        """
        del session_key
        text = _content_to_text(content, self._bridge_dir)
        if not text:
            return False
        self._refresh_auth_headers()
        enqueue_user_message(self._bridge_dir, text)
        return True

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Queue the latest user message for Pi.

        :param messages: Conversation history in executor message shape.
        :param tools: Tool schemas from Omnigent. Ignored for now; native
            Pi owns its configured tool surface.
        :param system_prompt: System prompt from the agent spec. Ignored
            because the native Pi terminal controls its own prompt/settings.
        :param config: Per-turn executor config. Unused.
        :yields: :class:`TurnComplete` after the input was queued, or an
            :class:`ExecutorError` when no user text can be sent.
        """
        del tools, system_prompt, config
        text = _latest_user_text(messages, self._bridge_dir)
        if not text:
            yield ExecutorError(message="Pi native turn had no user text to send")
            return
        self._refresh_auth_headers()
        enqueue_user_message(self._bridge_dir, text)
        yield TurnComplete(response=None)

    def _refresh_auth_headers(self) -> None:
        """
        Re-mint the extension's bearer so a long session survives token expiry.

        The bearer baked into ``config.json`` at launch dies with the ~1h
        Databricks OAuth lifetime, and the resident extension re-reads the
        config per request (``freshAuthHeaders``). Refreshing it at each turn
        keeps its policy/MCP POSTs authenticated instead of failing closed —
        the same outcome the refresh-capable runtime auth and the native
        policy hooks already get via re-mint. Minting runs in-runner through
        the same factory those use. Best-effort: any failure (no factory in
        local/unauthenticated runs, transient SDK error) leaves the existing
        headers in place rather than blocking the turn.

        ponytail: refreshes per turn, so a *single* turn running past ~1h
        still risks expiry mid-turn; add a background refresh task keyed to
        the pi terminal if that case ever bites.
        """
        try:
            from omnigent.runner._entry import _make_auth_token_factory

            factory = _make_auth_token_factory()
            token = factory() if factory is not None else None
            if token:
                refresh_config_auth_headers(self._bridge_dir, {"Authorization": f"Bearer {token}"})
        except Exception:  # noqa: BLE001 — best-effort refresh; never block a turn
            pass


def _bridge_dir_from_env() -> Path:
    """
    Resolve the native Pi bridge directory from harness spawn env.

    :returns: Bridge directory path.
    :raises RuntimeError: If the env var is missing.
    """
    raw = os.environ.get(PI_NATIVE_BRIDGE_DIR_ENV_VAR, "").strip()
    if not raw:
        raise RuntimeError(f"{PI_NATIVE_BRIDGE_DIR_ENV_VAR} is required for pi-native harness")
    return Path(raw)


def _request_session_id_from_env() -> str | None:
    """
    Resolve the Omnigent session id that requested this harness process.

    :returns: Omnigent session id, e.g. ``"conv_abc123"``, or ``None``.
    """
    raw = os.environ.get(PI_NATIVE_REQUEST_SESSION_ID_ENV_VAR, "").strip()
    return raw or None


def _latest_user_text(messages: list[Message], bridge_dir: Path) -> str:
    """
    Return the latest user text from executor messages.

    :param messages: Conversation history in executor message shape.
    :param bridge_dir: Bridge directory for materializing attachments.
    :returns: Plain text content, or ``""`` when no user text is present.
    """
    for message in reversed(messages):
        if message.get("role") == "user":
            return _content_to_text(message.get("content"), bridge_dir)
    return ""


def _content_to_text(content: Any, bridge_dir: Path) -> str:
    """
    Normalize executor content into plain text for Pi.

    Text blocks are extracted directly. Image/file blocks are materialized
    to the bridge directory and referenced by path so Pi can inspect them
    with its native filesystem tools.
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
                    attachment_lines.append(f"[Attached: {path}]")
        return "\n\n".join([*attachment_lines, *text_parts])
    if content is None:
        return ""
    return str(content)
