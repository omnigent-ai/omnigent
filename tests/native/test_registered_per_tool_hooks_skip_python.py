"""The registered per-tool hook commands do not start Python while the relay is up.

Behavioral form of the per-call latency report: each command Claude Code and
Codex block a tool call on is run exactly as registered, with a fake relay
answering and the interpreter replaced by a stub that records being started.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.codex_native.app_server import _codex_policy_hooks_settings

_PAYLOAD = json.dumps(
    {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}}
)


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _registered_per_tool_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, python: str):
    monkeypatch.setattr(claude_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    claude_dir = claude_bridge.prepare_bridge_dir("conv_hooks", workspace=tmp_path)
    claude_hooks = claude_bridge.build_hook_settings(
        claude_dir, python_executable=python, ap_server_url="http://127.0.0.1:9"
    )["hooks"]
    codex_dir = tmp_path / "codex-bridge"
    codex_dir.mkdir()
    codex_hooks = _codex_policy_hooks_settings(codex_dir, python)["hooks"]

    def registered(hooks: dict, event: str, marker: str) -> str:
        commands = [
            str(hook["command"])
            for entry in hooks[event]
            for hook in entry["hooks"]
            if marker in str(hook["command"])
        ]
        assert len(commands) == 1, (event, marker, commands)
        return commands[0]

    return {
        "claude PreToolUse policy": (
            claude_dir,
            registered(claude_hooks, "PreToolUse", "evaluate-policy"),
        ),
        "claude PostToolUse observer": (
            claude_dir,
            registered(claude_hooks, "PostToolUse", "observe-tool"),
        ),
        "codex PreToolUse policy": (
            codex_dir,
            registered(codex_hooks, "PreToolUse", "evaluate-policy"),
        ),
        "codex PostToolUse policy": (
            codex_dir,
            registered(codex_hooks, "PostToolUse", "evaluate-policy"),
        ),
        "codex PostToolUse observer": (
            codex_dir,
            registered(codex_hooks, "PostToolUse", "observe-tool"),
        ),
    }


def test_registered_per_tool_hooks_do_not_start_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl is not installed")
    started = tmp_path / "python-started"
    stub = tmp_path / "python-stub"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {started}\ncat > /dev/null\n")
    stub.chmod(0o700)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env_text = f"OMNIGENT_RELAY_URL='http://127.0.0.1:{server.server_address[1]}'\n"
        spawned: dict[str, list[str]] = {}
        for label, (bridge_dir, command) in _registered_per_tool_commands(
            tmp_path, monkeypatch, str(stub)
        ).items():
            (bridge_dir / "tool_relay.env").write_text(env_text)
            (bridge_dir / "tool_relay.curlrc").write_text('header = "Authorization: Bearer t"\n')
            started.unlink(missing_ok=True)
            proc = subprocess.run(
                command, shell=True, input=_PAYLOAD, capture_output=True, text=True, timeout=60
            )
            assert proc.returncode == 0, (label, proc.stderr)
            if started.exists():
                spawned[label] = started.read_text().splitlines()
    finally:
        server.shutdown()
        server.server_close()
    assert spawned == {}, f"hooks that started a Python interpreter on a tool call: {spawned}"
