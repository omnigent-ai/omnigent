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


@pytest.mark.parametrize("editor_source", ["configured", "default"])
def test_claude_external_editor_refuses_mismatches_without_mutating_the_draft(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    claude_managed_settings: None,
    record_property: Callable[[str, object], None],
    editor_source: str,
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
    process_context = (
        "import os, psutil\n"
        "def native_context():\n"
        "    parents = psutil.Process().parents()[:3]\n"
        "    return {'parents': [{'pid': p.pid, 'created': p.create_time(), "
        "'name': p.name(), 'exe': p.exe()} for p in parents], "
        "'tty': os.ttyname(0) if os.isatty(0) else None, "
        "'session_env': {key: os.environ.get(key) for key in "
        "('CLAUDE_CODE_SESSION_ID', 'CLAUDE_SESSION_ID', "
        "'CLAUDE_CODE_PARENT_SESSION_ID', 'TMUX_PANE')}}\n"
    )
    hook = tmp_path / "block-prompt.py"
    hook.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        + process_context
        + f"destination = Path({str(hook_record)!r})\n"
        "payload = json.load(sys.stdin)\n"
        "temporary = destination.with_suffix('.tmp')\n"
        "record = {key: payload.get(key) for key in "
        "('prompt', 'session_id', 'hook_event_name')}\n"
        "record['native_context'] = native_context()\n"
        "temporary.write_text(json.dumps(record))\n"
        "temporary.replace(destination)\n"
        "print(json.dumps({'decision': 'block', "
        "'reason': 'blocked-draft-ownership-probe'}))\n"
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
        + process_context
        + f"instruction = Path({str(instruction)!r})\n"
        f"observed = Path({str(observed)!r})\n"
        "source = Path(sys.argv[-1])\n"
        "content = source.read_bytes().decode('utf-8')\n"
        "request = json.loads(instruction.read_text())\n"
        "accepted = content == request['expected']\n"
        "program = sys.argv[sys.argv.index('--editor-program') + 1]\n"
        "with observed.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'content': content, 'accepted': accepted, "
        "'program': program, 'native_context': native_context()}) + '\\n')\n"
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
        "EDITOR": editor_command + " --editor-program editor",
        "VISUAL": editor_command + " --editor-program visual",
    }
    if editor_source == "default":
        env.pop("EDITOR")
        env.pop("VISUAL")
        programs = tmp_path / "editors"
        programs.mkdir()
        for program in (
            "vi",
            "vim",
            "nano",
            "emacs",
            "nvim",
            "code",
            "code-insiders",
            "cursor",
            "zed",
            "subl",
            "editor",
            "sensible-editor",
        ):
            launcher = programs / program
            launcher.write_text(
                "#!/bin/sh\nexec "
                + shlex.join([sys.executable, str(editor), "--editor-program", program])
                + ' "$@"\n'
            )
            launcher.chmod(0o700)
        env["PATH"] = str(programs) + os.pathsep + env["PATH"]
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
                    "editor_source": editor_source,
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
