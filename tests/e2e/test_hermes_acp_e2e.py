"""End-to-end tests: the ``hermes-acp`` harness drives ``hermes acp``.

The streaming sibling of the batch ``hermes`` harness.
:class:`omnigent.inner.hermes_acp_executor.HermesAcpExecutor` spawns
``hermes acp`` (Agent Client Protocol, JSON-RPC over stdio), streams
``agent_message_chunk`` updates as chat text, and emits Hermes' self-executed
tool calls as ToolCallRequest/Complete pairs with ``self_executed`` metadata.
This test drives the executor directly against a *real* ``hermes acp`` process
and asserts the round-trip: streaming text, a self-executed tool call with
paired completion, and token usage on the prompt response.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* **Opt-in only**: set ``OMNIGENT_E2E_HERMES_ACP=1`` to run. Needs the
  ``hermes`` binary on PATH and a configured inference provider in the user's
  ``~/.hermes`` (set up via ``hermes model`` / ``hermes auth``). Without
  ``RUNNER_SERVER_URL`` the executor runs hook-less against that real config,
  the same as the batch hermes e2e posture.

    OMNIGENT_E2E_HERMES_ACP=1 \
    .venv/bin/python -m pytest tests/e2e/test_hermes_acp_e2e.py -v
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
from omnigent.inner.hermes_acp_executor import HermesAcpExecutor

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_HERMES_ACP") != "1" or shutil.which("hermes") is None,
    reason=(
        "hermes acp e2e is opt-in: set OMNIGENT_E2E_HERMES_ACP=1 with the "
        "`hermes` binary on PATH and a configured provider in ~/.hermes."
    ),
)


@pytest.mark.asyncio
async def test_hermes_acp_streams_and_completes() -> None:
    """A plain prose turn streams agent text and completes with token usage."""
    executor = HermesAcpExecutor()
    chunks: list[str] = []
    final: TurnComplete | None = None
    try:
        async for ev in executor.run_turn(
            [{"role": "user", "content": "Reply with exactly the word PONG and nothing else."}],
            tools=[],
            system_prompt="You are a terse assistant.",
        ):
            if isinstance(ev, TextChunk):
                chunks.append(ev.text)
            elif isinstance(ev, TurnComplete):
                final = ev
            elif isinstance(ev, ExecutorError):
                pytest.fail(f"executor error: {ev.message}")
    finally:
        await executor.close()

    assert final is not None, "expected a TurnComplete"
    assert "PONG" in ("".join(chunks) + (final.response or ""))
    # Hermes reports usage on the session/prompt response; the executor
    # normalizes it onto TurnComplete.usage.
    assert final.usage is not None and final.usage.get("total_tokens", 0) > 0


@pytest.mark.asyncio
async def test_hermes_acp_tool_call_emits_self_executed_pair() -> None:
    """A shell tool call emits a ToolCallRequest/Complete pair with
    ``self_executed`` metadata, and the tool output reaches the completion."""
    executor = HermesAcpExecutor()
    requests: list[ToolCallRequest] = []
    completes: list[ToolCallComplete] = []
    final: TurnComplete | None = None
    try:
        async for ev in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": (
                        "Run the shell command: echo HERMES_ACP_MARKER. Use the terminal tool."
                    ),
                }
            ],
            tools=[],
            system_prompt="You are a helpful coding agent.",
        ):
            if isinstance(ev, ToolCallRequest):
                requests.append(ev)
            elif isinstance(ev, ToolCallComplete):
                completes.append(ev)
            elif isinstance(ev, TurnComplete):
                final = ev
            elif isinstance(ev, ExecutorError):
                pytest.fail(f"executor error: {ev.message}")
    finally:
        await executor.close()

    assert final is not None
    assert requests, "expected at least one self-executed ToolCallRequest"
    assert completes, "expected a paired ToolCallComplete"
    assert all(r.metadata.get("self_executed") is True for r in requests)
    assert all(c.metadata.get("self_executed") is True for c in completes)
    # Request/completion pair by call_id (the adapter's dedupe key).
    req_ids = {r.metadata.get("call_id") for r in requests}
    comp_ids = {c.metadata.get("call_id") for c in completes}
    assert req_ids & comp_ids, f"no paired call ids: {req_ids} vs {comp_ids}"
    # The tool ran: its marker shows up in a completion result (the generic
    # client passes the raw ACP content through, so stringify).
    assert any("HERMES_ACP_MARKER" in str(c.result or "") for c in completes)
