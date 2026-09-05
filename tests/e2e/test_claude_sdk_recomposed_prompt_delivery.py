"""The claude-sdk harness delivers a recomposed system prompt mid-session.

The harness keeps a persistent ``ClaudeSDKClient`` per Omnigent session,
cached in ``ClaudeSDKExecutor._clients`` keyed on the session id. When the
composed system prompt changes between turns — an agent-spec instructions
edit, or a framework-owned instruction that activates mid-conversation and
is appended by ``omnigent/runtime/prompt.py`` — the executor must deliver
the updated prompt to the vendor session on the very next turn (rebuild the
client, or update the prompt in place).

This test drives two turns of the same session through ``run_turn`` with
different composed prompts and asserts the second turn's prompt is the one
the SDK actually sees; a cached client pinned to the turn-1 composition
fails it.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch


def test_second_turn_delivers_updated_composed_prompt() -> None:
    """Two ``run_turn`` calls for one session with different composed
    prompts: the prompt active for the second turn must be the second
    turn's composed text, not the cached turn-1 value."""
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    class _ResultMessage:
        def __init__(self, subtype: str, result: str) -> None:
            self.subtype = subtype
            self.result = result

    # Options captured at client construction; the prompt the vendor
    # session runs on is the one in the options of the client that
    # serves the turn.
    captured_options: list[Any] = []

    class _FakeSDK:
        AssistantMessage = type("AssistantMessage", (), {})
        UserMessage = type("UserMessage", (), {})
        SystemMessage = type("SystemMessage", (), {})
        ResultMessage = _ResultMessage
        StreamEvent = type("StreamEvent", (), {})
        ClaudeAgentOptions = type(
            "ClaudeAgentOptions",
            (),
            {"__init__": lambda self, **kwargs: self.__dict__.update(kwargs)},
        )

        class ClaudeSDKClient:
            def __init__(self, options: Any) -> None:
                captured_options.append(options)

            async def connect(self) -> None:
                return None

            async def query(self, prompt: Any, session_id: str = "default") -> None:
                return None

            async def receive_response(self) -> Any:
                yield _ResultMessage("default", "ok")

            async def disconnect(self) -> None:
                return None

            async def set_model(self, model: Any) -> None:
                return None

    turn1_prompt = "Base authored instructions."
    # A framework instruction activates between turns; the runner
    # recomposes and passes the new value on the next run_turn call.
    turn2_prompt = (
        turn1_prompt + "\n\n[framework] Attribute shared-session messages to their author."
    )

    async def _drive() -> None:
        executor = ClaudeSDKExecutor()
        with patch(
            "omnigent.inner.claude_sdk_executor._ensure_sdk",
            return_value=_FakeSDK,
        ):
            async for _ in executor.run_turn(
                [{"role": "user", "content": "hello", "session_id": "sess-recompose"}],
                [],
                turn1_prompt,
            ):
                pass
            async for _ in executor.run_turn(
                [{"role": "user", "content": "follow-up", "session_id": "sess-recompose"}],
                [],
                turn2_prompt,
            ):
                pass

    asyncio.run(_drive())

    assert captured_options, "no SDK client was ever constructed"
    delivered = captured_options[-1].system_prompt
    assert delivered == turn2_prompt, (
        "stale composed prompt: the client serving turn 2 was built with "
        f"{delivered!r}; the recomposed turn-2 prompt {turn2_prompt!r} never "
        "reached the vendor session (cached client not rebuilt/refreshed)"
    )
