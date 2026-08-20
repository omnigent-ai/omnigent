"""Opt-in live check for the builtin Hermes ACP harness."""

from __future__ import annotations

import os
import shutil

import pytest

from omnigent.hermes_native_bridge import bridge_dir_for_session_id
from omnigent.inner import acp_harness
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.runtime.workflow import _build_acp_cli_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_HERMES_ACP") != "1" or shutil.which("hermes") is None,
    reason=(
        "hermes ACP e2e is opt-in: set OMNIGENT_E2E_HERMES_ACP=1 with the "
        "hermes binary and a configured provider"
    ),
)


@pytest.mark.asyncio
async def test_builtin_hermes_acp_streams_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog builder and generic ACP wrapper complete one real turn."""
    session_id = "e2e_hermes_acp"
    bridge_dir = bridge_dir_for_session_id(session_id)
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    spec = AgentSpec(
        spec_version=1,
        name="hermes-acp-e2e",
        instructions="Reply briefly.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "hermes-acp"}),
    )
    wrapper_env = _build_acp_cli_spawn_env(
        spec,
        harness="hermes-acp",
        session_id=session_id,
    )
    for name, value in wrapper_env.items():
        monkeypatch.setenv(name, value)

    executor = acp_harness._build_acp_executor()
    chunks: list[str] = []
    final: TurnComplete | None = None
    try:
        async for event in executor.run_turn(
            [{"role": "user", "content": "Reply with exactly PONG."}],
            tools=[],
            system_prompt="Reply with exactly the requested word.",
        ):
            if isinstance(event, TextChunk):
                chunks.append(event.text)
            elif isinstance(event, TurnComplete):
                final = event
            elif isinstance(event, ExecutorError):
                pytest.fail(f"executor error: {event.message}")
    finally:
        await executor.close()
        shutil.rmtree(bridge_dir, ignore_errors=True)

    assert final is not None
    assert "PONG" in "".join(chunks) + (final.response or "")
