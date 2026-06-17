"""CursorNativeExecutor: run agents through ``cursor-agent acp`` (the Cursor CLI).

This is the cursor-native harness: it drives the official Cursor CLI's Agent
Client Protocol server over stdio — the codex-native model, but stdio instead of
a WebSocket. One
:class:`~omnigent.inner.cursor_acp_client.CursorAcpClient` (one ``cursor-agent
acp`` subprocess) is kept per Omnigent conversation and reused turn to turn; each
``run_turn`` issues one ``session/prompt`` and translates the streamed
``session/update`` notifications into ExecutorEvents:

- ``agent_message_chunk`` → :class:`TextChunk`
- ``agent_thought_chunk`` → :class:`ReasoningChunk`
- ``tool_call`` / ``tool_call_update`` → :class:`ToolCallRequest` / :class:`ToolCallComplete`

Unlike the SDK ``cursor`` harness, auth is the ambient ``cursor-agent login``
(``$HOME/.cursor``) — no ``CURSOR_API_KEY`` is required.

Scope (PR1 — the core): text/reasoning/tool-call streaming and turn completion.
Deferred to later PRs: an MCP relay so the agent can call host ``sys_*`` tools,
an ACP ``session/request_permission`` → policy bridge, session resume via
``session/load``, and per-session ``$HOME`` isolation. ``tools`` is therefore
ignored here — cursor-agent runs its own built-in tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from omnigent.inner.cursor_acp_client import CursorAcpClient
from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
    classify_tool_result,
)

logger = logging.getLogger(__name__)


@dataclass
class _AcpSession:
    """Per-Omnigent-conversation ACP session state."""

    client: CursorAcpClient
    session_id: str
    has_sent_prompt: bool = False


class CursorNativeExecutor(Executor):
    """Execute agent turns via a persistent ``cursor-agent acp`` session."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        model: str | None = None,
        binary: str = "cursor-agent",
    ) -> None:
        """Create a CursorNativeExecutor.

        :param cwd: Working directory the agent operates in; ``None`` falls back
            to the process cwd.
        :param model: Reserved for a future ``session/new`` model pin; cursor-agent
            currently selects its configured default. Logged when set but ignored.
        :param binary: The ``cursor-agent`` executable name or path.
        """
        self._cwd = cwd
        self._model = model
        self._binary = binary
        self._sessions: dict[str, _AcpSession] = {}
        if model:
            logger.info(
                "CursorNativeExecutor: model %r requested; cursor-agent acp uses its "
                "configured default model in this build (model pin is a follow-up).",
                model,
            )

    def supports_streaming(self) -> bool:
        """:returns: ``True`` — ``session/update`` is streamed as it arrives."""
        return True

    def supports_tool_calling(self) -> bool:
        """:returns: ``True`` — cursor-agent runs its own built-in tools."""
        return True

    def handles_tools_internally(self) -> bool:
        """:returns: ``True`` — tool calls execute inside cursor-agent, not the runtime."""
        return True

    def supports_live_message_queue(self) -> bool:
        """:returns: ``False`` — ACP exposes no mid-turn steer (only session/cancel)."""
        return False

    def _session_key(self, messages: list[Message]) -> str:
        """Derive the per-conversation session key (mirrors :class:`CursorExecutor`)."""
        if messages:
            last = messages[-1]
            if last.get("session_id"):
                return str(last["session_id"])
            meta = last.get("metadata", {})
            if isinstance(meta, dict) and meta.get("session_id"):
                return str(meta["session_id"])
        return "__default__"

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """Send the latest user message to ``cursor-agent acp`` and stream the reply.

        :param messages: Conversation history; the latest user message is sent.
        :param tools: Ignored — cursor-agent owns its tool surface (host-tool
            relay is a later PR).
        :param system_prompt: Prepended to the first turn's prompt (ACP has no
            separate system-prompt field).
        :param config: Per-turn config; unused in this build.
        """
        del tools, config
        session_key = self._session_key(messages)
        state = self._sessions.get(session_key)
        # Use the persistent flag (not just session existence): a prior turn
        # with an empty prompt returns below without ever sending, so the next
        # turn must still be treated as first (prepend the system prompt).
        is_first_turn = state is None or not state.has_sent_prompt

        # Build the prompt before spawning anything so an empty turn is a cheap
        # no-op (no wasted ``cursor-agent acp`` subprocess).
        prompt = _build_prompt(messages, is_first_turn=is_first_turn, system_prompt=system_prompt)
        if not prompt:
            yield TurnComplete(response=None)
            return

        if state is None:
            client = CursorAcpClient(binary=self._binary, cwd=self._cwd or os.getcwd())
            try:
                await client.start()
                session_id = await client.new_session()
            except Exception as exc:  # noqa: BLE001 — surfaced as ExecutorError (CancelledError propagates)
                # Close the local client directly: it is not yet stored in
                # ``self._sessions``, so ``close_session`` couldn't reach it and
                # the subprocess + reader tasks would orphan.
                with contextlib.suppress(Exception):
                    await client.close()
                yield ExecutorError(message=f"Failed to start cursor-agent acp: {exc}")
                return
            state = _AcpSession(client=client, session_id=session_id)
            self._sessions[session_key] = state

        response_text = ""
        try:
            state.has_sent_prompt = True
            async for update in state.client.prompt(
                state.session_id, [{"type": "text", "text": prompt}]
            ):
                for event in _update_to_events(update):
                    if isinstance(event, TextChunk):
                        response_text += event.text
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — turn-level failure surfaced as a retryable error
            await self.close_session(session_key)
            yield ExecutorError(message=f"cursor-agent acp turn failed: {exc}", retryable=True)
            return

        yield TurnComplete(response=response_text or None, usage=None)

    async def close_session(self, session_key: str) -> None:
        """Close and drop the ACP session for *session_key* (terminates its subprocess)."""
        state = self._sessions.pop(session_key, None)
        if state is not None:
            await state.client.close()

    async def interrupt_session(self, session_key: str) -> bool:
        """Drop the session so the next turn starts a fresh ACP session.

        Mirrors :class:`CursorExecutor`: a resumed turn would bypass the runner's
        interrupt marker, so the cleanest interrupt is to tear the session down.
        """
        if session_key not in self._sessions:
            return False
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface as False
            logger.debug("CursorNativeExecutor: close after interrupt failed: %s", exc)
            return False

    async def close(self) -> None:
        """Close every live ACP session."""
        for key in list(self._sessions.keys()):
            await self.close_session(key)


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _extract_text(message: Message) -> str:
    """Extract plain text from a message's ``content`` (string or block list)."""
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return str(content)


