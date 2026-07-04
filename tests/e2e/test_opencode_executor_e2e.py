"""Opt-in end-to-end test for the OpenCode SDK harness.

Gated on ``OMNIGENT_E2E_OPENCODE=1`` and a configured ``opencode`` install
(binary on PATH + a provider authenticated via ``opencode auth login``).
Skipped by default so CI never depends on a live OpenCode server.

Run it manually with::

    OMNIGENT_E2E_OPENCODE=1 .venv/bin/python -m pytest \
        tests/e2e/test_opencode_executor_e2e.py -v

It boots a real ``opencode serve`` subprocess via :class:`OpenCodeExecutor`,
sends one prompt over the SDK, and asserts the streamed reply contains the
sentinel token. Token usage is asserted opportunistically (the provider may or
may not report it).
"""

from __future__ import annotations

import os

import pytest

from omnigent.inner.executor import TextChunk, TurnComplete
from omnigent.inner.opencode_executor import OpenCodeExecutor

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_OPENCODE") != "1",
    reason="set OMNIGENT_E2E_OPENCODE=1 and have `opencode` configured to run",
)


@pytest.mark.asyncio
async def test_opencode_server_roundtrip() -> None:
    """Boot opencode serve, send one prompt, and assert the reply + usage."""
    ex = OpenCodeExecutor()
    try:
        texts: list[str] = []
        usage: dict[str, object] | None = None
        async for event in ex.run_turn(
            [{"role": "user", "content": "Reply with exactly: PONG", "session_id": "e2e"}],
            tools=[],
            system_prompt="",
            config=None,
        ):
            if isinstance(event, TextChunk):
                texts.append(event.text)
            if isinstance(event, TurnComplete):
                usage = event.usage
        assert "PONG" in "".join(texts)
        # Usage is best-effort: when present it must carry the token keys.
        assert usage is None or "input_tokens" in usage
    finally:
        await ex.close()
