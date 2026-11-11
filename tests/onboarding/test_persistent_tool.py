"""Credential-helper setup keeps the selected source and native auth arguments."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.onboarding import persistent_tool
from omnigent.onboarding.persistent_tool import main, persistent_tool_command, prepare_tool


def test_pinned_tool_resolution_defers_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))

    def unexpected_install(*args: object, **kwargs: object) -> None:
        raise AssertionError("Resolution must stay off the host's connection path")

    monkeypatch.setattr("subprocess.run", unexpected_install)

    @persistent_tool_command
    def resolve() -> list[str]:
        return ["/usr/bin/uvx", "--from", "helper-package==1.0", "gateway-helper"]

    command = resolve()
    assert command[:2] == [sys.executable, "-I"]
    assert Path(command[2]).name == "persistent_tool.py"
    assert Path(command[3]).parent == tmp_path / "config" / "tools"
    assert command[4:] == ["helper-package==1.0", "gateway-helper"]
    assert command == resolve()
    assert not (tmp_path / "config").exists()


@pytest.mark.parametrize(
    "prefix",
    [
        ["/selected/bin/helper"],
        ["sh", "-c", "custom-helper"],
        ["custom-runner", "--from", "selected-source", "helper"],
    ],
)
def test_custom_tool_commands_keep_the_selected_executable(prefix: list[str]) -> None:
    @persistent_tool_command
    def resolve() -> list[str]:
        return prefix

    assert resolve() == prefix


@pytest.mark.parametrize("installed", [False, True])
def test_persistent_installation_is_reused_without_cache_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: bool
) -> None:
    executable = tmp_path / "bin" / ("gateway-helper.exe" if os.name == "nt" else "gateway-helper")
    monkeypatch.setenv("UV_LINK_MODE", "symlink")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "selected-account")
    installs: list[list[str]] = []

    def install(
        argv: list[str], *, env: dict[str, str], check: bool, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        installs.append(argv)
        assert env["UV_TOOL_BIN_DIR"] == str(executable.parent)
        assert env["UV_TOOL_DIR"] == str(tmp_path / "environments")
        assert env["DATABRICKS_CONFIG_PROFILE"] == "selected-account"
        executable.parent.mkdir(parents=True)
        executable.write_text("helper fixture")
        executable.chmod(0o700)
        return subprocess.CompletedProcess(argv, 0)

    if installed:
        executable.parent.mkdir()
        executable.write_text("helper fixture")
        executable.chmod(0o700)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr("subprocess.run", install)

    assert prepare_tool(tmp_path, "helper-package==1.0", "gateway-helper") == executable
    assert installs == (
        []
        if installed
        else [
            [
                "/usr/bin/uv",
                "tool",
                "install",
                "--force",
                "--link-mode",
                "copy",
                "helper-package==1.0",
            ]
        ]
    )
    assert prepare_tool(tmp_path, "helper-package==1.0", "gateway-helper") == executable
    assert len(installs) == (0 if installed else 1)


def test_concurrent_setup_reuses_one_complete_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    installing, release, second_started = Event(), Event(), Event()
    executable = tmp_path / "bin" / ("gateway-helper.exe" if os.name == "nt" else "gateway-helper")
    installs: list[list[str]] = []

    def install(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        installs.append(argv)
        installing.set()
        assert release.wait(5)
        executable.parent.mkdir()
        executable.write_text("complete helper fixture")
        executable.chmod(0o700)
        return subprocess.CompletedProcess(argv, 0)

    def prepare(second: bool = False) -> Path:
        if second:
            second_started.set()
        return prepare_tool(tmp_path, "helper-package==1.0", "gateway-helper")

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr("subprocess.run", install)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(prepare)
        try:
            assert installing.wait(5)
            second = executor.submit(prepare, True)
            assert second_started.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5) == executable
        assert second.result(timeout=5) == executable
    assert len(installs) == 1


@pytest.mark.parametrize("failure", ["exit", "missing-launcher", "timeout", "missing-installer"])
def test_failed_setup_does_not_choose_another_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name: None if failure == "missing-installer" else "/usr/bin/uv"
    )

    def install(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 120)
        if failure == "exit":
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("subprocess.run", install)
    with pytest.raises((RuntimeError, subprocess.SubprocessError)):
        prepare_tool(tmp_path, "helper-package==1.0", "gateway-helper")


def test_configuration_executes_with_the_selected_profile_and_helper_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "bin" / ("gateway-helper.exe" if os.name == "nt" else "gateway-helper")
    executable.parent.mkdir()
    executable.write_text("helper fixture")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", "/other/bin")
    monkeypatch.setenv("DATABRICKS_BEARER_COMMAND", "selected-broker --token")
    executions: list[tuple[str, list[str], dict[str, str]]] = []

    def execute(path: str, argv: list[str], env: dict[str, str]) -> None:
        executions.append((path, argv, env))
        raise SystemExit(0)

    monkeypatch.setattr("os.execve", execute)
    with pytest.raises(SystemExit) as result:
        main(
            [
                str(tmp_path),
                "helper-package==1.0",
                "gateway-helper",
                "configure",
                "--profiles",
                "selected account",
                "--use-pat",
            ]
        )
    assert result.value.code == 0
    path, argv, env = executions[0]
    assert path == str(executable)
    assert argv == [str(executable), "configure", "--profiles", "selected account", "--use-pat"]
    assert env["PATH"] == f"{executable.parent}{os.pathsep}/other/bin"
    assert env["DATABRICKS_BEARER_COMMAND"] == "selected-broker --token"
    assert os.environ["PATH"] == "/other/bin"


def test_setup_failure_exits_before_running_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert main([str(tmp_path), "helper-package==1.0", "gateway-helper", "configure"]) == 1
    assert "Install uv" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="fixture uses a POSIX script entrypoint")
def test_setup_process_generates_a_durable_auth_entrypoint_from_another_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "persistent helper"
    executable = root / "bin" / "gateway-helper"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, shutil, sys\n"
        "print(json.dumps({'command': shutil.which('gateway-helper'), "
        "'args': sys.argv[1:], 'profile': os.environ['DATABRICKS_CONFIG_PROFILE']}))\n"
    )
    executable.chmod(0o700)
    workspace = tmp_path / "unrelated workspace"
    workspace.mkdir()
    arguments = ["configure", "--profile", "selected account; $(literal)"]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(persistent_tool.__file__).resolve()),
            str(root),
            "helper-package==1.0",
            "gateway-helper",
            *arguments,
        ],
        env={**os.environ, "DATABRICKS_CONFIG_PROFILE": "selected-account"},
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {
        "command": str(executable),
        "args": arguments,
        "profile": "selected-account",
    }


def test_incomplete_configuration_command_is_rejected() -> None:
    assert main([]) == 2
