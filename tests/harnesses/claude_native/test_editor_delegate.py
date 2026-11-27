"""Invalid editor delegation cannot launch a command or change the native export."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnigent.harnesses.claude_native import editor_delegate

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX editor-file ownership")


@pytest.mark.parametrize(
    "problem",
    [
        "version",
        "missing-command",
        "argument-type",
        "environment",
        "permissions",
        "symlink",
        "json",
    ],
)
def test_invalid_delegation_preserves_the_export_and_executes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    problem: str,
) -> None:
    manifest = tmp_path / "editor.json"
    configuration: dict[str, object] = {
        "version": 1,
        "command": ["/synthetic/editor", "--wait"],
        "restore_environment": {"VISUAL": "original-editor"},
        "injected_environment": {"VISUAL": "wrapper-editor"},
    }
    if problem == "version":
        configuration["version"] = 2
    elif problem == "missing-command":
        configuration["command"] = []
    elif problem == "argument-type":
        configuration["command"] = ["/synthetic/editor", 7]
    elif problem == "environment":
        configuration["restore_environment"] = {"UNRELATED_FIELD": "value"}
        configuration["injected_environment"] = {"UNRELATED_FIELD": "wrapper"}
    manifest.write_text("{" if problem == "json" else json.dumps(configuration))
    manifest.chmod(0o644 if problem == "permissions" else 0o600)
    if problem == "symlink":
        link = tmp_path / "editor-link.json"
        link.symlink_to(manifest)
        manifest = link
    draft = tmp_path / "native-draft.md"
    original = b"  original draft\n\nsecond line  \n"
    draft.write_bytes(original)
    execute = MagicMock(side_effect=AssertionError("Invalid editor configuration executed"))
    operations = {
        name: getattr(os, name)
        for name in (
            "open",
            "fstat",
            "getuid",
            "read",
            "close",
            "O_RDONLY",
            "O_NOFOLLOW",
            "O_NONBLOCK",
        )
    }
    monkeypatch.setattr(
        editor_delegate,
        "os",
        SimpleNamespace(
            **operations, environ={"TMUX_PANE": "%0"}, pathsep=os.pathsep, execvpe=execute
        ),
    )
    assert editor_delegate.main(["--manifest", str(manifest), "--", str(draft)]) == 1
    execute.assert_not_called()
    assert draft.read_bytes() == original
    assert capsys.readouterr().err.strip() == (
        "Could not open the configured editor. Check its command and retry."
    )


@pytest.mark.parametrize("newer_editor", [False, True])
def test_delegation_preserves_runtime_updates_and_native_editor_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, newer_editor: bool
) -> None:
    manifest = tmp_path / "editor.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "command": ["/synthetic/code", "--user-argument", "value with spaces"],
                "restore_environment": {"VISUAL": "original-editor", "EDITOR": None},
                "injected_environment": {"VISUAL": "proxy-visual", "EDITOR": "proxy-editor"},
                "path_prefix": "/framework-proxy",
            }
        )
    )
    manifest.chmod(0o600)

    class Executed(BaseException):
        pass

    execute = MagicMock(side_effect=Executed)
    operations = {
        name: getattr(os, name)
        for name in (
            "open",
            "fstat",
            "getuid",
            "read",
            "close",
            "O_RDONLY",
            "O_NOFOLLOW",
            "O_NONBLOCK",
        )
    }
    monkeypatch.setattr(
        editor_delegate,
        "os",
        SimpleNamespace(
            **operations,
            environ={
                "TMUX_PANE": "%7",
                "VISUAL": "newer-editor" if newer_editor else "proxy-visual",
                "EDITOR": "proxy-editor",
                "PATH": "/native-added:/framework-proxy:/framework-proxy-extra:/original/bin:",
            },
            pathsep=os.pathsep,
            execvpe=execute,
        ),
    )
    with pytest.raises(Executed):
        editor_delegate.main(["--manifest", str(manifest), "--", "-w", "-g", "draft name.md:5"])
    execute.assert_called_once()
    command, arguments, environment = execute.call_args.args
    assert command == "/synthetic/code"
    assert arguments == [
        command,
        "--user-argument",
        "value with spaces",
        "-w",
        "-g",
        "draft name.md:5",
    ]
    assert environment == {
        "TMUX_PANE": "%7",
        "VISUAL": "newer-editor" if newer_editor else "original-editor",
        "PATH": "/native-added:/framework-proxy-extra:/original/bin:",
    }
