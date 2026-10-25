"""Prompt editing through the real key processor and an external editor process."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest
from omnigent_ui_sdk.terminal._host import TerminalHost
from prompt_toolkit.application.current import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from tests._helpers.polling import poll_until_async


@pytest.mark.parametrize("shortcut", ["\x07", "\x18\x05"], ids=["ctrl-g", "ctrl-x-ctrl-e"])
@pytest.mark.parametrize("editor_variable", ["EDITOR", "VISUAL"])
@pytest.mark.parametrize("exit_code", [0, 1], ids=["save", "failure"])
@pytest.mark.parametrize(
    "original",
    ["", "draft", "\n".join(f"line {i}" for i in range(22))],
    ids=["empty", "plain", "collapsed-paste"],
)
async def test_external_editor_returns_to_prompt_without_submitting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shortcut: str,
    editor_variable: str,
    exit_code: int,
    original: str,
) -> None:
    record = tmp_path / "editor-input.json"
    editor = tmp_path / "test editor.py"
    editor.write_text(
        "import json, pathlib, sys\n"
        "record, status, filename = sys.argv[1:]\n"
        "path = pathlib.Path(filename)\n"
        "captured = {'text': path.read_text(), 'path': filename}\n"
        "pathlib.Path(record).write_text(json.dumps(captured))\n"
        "path.write_text('edited draft\\nsecond line\\n')\n"
        "sys.exit(int(status))\n"
    )
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", "/nonexistent/editor")
    monkeypatch.setenv(
        editor_variable, shlex.join([sys.executable, str(editor), str(record), str(exit_code)])
    )
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host.create_output", DummyOutput)

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        host = TerminalHost(history_file=str(tmp_path / "history"))
        pending = asyncio.create_task(host._read_input())
        try:
            await poll_until_async(lambda: host._prompt.app.is_running)
            pipe.send_text("\x1b[200~" + original + "\x1b[201~")
            if "\n" in original:
                await poll_until_async(lambda: bool(host._pasted_blocks))
            else:
                await poll_until_async(lambda: host._prompt.default_buffer.text == original)
            pipe.send_text(shortcut)
            await poll_until_async(
                lambda: record.exists() and not host._prompt.app._running_in_terminal,
                timeout_s=5,
            )

            captured = json.loads(record.read_text())
            assert captured["text"] == original
            assert not Path(captured["path"]).exists()
            expected = "edited draft\nsecond line" if exit_code == 0 else original
            assert host._prompt.default_buffer.text == expected
            assert not pending.done()
            if exit_code == 0:
                host._prompt.default_buffer.undo()
                assert host._prompt.default_buffer.text == original
                host._prompt.default_buffer.redo()
                assert host._prompt.default_buffer.text == expected

            pipe.send_text("\r")
            assert await asyncio.wait_for(pending, timeout=2) == expected
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending


async def test_editor_shortcut_does_not_replace_history_search_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent_ui_sdk.terminal._host.create_output", DummyOutput)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        host = TerminalHost(history_file=str(tmp_path / "history"))
        pending = asyncio.create_task(host._read_input())
        try:
            await poll_until_async(lambda: host._prompt.app.is_running)
            pipe.send_text("draft\x12")
            await poll_until_async(lambda: host._prompt.app.layout.is_searching)
            pipe.send_text("\x07")
            await poll_until_async(lambda: not host._prompt.app.layout.is_searching)
            assert host._prompt.default_buffer.text == "draft"
            assert not pending.done()
            pipe.send_text("\r")
            assert await asyncio.wait_for(pending, timeout=2) == "draft"
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
