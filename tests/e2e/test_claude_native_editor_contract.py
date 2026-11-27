"""Check exact native draft export and replacement through the external editor."""

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


def test_claude_external_editor_preserves_exact_logical_drafts(
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
    editor = tmp_path / "editor.py"
    editor.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        f"instruction = Path({str(instruction)!r})\n"
        f"observed = Path({str(observed)!r})\n"
        "source = Path(sys.argv[-1])\n"
        "content = source.read_bytes().decode('utf-8')\n"
        "with observed.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'content': content}) + '\\n')\n"
        "replacement = json.loads(instruction.read_text())['replacement']\n"
        "source.write_bytes(replacement.encode('utf-8'))\n"
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
    drafts = [
        "editor-wrapped-draft " + "0123456789 " * 24 + "draft-end",
        "  editor-multiline-draft\n\nsecond line with spaces  \nthird line 😀\n",
        "editor-replacement-confirmed",
    ]

    def observations() -> list[dict[str, str]]:
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
            command("load-buffer", "-b", "editor-draft", "-", text=drafts[0])
            command("paste-buffer", "-d", "-p", "-b", "editor-draft", "-t", target)
            poll_until(lambda: "draft-end" in pane(), timeout_s=10)
            for index, expected in enumerate(drafts):
                replacement = drafts[min(index + 1, len(drafts) - 1)]
                instruction.write_text(json.dumps({"replacement": replacement}))
                command("send-keys", "-t", target, "C-g")
                poll_until(lambda index=index: len(observations()) == index + 1, timeout_s=15)
                actual = observations()[-1]["content"]
                assert actual == expected, {"step": index, "expected": expected, "actual": actual}
                poll_until(
                    lambda replacement=replacement: (
                        bridge._composer_box(pane()) is not None
                        and replacement.splitlines()[0].strip() in pane()
                    ),
                    timeout_s=10,
                )
            assert get_mock_message_requests(isolated_mock_llm_server_url) == []
        finally:
            try:
                diagnostics = {
                    "version": version.strip(),
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
