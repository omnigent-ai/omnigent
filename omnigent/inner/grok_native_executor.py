"""Executor that bridges Omnigent web turns into a native Grok session.

The runner-side launcher owns the ``grok agent serve`` WebSocket server and
its forwarder; this executor reads the endpoint coordinates from bridge
state and drives ``session/prompt`` for each web-UI turn — the same
injection/completion split as goose-native: output is mirrored by the
forwarder, so ``run_turn`` only admits the prompt and yields
``TurnComplete``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from omnigent.harnesses.grok_native.bridge import (
    GrokBridgeState,
    bridge_dir_from_env,
    read_bridge_state,
)
from omnigent.harnesses.grok_native.client import GrokAcpError
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

GROK_NATIVE_HARNESS_ID = "grok-native"


class GrokNativeExecutor(Executor):
    """Harness-side executor for ``omnigent grok`` web-UI turns.

    :param bridge_dir: Optional bridge dir override; ``None`` reads
        :data:`GROK_NATIVE_BRIDGE_DIR_ENV_VAR` from the spawn env.
    """

    def __init__(self, bridge_dir: Path | None = None) -> None:
        self._bridge_dir = bridge_dir or bridge_dir_from_env()

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is mirrored by the runner-side forwarder."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``False`` — mid-turn steering is not wired yet."""
        return False

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig,
    ) -> AsyncIterator[ExecutorEvent]:
        """Admit the latest web-UI user message as a ``session/prompt``."""
        del tools, system_prompt, config
        text = _latest_user_text(messages)
        if not text:
            yield ExecutorError(message="grok native turn had no user text to send")
            return
        state = read_bridge_state(self._bridge_dir)
        if state is None or not state.session_id:
            yield ExecutorError(
                message="grok native session not ready (no bridge state/session id)"
            )
            return
        try:
            await _prompt_session(state, text)
        except (GrokAcpError, OSError, RuntimeError) as exc:
            yield ExecutorError(message=describe_exception(exc))
            return
        yield TurnComplete(response=None)

    async def enqueue_session_message(self, content: EnqueuedContent) -> None:
        """Reject mid-turn steering until the grok steer path is wired."""
        del content
        raise RuntimeError("grok-native does not support mid-turn steering yet")

    async def stop(self) -> None:
        """Interrupt the active turn via ``session/cancel``."""
        state = read_bridge_state(self._bridge_dir)
        if state is None or not state.session_id:
            return
        with contextlib.suppress(GrokAcpError, OSError, RuntimeError):
            await _cancel_session(state)


async def _prompt_session(state: GrokBridgeState, text: str) -> None:
    """Open a WS to the serve endpoint and drive one prompt turn."""
    from omnigent.harnesses.grok_native.client import GrokAcpClient

    client = await GrokAcpClient.connect(state.ws_url)
    try:
        await client.session_prompt(session_id=state.session_id, text=text)
    finally:
        await client.close()


async def _cancel_session(state: GrokBridgeState) -> None:
    """Open a WS and send ``session/cancel`` for the active turn."""
    from omnigent.harnesses.grok_native.client import GrokAcpClient

    client = await GrokAcpClient.connect(state.ws_url)
    try:
        await client.session_cancel(session_id=state.session_id)
    finally:
        await client.close()


def _latest_user_text(messages: list[Message]) -> str:
    """Return the latest user message's text content."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n\n".join(
                str(b.get("text"))
                for b in content
                if isinstance(b, dict)
                and b.get("type") in ("input_text", "text")
                and b.get("text")
            )
    return ""


__all__ = ["GROK_NATIVE_HARNESS_ID", "GrokNativeExecutor"]
