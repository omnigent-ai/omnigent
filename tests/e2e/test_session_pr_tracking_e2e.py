"""Exercise native hook subprocesses → local relay → durable PRs → host resources.

GitHub is replaced by a small CLI fixture; no remote repositories are modified.
The harness payloads follow the documented PostToolUse contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.native.tool_observer_hook import hook_settings
from omnigent.runner.session_prs import SessionPrRegistry
from omnigent.workspace_fs import WorkspaceReader


@pytest.mark.parametrize("harness", ["claude_native", "codex_native"])
async def test_native_session_tracks_prs_across_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    binary = tmp_path / "bin"
    binary.mkdir()
    gh = binary / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] not in {'pr', 'api'}: sys.exit(0)\n"
        "repo = ('/'.join(args[1].strip('/').split('/')[1:3])\n"
        "        if args[0] == 'api' else "
        "args[args.index('-R') + 1].removeprefix('github.com/'))\n"
        "url = f'https://github.com/{repo}/pull/42'\n"
        "if args[:2] == ['pr', 'create'] or args[0] == 'api': print(url)\n"
        "elif args[:2] == ['pr', 'view'] and '--comments' in args:\n"
        "    print('Supersedes https://github.com/unrelated/repo/pull/7')\n"
        "    print('https://github.com/unrelated/repo/pull/7')\n"
        "    print('View this pull request on GitHub: ' + url)\n"
        "elif args[:2] == ['pr', 'view']: "
        "print(json.dumps({'number': 42, 'url': url, 'title': repo, 'state': 'OPEN'}))\n"
        "elif args[:2] == ['pr', 'diff']: print('patch for ' + repo)\n"
        "else: sys.exit(1)\n"
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir = bridge.prepare_bridge_dir("tracking", workspace=workspace)
    relay = bridge.start_tool_relay(
        bridge_dir=bridge_dir,
        tools=[],
        tool_executor=None,
        loop=asyncio.get_running_loop(),
        session_id="conv_owned",
    )
    hook = hook_settings(bridge_dir, sys.executable, f"omnigent.harnesses.{harness}.hook")
    command = shlex.split(str(hook["command"]))
    try:
        shell_command = (
            "gh auth switch --user example-user; gh repo set-default example/one && "
            "gh pr create -R example/one; gh config set pager cat"
        )
        output = subprocess.check_output(
            ["/bin/sh", "-c", shell_command], text=True, cwd=workspace
        )
        rest_command = (
            "gh auth switch --user example-user; "
            "printf 'HEAD SHA: fixture\\n' && gh api /repos/example/three/pulls \\\n"
            "  --method POST \\\n"
            "  --field title='fixture PR' \\\n"
            "  --field body='Summary\nA fixture PR.' \\\n"
            "  --jq '.html_url' 2>&1"
        )
        rest_output = subprocess.check_output(
            ["/bin/sh", "-c", rest_command], text=True, cwd=workspace
        )
        mixed_command = (
            "gh api /repos/example/five/issues/42/comments -f body=fixture; "
            "gh pr view 42 -R example/one --json url"
        )
        mixed_output = subprocess.check_output(
            ["/bin/sh", "-c", mixed_command], text=True, cwd=workspace
        )
        view_command = "gh pr view 42 -R example/one --comments"
        view_output = subprocess.check_output(
            ["/bin/sh", "-c", view_command], text=True, cwd=workspace
        )
        payloads = [
            {
                "tool_name": "Bash",
                "tool_input": {"command": shell_command},
                "tool_response": {"stdout": output, "exit_code": 0},
            },
            {
                "tool_name": "Bash",
                "tool_input": {"command": rest_command},
                "tool_response": {"stdout": rest_output, "exit_code": 0},
            },
            {
                "tool_name": "mcp__custom__create_pull_request",
                "tool_input": {},
                "tool_response": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"html_url": "https://github.com/example/two/pull/42"}
                            ),
                        }
                    ]
                },
            },
            {
                "tool_name": "mcp__github__github_write_api_call",
                "tool_input": {
                    "endpoint": "pull_requests.create",
                    "params": {
                        "org": "example",
                        "repo": "four",
                        "branch": "user/topic",
                        "base_ref_name": "main",
                        "draft": True,
                    },
                },
                "tool_response": json.dumps(
                    {
                        "result": "=== PULL REQUEST CREATED ===\n\n"
                        "✓ Successfully created PR #42\n\n"
                        "View PR: https://github.com/example/four/pull/42\n"
                        "Labels: [ai-assisted]\n\n"
                        "Next steps:\n  • Assign reviewers\n  • Monitor CI checks\n"
                    }
                ),
            },
            {
                "tool_name": "Bash",
                "tool_input": {"command": view_command},
                "tool_response": {"stdout": view_output, "exit_code": 0},
            },
            {
                "tool_name": "Bash",
                "tool_input": {"command": mixed_command},
                "tool_response": {"stdout": mixed_output, "exit_code": 0},
            },
        ]
        for index, payload in enumerate(payloads):
            payload.update(
                hook_event_name="PostToolUse",
                session_id="provider-session",
                tool_use_id=f"call-{index}",
            )
            for _ in range(2):
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
        registry = SessionPrRegistry("conv_owned")
        entries = registry.list()
        assert len(entries) == 5
        assert {entry.repository: entry.relationship for entry in entries} == {
            "example/one": "created",
            "example/two": "created",
            "example/three": "created",
            "example/four": "created",
            "example/five": "worked_on",
        }
        registry.remove("https://github.com/example/five/pull/42")
        # A new observation, as well as hook replay, must respect explicit unlinking.
        for call_id in (payloads[-1]["tool_use_id"], "new-comment"):
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                input=json.dumps({**payloads[-1], "tool_use_id": call_id}),
                text=True,
                capture_output=True,
                timeout=10,
            )
            assert completed.returncode == 0, completed.stderr
            assert completed.stdout == ""
        info = json.loads((bridge_dir / "tool_relay.json").read_text())
        async with httpx.AsyncClient() as client:
            denied = await client.post(info["url"] + "/hook/observe-tool", json=payloads[0])
        assert denied.status_code == 401
    finally:
        relay.close()
    # Closing the relay and using a fresh reader models the parked-runner host path.
    entries = SessionPrRegistry("conv_owned").list()
    assert len(entries) == 4
    assert all(entry.relationship == "created" for entry in entries)
    assert SessionPrRegistry("provider-session").list() == []
    reader = WorkspaceReader(workspace)
    for entry in entries:
        result = reader.github_info(session_id="conv_owned", pr_url=entry.url)
        assert result["pr"]["title"] == entry.repository
        assert len(result["prs"]) == 4
        assert (
            reader.github_pr_diff(session_id="conv_owned", pr_url=entry.url)["patch"].strip()
            == f"patch for {entry.repository}"
        )