def _latest_user_text(messages: list[Message]) -> str:
    """Return the text of the latest user message."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return _extract_text(message)
    return ""


def _build_prompt(messages: list[Message], *, is_first_turn: bool, system_prompt: str) -> str:
    """Build the ``session/prompt`` text.

    The ACP session persists history across prompts, so on the first turn the
    Omnigent system prompt is prepended (ACP has no system-prompt field) and any
    prior history (e.g. a ``pass_history=True`` sub-agent) is serialized; later
    turns send only the latest user message. Mirrors
    :func:`omnigent.inner.cursor_executor._build_cursor_prompt`.
    """
    if is_first_turn and len(messages) > 1:
        lines = ["Conversation so far:"]
        for message in messages:
            role = str(message.get("role") or "user").replace("_", " ")
            lines.append(f"{role}: {_extract_text(message)}")
        lines.append("")
        lines.append(
            "Respond to the latest user message, using the conversation above as context."
        )
        body = "\n".join(lines)
    else:
        body = _latest_user_text(messages)

    if is_first_turn and system_prompt:
        return f"{system_prompt}\n\n{body}" if body else system_prompt
    return body


# ---------------------------------------------------------------------------
# session/update → ExecutorEvent
# ---------------------------------------------------------------------------


def _chunk_text(update: dict[str, Any]) -> str:
    """Pull the text out of an ``*_chunk`` update's ``content`` block."""
    content = update.get("content")
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


def _update_to_events(update: dict[str, Any]) -> list[ExecutorEvent]:
    """Map one ACP ``session/update`` payload to zero or more ExecutorEvents.

    Update kinds not surfaced here (``plan``, ``available_commands_update``,
    ``current_mode_update``, …) yield nothing.
    """
    kind = update.get("sessionUpdate")
    events: list[ExecutorEvent] = []

    if kind == "agent_message_chunk":
        text = _chunk_text(update)
        if text:
            events.append(TextChunk(text=text))
        return events

    if kind == "agent_thought_chunk":
        text = _chunk_text(update)
        if text:
            events.append(ReasoningChunk(delta=text, event_type="reasoning_text"))
        return events

    if kind == "tool_call":
        name = str(update.get("title") or update.get("kind") or "tool")
        raw_input = update.get("rawInput")
        args = raw_input if isinstance(raw_input, dict) else {}
        events.append(
            ToolCallRequest(name=name, args=args, metadata={"call_id": update.get("toolCallId")})
        )
        return events

    if kind == "tool_call_update":
        status = update.get("status")
        if status in ("completed", "failed"):
            name = str(update.get("title") or update.get("kind") or "tool")
            raw_content = update.get("content")
            classification = classify_tool_result(raw_content)
            tool_status = classification.status
            error = classification.error or None
            if status == "failed":
                tool_status = ToolCallStatus.ERROR
                error = error or "tool call failed"
            events.append(
                ToolCallComplete(
                    name=name,
                    status=tool_status,
                    result=raw_content,
                    error=error,
                    metadata={"call_id": update.get("toolCallId")},
                )
            )
        return events

    return events
