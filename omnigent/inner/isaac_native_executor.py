"""No-op executor for the ``isaac-native`` terminal mirror.

Unlike the claude/codex/pi native executors, this one forwards nothing. Isaac
runs in the session terminal and the web UI mirrors that terminal directly:
browser keystrokes reach Isaac as raw PTY bytes through
:mod:`omnigent.terminals.ws_bridge`, never through an executor turn. So there is
no message-injection path, no bridge, and no per-session state — the harness
subprocess only needs *an* executor to exist so :class:`ExecutorAdapter` can
build its FastAPI app.

CRITICAL: this executor must read no required spawn env in ``__init__``. The
pi/codex native executors raise when their bridge-dir env var is unset; copying
that here would crash every isaac-native harness spawn at construction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorEvent,
    Message,
    ToolSpec,
    TurnComplete,
)


class IsaacNativeExecutor(Executor):
    """Harness-side no-op executor for ``omnigent isaac`` sessions.

    Isaac owns its own terminal I/O; this executor exists only to satisfy the
    harness contract. Each turn completes immediately with no response.
    """

    def supports_streaming(self) -> bool:
        """:returns: ``False`` — output is the mirrored terminal, not turn events."""
        return False

    def supports_live_message_queue(self) -> bool:
        """:returns: ``False`` — input reaches Isaac through the terminal PTY."""
        return False

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Complete a turn without doing anything.

        :param messages: Ignored — Isaac receives input via the terminal.
        :param tools: Ignored — Isaac owns its own tool surface.
        :param system_prompt: Ignored — Isaac controls its own prompt.
        :param config: Ignored.
        :yields: A single :class:`TurnComplete` with no response.
        """
        del messages, tools, system_prompt, config
        yield TurnComplete(response=None)
