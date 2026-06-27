"""Gated live E2E for the Rovo Dev ACP harness.

Skips unless the real ``acli`` CLI is installed AND
``OMNIGENT_RUN_ROVO_LIVE=1`` is set, so CI without the CLI (and normal local
runs) stay green. Run it explicitly with::

    OMNIGENT_RUN_ROVO_LIVE=1 pytest tests/inner/test_rovo_live_e2e.py

It drives a real ``acli rovodev acp`` session end-to-end and asserts a turn
streams text and completes.
"""

from __future__ import annotations

import os
import shutil

import pytest

from omnigent.inner.executor import (
    ExecutorError,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    TurnComplete,
)
from omnigent.inner.rovo_executor import RovoExecutor

pytestmark = [
    pytest.mark.skipif(
        shutil.which("acli") is None,
        reason="acli CLI not installed",
    ),
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_RUN_ROVO_LIVE") != "1",
        reason="set OMNIGENT_RUN_ROVO_LIVE=1 to run the live Rovo E2E",
    ),
]


@pytest.mark.asyncio
async def test_live_rovo_turn_streams_and_completes() -> None:
    ex = RovoExecutor()
    messages = [
        {
            "role": "user",
            "content": "Reply with exactly the word: PONG",
            "session_id": "rovo-live-1",
        }
    ]
    events = []
    try:
        async for ev in ex.run_turn(messages, [], "You are a terse assistant."):
            events.append(ev)
    finally:
        await ex.close()

    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert completes, "expected a TurnComplete event"
    assert text.strip(), "expected some streamed text from Rovo Dev"


@pytest.mark.asyncio
async def test_live_rovo_multi_turn_with_tools() -> None:
    """Two turns on one warm session, the second forcing a tool call.

    Exercises the full path that regressed in development: a tool call triggers
    an ACP ``session/request_permission`` which must be auto-allowed, the tool
    must complete, and a *subsequent* prompt on the same session must still be
    accepted (no "unprocessed tool calls" error).
    """

    async def _run(ex: RovoExecutor, session_id: str, prompt: str) -> list[object]:
        events: list[object] = []
        async for ev in ex.run_turn(
            [{"role": "user", "content": prompt, "session_id": session_id}],
            [],
            "Be terse. Use your tools to inspect the repository when asked.",
        ):
            events.append(ev)
            if isinstance(ev, ExecutorError):
                pytest.fail(f"unexpected ExecutorError: {ev.message}")
        return events

    ex = RovoExecutor()
    try:
        first = await _run(ex, "rovo-live-multi", "Reply with exactly: HARNESS OK")
        assert any(isinstance(e, TurnComplete) for e in first)

        # Second turn on the SAME warm session, forcing tool use.
        second = await _run(
            ex,
            "rovo-live-multi",
            "Using your tools, count the .py files directly inside "
            "omnigent/inner and reply with just the number.",
        )
    finally:
        await ex.close()

    assert any(isinstance(e, ToolCallRequest) for e in second), (
        "expected at least one tool call on the second turn"
    )
    assert any(isinstance(e, ToolCallComplete) for e in second), (
        "expected the tool call to complete (permission auto-allowed)"
    )
    assert any(isinstance(e, TurnComplete) for e in second), (
        "expected the post-tool turn to complete cleanly"
    )
