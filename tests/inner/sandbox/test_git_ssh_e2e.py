"""Git SSH clone through an active OS sandbox and the parent-side broker."""

from __future__ import annotations

import shlex
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import omnigent.inner.os_env as os_env_module
import omnigent.inner.terminal as terminal_module
from omnigent.inner.datamodel import GitSshBinding, OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.os_env import create_os_environment
from omnigent.inner.terminal import create_terminal_instance
from tests.inner.sandbox.conftest import run_async


def test_git_ssh_clone_inside_real_sandbox(
    ssh_git_server: tuple[GitSshBinding, str, Path],
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
) -> None:
    binding, url, _ = ssh_git_server
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = active_sandbox_spec_factory(write_paths=["."], git_ssh=[binding])
    environment = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=sandbox)
    )
    assert environment is not None
    try:
        command = (
            f"test ! -r {shlex.quote(binding.identity_file)} && "
            f"test ! -r {shlex.quote(binding.known_hosts_file)} && "
            f"git clone --branch main {shlex.quote(url)} cloned"
        )
        result = run_async(environment.shell(command, timeout=45))
        assert result.get("exit_code") == 0, result.get("stderr")
        assert (workspace / "cloned" / "README.md").read_text() == "Git SSH fixture\n"
        denied_url = url.replace("repo.git", "other.git")
        other = run_async(environment.shell(f"git ls-remote {shlex.quote(denied_url)}"))
        assert other.get("exit_code") != 0 and "not allowed" in other.get("stderr", "")
        push = run_async(environment.shell(f"git -C cloned push {shlex.quote(url)} HEAD:denied"))
        assert push.get("exit_code") != 0 and "not allowed" in push.get("stderr", "")
        python = shlex.quote(sys.executable)
        smoke = run_async(environment.shell(f"{python} -I -c 'print(123)'"))
        assert smoke.get("exit_code") == 0 and "123" in smoke.get("stdout", ""), smoke
        probe = f"import socket; socket.create_connection(('127.0.0.1', {binding.port}), 1)"
        direct = run_async(environment.shell(f"{python} -I -c {shlex.quote(probe)}"))
        assert direct.get("exit_code") != 0, direct
        assert any(
            detail in direct.get("stderr", "")
            for detail in ("PermissionError", "ConnectionRefusedError", "Network is unreachable")
        ), direct
    finally:
        environment.close()


def test_git_ssh_clone_inside_sandboxed_terminal(
    ssh_git_server: tuple[GitSshBinding, str, Path],
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
) -> None:
    binding, url, _ = ssh_git_server
    workspace = tmp_path / "terminal-workspace"
    workspace.mkdir()
    marker = workspace / "result"
    command = (
        f"test ! -r {shlex.quote(binding.identity_file)} && "
        f"git clone --branch main {shlex.quote(url)} cloned && "
        f"! git ls-remote {shlex.quote(url.replace('repo.git', 'other.git'))} >/dev/null 2>&1 && "
        f"! git -C cloned push {shlex.quote(url)} HEAD:denied >/dev/null 2>&1 "
        f"&& printf success > {shlex.quote(str(marker))} "
        f"|| printf failure > {shlex.quote(str(marker))}"
    )
    spec = TerminalEnvSpec(
        command="/bin/bash",
        args=["-lc", command],
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=active_sandbox_spec_factory(write_paths=["."], git_ssh=[binding]),
        ),
    )
    instance = create_terminal_instance(name="git-ssh", session_key="test", spec=spec).instance
    try:
        run_async(instance.launch(cwd=workspace))
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.exists(), run_async(instance.read()).get("screen")
        assert marker.read_text() == "success", run_async(instance.read()).get("screen")
        assert (workspace / "cloned" / "README.md").read_text() == "Git SSH fixture\n"
    finally:
        run_async(instance.close())


def test_git_ssh_helper_start_failure_closes_broker(
    ssh_git_server: tuple[GitSshBinding, str, Path],
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, _, _ = ssh_git_server
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = active_sandbox_spec_factory(git_ssh=[binding])
    environment = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=sandbox)
    )
    assert environment is not None
    brokers = []
    original_start = os_env_module.start_git_ssh_broker

    def capture_start(bindings: list[GitSshBinding], folder: Path):
        broker = original_start(bindings, folder)
        brokers.append(broker)
        return broker

    backend = os_env_module.get_backend(sandbox.type)
    monkeypatch.setattr(os_env_module, "start_git_ssh_broker", capture_start)
    monkeypatch.setattr(
        backend,
        "wrap_launcher_argv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("wrap failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="wrap failed"):
            run_async(environment.shell("true"))
        assert brokers and brokers[0]._closed
        assert not brokers[0].socket_path.exists()
    finally:
        environment.close()


def test_git_ssh_terminal_start_failure_closes_broker(
    ssh_git_server: tuple[GitSshBinding, str, Path],
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding, _, _ = ssh_git_server
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = TerminalEnvSpec(
        command="/bin/bash",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=active_sandbox_spec_factory(git_ssh=[binding]),
        ),
    )
    instance = create_terminal_instance(name="git-ssh", session_key="test", spec=spec).instance
    brokers = []
    original_start = terminal_module.start_git_ssh_broker

    def capture_start(bindings: list[GitSshBinding], folder: Path):
        broker = original_start(bindings, folder)
        brokers.append(broker)
        return broker

    monkeypatch.setattr(terminal_module, "start_git_ssh_broker", capture_start)
    monkeypatch.setattr(
        terminal_module,
        "create_exec_launcher",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("launcher failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="launcher failed"):
            run_async(instance.launch(cwd=workspace))
        assert brokers and brokers[0]._closed
        assert not brokers[0].socket_path.exists()
    finally:
        run_async(instance.close())
