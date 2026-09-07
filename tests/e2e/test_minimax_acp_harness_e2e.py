"""Hermetic e2e for the MiniMax Code builtin ACP harness."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from omnigent.inner.acp_harness import _build_acp_executor
from omnigent.inner.executor import ExecutorError, TextChunk, TurnComplete
from omnigent.runtime.workflow import _build_acp_cli_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec

_MARKER = "MINIMAX_ACP_E2E_OK"
_FAKE_MCODE = rf"""#!/usr/bin/env python3
import json
import sys

if sys.argv[1:] != ["acp"]:
    raise SystemExit(f"expected 'acp', got {{sys.argv[1:]!r}}")

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    message = json.loads(line)
    request_id = message.get("id")
    method = message.get("method")
    if method == "initialize":
        send({{"jsonrpc": "2.0", "id": request_id, "result": {{
            "protocolVersion": 1,
            "agentCapabilities": {{"promptCapabilities": {{"image": False}}}},
        }}}})
    elif method == "session/new":
        send({{"jsonrpc": "2.0", "id": request_id,
              "result": {{"sessionId": "minimax-e2e-session"}}}})
    elif method == "session/prompt":
        session_id = message["params"]["sessionId"]
        send({{"jsonrpc": "2.0", "method": "session/update", "params": {{
            "sessionId": session_id,
            "update": {{
                "sessionUpdate": "agent_message_chunk",
                "content": {{"type": "text", "text": "{_MARKER}"}},
            }},
        }}}})
        send({{"jsonrpc": "2.0", "id": request_id, "result": {{
            "stopReason": "end_turn",
            "usage": {{"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}},
        }}}})
"""


@pytest.mark.asyncio
async def test_minimax_catalog_launches_mcode_acp_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public MiniMax harness row launches ``mcode acp`` and streams a turn."""
    fake_mcode = tmp_path / "mcode"
    fake_mcode.write_text(_FAKE_MCODE, encoding="utf-8")
    fake_mcode.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_MINIMAX_PATH", str(fake_mcode))

    spec = AgentSpec(
        spec_version=1,
        name="minimax-e2e",
        instructions="Test MiniMax Code.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "minimax"}),
    )
    spawn_env = _build_acp_cli_spawn_env(spec, harness="minimax", cwd=tmp_path)
    assert shlex.split(spawn_env["HARNESS_ACP_COMMAND"]) == [str(fake_mcode), "acp"]
    assert spawn_env["HARNESS_ACP_NAME"] == "MiniMax Code"
    for name, value in spawn_env.items():
        monkeypatch.setenv(name, value)

    executor = _build_acp_executor()
    events = []
    try:
        async for event in executor.run_turn(
            [{"role": "user", "content": "Reply with the marker."}],
            tools=[],
            system_prompt="You are a test agent.",
        ):
            events.append(event)
    finally:
        await executor.close()

    errors = [event for event in events if isinstance(event, ExecutorError)]
    assert not errors
    assert "".join(event.text for event in events if isinstance(event, TextChunk)) == _MARKER
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1
    assert completions[0].usage == {
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
    }
