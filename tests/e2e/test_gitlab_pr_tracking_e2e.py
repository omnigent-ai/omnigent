"""Native hook subprocesses carry GitLab writes into durable session resources."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.git_providers import reset_for_tests
from omnigent.harnesses.claude_native import bridge
from omnigent.native.tool_observer_hook import hook_settings
from omnigent.runner.session_prs import SessionPrRegistry
from omnigent.workspace_fs import WorkspaceReader

URL = "https://gitlab.com/team/sub/project/-/merge_requests/7"


@pytest.fixture(autouse=True)
def _isolated_registry() -> Iterator[None]:
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.mark.parametrize("harness", ["claude_native", "codex_native"])
async def test_native_hook_tracks_gitlab_mr_without_observer_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    binary = tmp_path / "bin"
    binary.mkdir()
    calls = tmp_path / "cli-calls.jsonl"
    mr = {
        "id": 99999,
        "iid": 7,
        "web_url": URL,
        "title": "Native GitLab MR",
        "state": "opened",
        "source_branch": "feature",
        "target_branch": "main",
        "description": "Hook relay fixture",
    }
    glab = binary / "glab"
    glab.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({str(calls)!r}, 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"if sys.argv[1:3] == ['mr', 'create']: print({URL!r})\n"
        "elif sys.argv[1] == 'api':\n"
        f"    print(json.dumps([] if sys.argv[-1].split('?')[0].endswith('/notes') else {mr!r}))\n"
        "else: sys.exit(1)\n"
    )
    glab.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir = bridge.prepare_bridge_dir("gitlab-tracking", workspace=workspace)
    relay = bridge.start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[],
        tool_executor=None,
        loop=asyncio.get_running_loop(),
        session_id="gitlab-owned",
    )
    command = shlex.split(
        str(
            hook_settings(bridge_dir, sys.executable, f"omnigent.harnesses.{harness}.hook")[
                "command"
            ]
        )
    )
    output = subprocess.check_output(
        [str(glab), "mr", "create", "-R", "team/sub/project"], text=True, cwd=workspace
    )
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "untrusted-provider-session",
        "tool_use_id": "gitlab-create",
        "tool_name": "Bash",
        "tool_input": {"command": "glab mr create -R team/sub/project"},
        "tool_response": {"stdout": output, "exit_code": 0},
    }
    before = calls.read_text()
    try:
        for invocation in (
            payload,
            payload,
            {
                **payload,
                "tool_use_id": "gitlab-read",
                "tool_input": {"command": "glab mr view 7 --comments"},
            },
        ):
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                input=json.dumps(invocation),
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert completed.returncode == 0, completed.stderr
            assert completed.stdout == ""
        [entry] = SessionPrRegistry("gitlab-owned").list()
        assert entry.url == URL and entry.relationship == "created" and entry.number == 7
        assert calls.read_text() == before, "the observer invoked the GitLab CLI"
        assert SessionPrRegistry("untrusted-provider-session").list() == []
    finally:
        relay.close()
    info = WorkspaceReader(workspace).github_info(session_id="gitlab-owned", pr_url=URL)
    assert info["pr"]["title"] == "Native GitLab MR"
    assert info["provider_display"]["number_prefix"] == "!"
    assert info["prs"][0]["provider"] == "gitlab"
