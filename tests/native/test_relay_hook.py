"""The shared relay hook command answers from the relay, else replays stdin."""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import start_tool_relay
from omnigent.harnesses.pi_native import bridge as pi_native_bridge
from omnigent.native.relay_hook import TOOL_RELAY_ENV_FILE, relay_hook_command

_PAYLOAD = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash"})


def _run(command: str) -> subprocess.CompletedProcess[str]:
    # As an argv, the way harnesses that do not use a shell would run it.
    return subprocess.run(
        shlex.split(command), input=_PAYLOAD, capture_output=True, text=True, timeout=30
    )


async def test_relay_hook_command_replays_stdin_unless_the_relay_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    bridge_dir = pi_native_bridge.prepare_bridge_dir("conv_relay_hook")
    command = relay_hook_command(
        bridge_dir, "/hook/observe-tool", "sed s/^/fallback:/", max_time_s=5
    )

    # Relay not started yet: no env file, so stdin goes to the fallback.
    absent = await asyncio.to_thread(_run, command)
    assert absent.returncode == 0, absent.stderr
    assert absent.stdout.strip() == f"fallback:{_PAYLOAD}"

    relay = start_tool_relay(
        bridge_dir=bridge_dir, tools=[], tool_executor=None, loop=asyncio.get_running_loop()
    )
    try:
        answered = await asyncio.to_thread(_run, command)
    finally:
        relay.close()
    assert answered.returncode == 0, answered.stderr
    assert answered.stdout == "", "the relay's (empty) answer is the hook output"

    # Runner gone, env file left behind: curl fails and stdin is replayed.
    assert (bridge_dir / TOOL_RELAY_ENV_FILE).is_file()
    stale = await asyncio.to_thread(_run, command)
    assert stale.returncode == 0, stale.stderr
    assert stale.stdout.strip() == f"fallback:{_PAYLOAD}"
