"""Native hook subprocesses → local relay → non-git filesystem registry.

Drives the real observer hook (the documented PostToolUse contract) through
the tool relay's ``/hook/observe-tool`` with a ``file_change_observer``, the
same wiring the runner installs, and verifies native file-mutating tool calls
land in an :class:`AgentEditFilesystemRegistry` — the registry non-git
workspaces use for ``GET .../changes``.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.native.tool_observer_hook import hook_settings
from omnigent.runner.native_file_observer import native_file_changes
from omnigent.runtime.filesystem_registry import AgentEditFilesystemRegistry

_SESSION_ID = "conv_owned"


@pytest.mark.parametrize("harness", ["claude_native", "codex_native"])
async def test_native_write_reaches_changes_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = AgentEditFilesystemRegistry(workspace)

    async def _observe(payload: dict[str, object]) -> None:
        # Mirrors the runner's relay wiring for a resolved session registry.
        for change in native_file_changes(payload):
            if change.baseline is not None:
                registry.seed_snapshot(change.path, change.baseline, session_id=_SESSION_ID)
            registry.record_change(change.path, change.operation, _SESSION_ID)

    bridge_dir = bridge.prepare_bridge_dir("changes-tracking", workspace=workspace)
    relay = bridge.start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[],
        tool_executor=None,
        loop=asyncio.get_running_loop(),
        session_id=_SESSION_ID,
        file_change_observer=_observe,
    )
    hook = hook_settings(bridge_dir, sys.executable, f"omnigent.harnesses.{harness}.hook")
    command = shlex.split(str(hook["command"]))
    try:
        report = workspace / "report.md"
        report.write_text("hi\n")
        payloads: list[dict[str, object]] = [
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(report), "content": "hello\n"},
                "tool_response": {"type": "create", "filePath": str(report)},
            },
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(report),
                    "old_string": "hello",
                    "new_string": "hi",
                },
                "tool_response": {"filePath": str(report), "originalFile": "hello\n"},
            },
            # Shell side effects are unattributable and must not be recorded.
            {
                "tool_name": "Bash",
                "tool_input": {"command": "echo x > shell.txt"},
                "tool_response": {"stdout": "", "exit_code": 0},
            },
            # Paths escaping the workspace are rejected by the registry.
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(tmp_path / "escape.txt"), "content": "x"},
                "tool_response": {"type": "create"},
            },
        ]
        for index, payload in enumerate(payloads):
            payload.update(
                hook_event_name="PostToolUse",
                session_id="provider-session",
                tool_use_id=f"call-{index}",
            )
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert completed.returncode == 0, completed.stderr
            assert completed.stdout == ""
    finally:
        relay.close()

    entries = registry.list_changed_files(_SESSION_ID, limit=10)
    assert [(entry["path"], entry["status"]) for entry in entries] == [("report.md", "created")]
    # The Edit's originalFile seeds the diff baseline.
    assert registry.get_baseline("report.md") == "hello\n"
    # Changes bind to the relay's session, not the provider's own id.
    assert registry.list_changed_files("provider-session", limit=10) == []
