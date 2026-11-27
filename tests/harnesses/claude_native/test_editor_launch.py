"""Editor choice, launch isolation and exact delegation without starting a process."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import editor_delegate, editor_launch


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


@pytest.fixture(autouse=True)
def private_bridge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(editor_launch.bridge, "_TRUSTED_PARENT", tmp_path.parent)
    monkeypatch.setattr(editor_launch.bridge, "_BRIDGE_ROOT", tmp_path)


@pytest.mark.parametrize("source", ["configured", "configured-code", "default"])
def test_native_editor_names_arguments_and_environment_survive_the_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    binary = _executable(tmp_path / "claude")
    programs = tmp_path / "programs"
    programs.mkdir()
    code = _executable(programs / "code")
    _executable(programs / "vi")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir(mode=0o700)
    environment = {"PATH": str(programs)}
    if source == "configured":
        environment["VISUAL"] = shlex.join([sys.executable, "my editor.py", "--wait"])
        environment["EDITOR"] = shlex.join([str(programs / "vi"), "-f"])
        expected_program, original_args = sys.executable, ["my editor.py", "--wait"]
    else:
        expected_program, original_args = str(code), []
        if source == "configured-code":
            original_args = ["--reuse-window"]
            environment["VISUAL"] = environment["EDITOR"] = shlex.join([str(code), *original_args])
    original = dict(environment)
    overrides = editor_launch.prepare_editor_environment(
        bridge_dir, executable=binary, environment=environment
    )
    assert overrides is not None and environment == original
    launch_environment = environment | overrides
    for key in ("VISUAL", "EDITOR", "PATH"):
        if key in launch_environment:
            monkeypatch.setenv(key, launch_environment[key])
        else:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("EDITOR_RUNTIME_UPDATE", "preserved")
    if source == "default":
        shim = Path(overrides["PATH"].split(os.pathsep)[0]) / "code"
        assert "VISUAL" not in overrides and "EDITOR" not in overrides
        assert {p.name for p in shim.parent.iterdir()} == {"code", "vi"}
    else:
        injected = shlex.split(overrides["VISUAL"])
        shim = Path(injected[0])
        assert injected[1:] == original_args
    assert shim.name == Path(expected_program).name
    command = shlex.split(shim.read_text().splitlines()[1])
    assert command[:5] == ["exec", sys.executable, "-I", "-m", editor_delegate.__name__]
    assert command[-2:] == ["--", "$@"]
    manifest = Path(command[command.index("--manifest") + 1])
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert stat.S_IMODE(shim.stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest.parent.stat().st_mode) == 0o700
    delegated, restored = editor_delegate._read_delegate(manifest)
    assert delegated == [expected_program]
    assert {k: restored.get(k) for k in ("VISUAL", "EDITOR", "PATH")} == {
        k: original.get(k) for k in ("VISUAL", "EDITOR", "PATH")
    }
    assert restored["EDITOR_RUNTIME_UPDATE"] == "preserved"
    called: list[tuple[str, list[str], dict[str, str]]] = []

    class Delegated(BaseException):
        pass

    def execute(program: str, arguments: list[str], env: dict[str, str]) -> None:
        called.append((program, arguments, env))
        raise Delegated

    monkeypatch.setattr(editor_delegate.os, "execvpe", execute)
    native_args = (
        ["--wait", "-g", "/tmp/export.md:3:2"] if "code" in source else ["/tmp/export.md"]
    )
    with pytest.raises(Delegated):
        editor_delegate.main([*command[5:-2], "--", *original_args, *native_args])
    assert called == [
        (expected_program, [expected_program, *original_args, *native_args], restored)
    ]


@pytest.mark.parametrize(
    "value",
    ["missing-editor --wait", "./relative-editor", "'unterminated", "vim; exit", "vim $FILE"],
)
def test_unsupported_commands_preserve_the_original_environment(
    tmp_path: Path, value: str
) -> None:
    binary = _executable(tmp_path / "claude")
    environment = {"VISUAL": value, "PATH": str(tmp_path)}
    original = dict(environment)
    assert (
        editor_launch.prepare_editor_environment(
            tmp_path, executable=binary, environment=environment
        )
        is None
    )
    assert environment == original and not (tmp_path / "editor-launches").exists()


def test_a_new_launch_keeps_previous_editor_delegates_immutable(tmp_path: Path) -> None:
    binary = _executable(tmp_path / "claude")
    editor = _executable(tmp_path / "vi")
    first = editor_launch.prepare_editor_environment(
        tmp_path, executable=binary, environment={"EDITOR": str(editor)}
    )
    assert first is not None
    files = {p: p.read_bytes() for p in (tmp_path / "editor-launches").rglob("*") if p.is_file()}
    second = editor_launch.prepare_editor_environment(
        tmp_path, executable=binary, environment={"EDITOR": str(editor) + " -f"}
    )
    assert second is not None and first != second
    assert all(path.read_bytes() == content for path, content in files.items())


@pytest.mark.parametrize("routing", [False, True])
def test_owner_hooks_keep_the_existing_recorder_and_routing_hooks(
    tmp_path: Path, routing: bool
) -> None:
    binary = tmp_path / "claude with spaces"
    root = tmp_path / "bridge"
    root.mkdir(mode=0o700)
    original = editor_launch.bridge.build_hook_settings(root, turn_routing=routing)
    prepared = editor_launch.bridge.build_hook_settings(
        root, turn_routing=routing, native_editor_executable=binary
    )

    def commands(settings: dict[str, Any], event: str) -> list[str]:
        return [h["command"] for group in settings["hooks"][event] for h in group["hooks"]]

    for event in ("SessionStart", "UserPromptSubmit"):
        before, after = commands(original, event), commands(prepared, event)
        assert after[:2] == before[:2]
        owner_commands = [command for command in after if "--native-editor-executable" in command]
        assert len(owner_commands) == 1
        command = owner_commands[0].split(" 2>>", 1)[0]
        arguments = shlex.split(command)
        assert arguments[arguments.index("--native-editor-executable") + 1] == str(binary)
        if event == "UserPromptSubmit" and routing:
            assert "route-turn" in arguments and "editor-owner" not in arguments
        else:
            assert "editor-owner" in arguments
    for event in set(original["hooks"]) - {"SessionStart", "UserPromptSubmit"}:
        assert prepared["hooks"][event] == original["hooks"][event]


@pytest.mark.parametrize(
    "wrapper",
    ["direct", "env", "clear-env", "replace-editor", "remove-path", "unknown", "split-editor"],
)
def test_terminal_preparation_preserves_wrapper_arguments_and_editor_environment(
    tmp_path: Path, wrapper: str
) -> None:
    binary = _executable(tmp_path / "claude")
    editor = _executable(tmp_path / "vi")
    environment = {"PATH": str(tmp_path), "EDITOR": str(editor)}
    command, arguments = str(binary), ["--resume", "native-session"]
    if wrapper != "direct":
        command = "/usr/bin/env"
        prefix = {
            "env": ["-u", "UNUSED", "EXTRA=preserved"],
            "clear-env": ["-i"],
            "replace-editor": ["EDITOR=another-editor"],
            "remove-path": ["-u", "PATH"],
            "unknown": ["--unknown-option"],
            "split-editor": ["-S", str(binary) + " --label=${EDITOR}"],
        }[wrapper]
        arguments = (
            [*prefix, *arguments]
            if wrapper == "split-editor"
            else [*prefix, str(binary), *arguments]
        )
    original_environment, original_arguments = dict(environment), list(arguments)
    prepared = editor_launch.prepare_terminal_editor_environment(
        tmp_path / "bridge",
        command=command,
        arguments=arguments,
        environment=environment,
    )
    assert environment == original_environment and arguments == original_arguments
    if wrapper not in {"direct", "env"}:
        assert prepared is None
        return
    assert prepared is not None and prepared.executable == binary
    assert set(prepared.overrides) == {"EDITOR"}
    shim = Path(shlex.split(prepared.overrides["EDITOR"])[0])
    invocation = shlex.split(shim.read_text().splitlines()[1])
    manifest = Path(invocation[invocation.index("--manifest") + 1])
    value = json.loads(manifest.read_text())
    assert value["command"] == [str(editor)]
    assert value["restore_environment"] == {"EDITOR": str(editor)}


@pytest.mark.parametrize("supported_editor", [False, True])
def test_cli_terminal_request_pairs_prepared_delegates_with_its_hook_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supported_editor: bool
) -> None:
    from omnigent.harnesses.claude_native import main

    binary = _executable(tmp_path / "claude")
    editor = _executable(tmp_path / "vi")
    original_editor = str(editor) if supported_editor else "missing-editor --wait"
    monkeypatch.setenv("EDITOR", original_editor)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("OMNIGENT_CLAUDE_LAUNCHER", raising=False)
    root = tmp_path / "bridge"
    request = main._claude_terminal_request((), command=str(binary), bridge_dir=root)
    specification = request["spec"]
    assert specification["command"] == str(binary)
    arguments = specification["args"]
    settings = json.loads(Path(arguments[arguments.index("--settings") + 1]).read_text())
    assert os.environ["EDITOR"] == original_editor
    commands = [
        hook["command"] for group in settings["hooks"]["SessionStart"] for hook in group["hooks"]
    ]
    assert any("editor-owner" in command for command in commands) is supported_editor
    if supported_editor:
        assert str(root / "editor-launches") in specification["env"]["EDITOR"]
    else:
        assert "EDITOR" not in specification["env"]
