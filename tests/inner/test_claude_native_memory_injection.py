"""Integration: automatic-recall content survives the real native executor.

The runner injects recalled memory into a native turn by prepending an
``input_text`` block to the user message (native harnesses fix the system
prompt at spawn, so content is the only per-turn channel). This test drives the
*real* :class:`ClaudeNativeExecutor.run_turn` — the strictest native consumer,
which extracts ``input_text`` blocks only — and asserts the recalled memory
reaches the text typed into Claude's pane, ahead of the user's own message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.runtime.memory import prepend_memory_to_content


@pytest.mark.asyncio
async def test_recalled_memory_reaches_claude_native_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge_dir = tmp_path / "bridge"
    sent: list[str] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del bridge_dir_arg, timeout_s
        sent.append(content)

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    # Shape the runner produces for a native turn: a user message whose content
    # is a list of input_text blocks.
    content: list[dict[str, Any]] = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "what do you know about me?"}],
        }
    ]
    memory_block = "## Relevant long-term memory\n- The user prefers tea over coffee."
    injected = prepend_memory_to_content(content, memory_block)

    executor = ClaudeNativeExecutor(bridge_dir)
    _ = [
        event
        async for event in executor.run_turn(
            messages=injected,  # type: ignore[arg-type]
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(sent) == 1
    prompt = sent[0]
    assert "prefers tea over coffee" in prompt
    assert "what do you know about me?" in prompt
    # Recalled memory precedes the user's own message text.
    assert prompt.index("prefers tea over coffee") < prompt.index("what do you know about me?")
