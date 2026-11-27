"""Check native draft export, refusal preservation and conditional replacement."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from tests._helpers.native_claude import _PINNED_CLAUDE_MODELS
from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_message_requests

pytestmark = [pytest.mark.mock_only, pytest.mark.timeout(120)]


def test_claude_external_editor_refuses_mismatches_without_mutating_the_draft(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    claude_managed_settings: None,
    record_property: Callable[[str, object], None],
) -> None:
    claude, tmux_binary = shutil.which("claude"), shutil.which("tmux")
    assert claude is not None and tmux_binary is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    instruction = tmp_path / "editor-instruction.json"
    observed = tmp_path / "editor-observed.jsonl"
    hook_record = tmp_path / "blocked-prompt.json"
    hook = tmp_path / "block-prompt.py"
    hook.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"destination = Path({str(hook_record)!r})\n"
        "payload = json.load(sys.stdin)\n"
        "temporary = destination.with_suffix('.tmp')\n"
        "temporary.write_text(json.dumps({key: payload.get(key) for key in "
        "('prompt', 'session_id', 'hook_event_name')}))\n"
        "temporary.replace(destination)\n"
        "print('blocked-draft-ownership-probe', file=sys.stderr)\n"
        "sys.exit(2)\n"
    )
    settings = tmp_path / "hook-settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": shlex.join([sys.executable, str(hook)]),
                                    "timeout": 15,
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    editor = tmp_path / "editor.py"
    editor.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"instruction = Path({str(instruction)!r})\n"
        f"observed = Path({str(observed)!r})\n"
        "source = Path(sys.argv[-1])\n"
        "content = source.read_bytes().decode('utf-8')\n"
        "request = json.loads(instruction.read_text())\n"
        "accepted = content == request['expected']\n"
        "with observed.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'content': content, 'accepted': accepted}) + '\\n')\n"
        "if not accepted:\n"
        "    sys.exit(1)\n"
        "source.write_bytes(request['replacement'].encode('utf-8'))\n"
    )
    editor_command = shlex.join([sys.executable, str(editor)])
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "CLAUDE_CONFIG_DIR": str(config),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_BASE_URL": isolated_mock_llm_server_url,
        "ANTHROPIC_AUTH_TOKEN": "mock-key",
        "DISABLE_AUTOUPDATER": "1",
        "EDITOR": editor_command,
        "VISUAL": editor_command,
    }
    bridge.ensure_claude_workspace_trusted(workspace, env)
    version = subprocess.check_output([claude, "--version"], env=env, text=True, timeout=10)
    typed_draft = (
        "  editor-wrapped-draft "
        + "0123456789 " * 24
        + "draft-end\n\nsecond line with spaces  \nthird line 😀\n"
    )

    def observations() -> list[dict[str, object]]:
        if not observed.exists():
            return []
        try:
            return [json.loads(line) for line in observed.read_text().splitlines()]
        except json.JSONDecodeError:
            return []

    with tempfile.TemporaryDirectory(prefix="omni-editor-") as socket_dir:
        tmux = [tmux_binary, "-S", str(Path(socket_dir) / "tmux.sock")]
        target = "editor:0.0"

        def command(*args: str, text: str | None = None) -> None:
            subprocess.run([*tmux, *args], input=text, text=True, check=True, timeout=10)

        last_pane = ""

        def pane() -> str:
            nonlocal last_pane
            last_pane = subprocess.check_output(
                [*tmux, "capture-pane", "-p", "-t", target], text=True, timeout=5
            )
            return last_pane

        try:
            subprocess.run(
                [
                    *tmux,
                    "-f",
                    os.devnull,
                    "new-session",
                    "-d",
                    "-s",
                    "editor",
                    "-x",
                    "100",
                    "-y",
                    "50",
                    "-c",
                    str(workspace),
                    claude,
                    "--settings",
                    str(settings),
                    "--model",
                    _PINNED_CLAUDE_MODELS["sonnet"],
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                ],
                env=env,
                check=True,
                timeout=10,
            )
            try:
                poll_until(lambda: bridge._composer_box(pane()) is not None, timeout_s=30)
            except AssertionError as exc:
                raise AssertionError({"phase": "initial-composer", "pane": pane()}) from exc
            command("load-buffer", "-b", "editor-draft", "-", text=typed_draft)
            command("paste-buffer", "-d", "-p", "-b", "editor-draft", "-t", target)
            poll_until(lambda: bridge._draft_in_input_box(pane(), "draft-end"), timeout_s=10)
            command("send-keys", "-t", target, "Enter")
            poll_until(hook_record.exists, timeout_s=15)
            poll_until(
                lambda: (
                    "blocked-draft-ownership-probe" in pane()
                    and bridge._draft_in_input_box(last_pane, "draft-end")
                ),
                timeout_s=10,
            )
            payload = json.loads(hook_record.read_text())
            draft = payload["prompt"]
            assert payload["hook_event_name"] == "UserPromptSubmit"
            assert isinstance(draft, str) and "\n" in draft and payload["session_id"]
            steps = [
                (draft[:-1] if draft.endswith("\n") else draft + "\n", draft, False),
                (draft.replace("\n", " "), draft, False),
                (draft, draft, True),
                ("unrelated claim", "/status", False),
            ]
            for index, (claimed, expected, accepted) in enumerate(steps):
                instruction.write_text(json.dumps({"expected": claimed, "replacement": "/status"}))
                command("send-keys", "-t", target, "C-g")
                poll_until(lambda index=index: len(observations()) == index + 1, timeout_s=15)
                observation = observations()[-1]
                actual = observation["content"]
                assert actual == expected, {"step": index, "expected": expected, "actual": actual}
                assert observation["accepted"] is accepted
                poll_until(
                    lambda expected=("/status" if accepted else expected): (
                        bridge._composer_box(pane()) is not None
                        and bridge._draft_in_input_box(last_pane, expected.splitlines()[0].strip())
                    ),
                    timeout_s=10,
                )
            assert get_mock_message_requests(isolated_mock_llm_server_url) == []
        finally:
            try:
                diagnostics = {
                    "version": version.strip(),
                    "typed_draft": typed_draft,
                    "blocked_hook": json.loads(hook_record.read_text())
                    if hook_record.exists()
                    else None,
                    "editor_observations": observations(),
                    "last_pane": last_pane,
                    "provider_messages": len(
                        get_mock_message_requests(isolated_mock_llm_server_url)
                    ),
                }
                record_property("editor_contract", json.dumps(diagnostics))
                print(json.dumps(diagnostics))
            finally:
                subprocess.run(
                    [*tmux, "kill-server"], capture_output=True, check=False, timeout=10
                )
