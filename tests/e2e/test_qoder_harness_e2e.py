"""Hermetic happy-path test for the first-class Qoder ACP harness."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.executor import TextChunk, TurnComplete
from omnigent.runtime.workflow import _build_acp_cli_spawn_env
from omnigent.spec.types import AgentSpec, ExecutorSpec

_FAKE_QODER = r"""#!/usr/bin/env python3
import json
import os
import sys

assert "--acp" in sys.argv

def send(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"sessionId": "qoder-test"}})
    elif method == "session/prompt":
        session_id = message["params"]["sessionId"]
        token = os.environ.get("QODER_PERSONAL_ACCESS_TOKEN", "missing")
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": session_id,
            "update": {"sessionUpdate": "agent_message_chunk", "content": {
                "type": "text", "text": f"qoder-acp-ok:{token}",
            }},
        }})
        send({"jsonrpc": "2.0", "id": request_id, "result": {"stopReason": "end_turn"}})
"""


def _spec() -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        name="qoder-e2e",
        instructions="Test Qoder.",
        executor=ExecutorSpec(type="omnigent", config={"harness": "qoder"}),
    )


@pytest.mark.asyncio
async def test_qoder_harness_runs_a_real_acp_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    qodercli = tmp_path / "qodercli"
    qodercli.write_text(_FAKE_QODER)
    qodercli.chmod(0o755)
    monkeypatch.setattr("omnigent._platform.resolve_cli_binary", lambda _binary: str(qodercli))
    monkeypatch.setenv("QODER_PERSONAL_ACCESS_TOKEN", "test-token")
    monkeypatch.delenv("OMNIGENT_QODER_PATH", raising=False)

    spawn_env = _build_acp_cli_spawn_env(_spec(), harness="qoder", cwd=tmp_path)
    command = spawn_env["HARNESS_ACP_COMMAND"]
    assert shlex.split(command) == [str(qodercli), "--acp"]

    executor = AcpExecutor(
        AcpAgentConfig(
            command=command,
            name=spawn_env["HARNESS_ACP_NAME"],
            env_passthrough=("QODER_PERSONAL_ACCESS_TOKEN",),
            omnigent_mcp=False,
        ),
        cwd=str(tmp_path),
    )
    events = []
    try:
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello"}], [], "You are Qoder."
        ):
            events.append(event)
    finally:
        await executor.close()

    assert "".join(event.text for event in events if isinstance(event, TextChunk)) == (
        "qoder-acp-ok:test-token"
    )
    assert sum(isinstance(event, TurnComplete) for event in events) == 1
