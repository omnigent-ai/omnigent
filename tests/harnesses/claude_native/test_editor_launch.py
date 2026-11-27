"""Editor choice, launch isolation and exact delegation without starting a process."""

from __future__ import annotations

import os
import shlex
import stat
import sys
from pathlib import Path

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
