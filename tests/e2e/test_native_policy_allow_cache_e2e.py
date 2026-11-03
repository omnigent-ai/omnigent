"""A policy added mid-session reaches native Claude hooks once the allow window ends.

Runs this checkout's server with a 60 s native hook allow window and the
PreToolUse command a Claude-native session installs. The command's shell check
reads the clock with ``date``, so a ``date`` shim on PATH stands in for
waiting out the window.

Run without credentials::

    uv run --no-sync pytest -o addopts='' tests/e2e/test_native_policy_allow_cache_e2e.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.harnesses.claude_native.bridge import (
    build_hook_settings,
    prepare_bridge_dir,
    write_active_session_id,
)
from tests._helpers.live_server import isolated_local_server
from tests._helpers.session import bundle_files, post_session_bundle

_AGENT = {
    "spec_version": 1,
    "name": "allow-cache-e2e",
    "prompt": "Reply briefly.",
    "executor": {"type": "omnigent", "config": {"harness": "claude-native"}},
}

_PRE_TOOL_USE = json.dumps(
    {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "true"}},
    separators=(",", ":"),
)


def _run_policy_hook(command: str, *, path: str) -> str:
    """Run the installed hook command through /bin/sh and return its stdout."""
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        input=_PRE_TOOL_USE,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PATH": path},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_policy_added_mid_session_applies_once_allow_window_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window delays a new policy by at most 60 s; after that the server enforces it."""
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path / "root")
    config = tmp_path / "server.yaml"
    config.write_text(yaml.safe_dump({"native_hook_allow_cache_seconds": 60}), encoding="utf-8")
    with (
        isolated_local_server(tmp_path / "server", extra_args=["--config", str(config)]) as url,
        httpx.Client(base_url=url, trust_env=False, timeout=30) as client,
    ):
        bundle = bundle_files({"config.yaml": yaml.safe_dump(_AGENT).encode()})
        created = post_session_bundle(client.post, "/v1/sessions", bundle)
        created.raise_for_status()
        session_id = created.json()["session_id"]
        bridge_dir = prepare_bridge_dir(
            session_id, bridge_id="bridge_allow_e2e", workspace=tmp_path
        )
        write_active_session_id(bridge_dir, session_id)
        settings = build_hook_settings(bridge_dir, ap_server_url=url)
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        path = os.environ["PATH"]

        # No policies yet: the server allows the call and opens a ~60 s window.
        assert _run_policy_hook(command, path=path) == ""
        expiry = int((bridge_dir / "policy_allow_until").read_text().split()[0])
        assert 50 < expiry - time.time() <= 60

        # A policy added from outside the agent (web UI or API) denies every tool call.
        added = client.post(
            f"/v1/sessions/{session_id}/policies",
            json={
                "name": "deny_all_tools",
                "type": "python",
                "handler": "omnigent.policies.builtins.safety.max_tool_calls_per_session",
                "factory_params": {"limit": 0},
            },
        )
        added.raise_for_status()

        # Inside the window the hook still answers in the shell.
        assert _run_policy_hook(command, path=path) == ""

        # One second after the window: the hook asks the server, which denies.
        clock = tmp_path / "clock"
        clock.mkdir()
        (clock / "date").write_text(f"#!/bin/sh\necho {expiry + 1}\n", encoding="utf-8")
        (clock / "date").chmod(0o755)
        denied = json.loads(_run_policy_hook(command, path=f"{clock}{os.pathsep}{path}"))
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "Exceeded 0 tool calls" in denied["hookSpecificOutput"]["permissionDecisionReason"]

        # The deny closed the window, so the next hook asks the server at once.
        denied_again = json.loads(_run_policy_hook(command, path=path))
        assert denied_again["hookSpecificOutput"]["permissionDecision"] == "deny"
