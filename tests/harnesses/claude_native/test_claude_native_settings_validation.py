"""Public doctor diagnostics, selected launch context, and bounded process cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, Mock, call

import psutil
import pytest

from omnigent.harnesses.claude_native import settings_validation as validation
from omnigent.inner import terminal as terminal_module
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.inner.terminal import TerminalInstance
from tests._helpers.polling import poll_until_async

_HELP = (
    "Usage: claude doctor [options]\n"
    "Reads settings files in the current directory without a trust prompt. "
    "For a full checkup that can also fix issues, run /doctor in a session.\n"
)
_HEADER = (
    "Claude Code doctor\n\nRunning: native (2.1.0)\n"
    "Commit: example-build\nPath: /opt/example/claude\n"
)
_COMPLETION = (
    "\nNo installation issues found.\n\n"
    "For a full setup checkup that can also fix issues, run /doctor in a Claude Code session.\n"
)


def _instance(tmp_path: Path) -> TerminalInstance:
    workspace, private_dir = tmp_path / "workspace", tmp_path / "terminal"
    workspace.mkdir()
    private_dir.mkdir(mode=0o700)
    return TerminalInstance(
        name="claude",
        session_key="settings",
        command="claude",
        args=["original-argument"],
        socket_path=private_dir / "tmux.sock",
        private_dir=private_dir,
        os_env=Mock(cwd=workspace),
        inherit_env=False,
        env={"PATH": "/selected/bin", "EXCLUDED": "private-value"},
    )


@pytest.mark.parametrize("reason", list(validation.CallerSettingsErrorReason))
def test_caller_settings_error_guidance_does_not_use_exception_text(
    reason: validation.CallerSettingsErrorReason,
) -> None:
    error = validation.CallerSettingsError(reason)
    message = error.client_message
    assert error.reason is reason
    assert str(error) == message
    error.args = ("private-diagnostic /private/example/settings.json",)
    assert error.client_message == message
    assert "private-diagnostic" not in error.client_message


def test_caller_settings_error_refuses_arbitrary_messages() -> None:
    with pytest.raises(TypeError, match="reason must be a CallerSettingsErrorReason"):
        validation.CallerSettingsError(
            cast(validation.CallerSettingsErrorReason, "private-diagnostic")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "rejected",
        "unsupported-help",
        "ignored-settings",
        "wrong-path",
        "incomplete",
        "changed-build",
        "unexpected-reference",
        "timeout",
    ],
)
async def test_validator_requires_complete_controls_and_keeps_the_full_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fault: str
) -> None:
    instance = _instance(tmp_path)
    secret = "private-caller-value"
    settings = {
        "attribution": False,
        "customOptions": {"future": secret},
        "remoteTools": {"allowUnattendedServing": False},
        "env": {"EXAMPLE_KEY": secret},
    }
    documents = []
    deadline_started, deadline_cancelled = asyncio.Event(), asyncio.Event()
    if fault == "timeout":
        monkeypatch.setattr(validation, "_VALIDATION_TIMEOUT", 0.05)

    async def doctor(command, args, *, cwd, env):
        assert command == "claude"
        assert "--bare" in args and args[args.index("--setting-sources") + 1] == ""
        assert cwd == instance.os_env.cwd
        assert Path(env["CLAUDE_CONFIG_DIR"]).is_relative_to(tmp_path)
        if "--help" in args:
            return ("unsupported" if fault == "unsupported-help" else _HELP), ""
        candidate = Path(args[args.index("--settings") + 1])
        document = json.loads(candidate.read_text())
        documents.append(document)
        issue = None
        if len(documents) == 2 and fault != "ignored-settings":
            issue = f"- {candidate} › hooks.PreToolUse.0: Expected object, but received null\n"
            if fault == "wrong-path":
                issue = issue.replace(str(candidate), str(candidate.with_name("different.json")))
        elif len(documents) == 3 and fault == "rejected":
            issue = f"- {candidate} › language: {secret}\n"
        body = _HEADER + ("\nInvalid settings\n" + issue if issue else "") + _COMPLETION
        if len(documents) == 3:
            if fault == "incomplete":
                body = _HEADER
            elif fault == "changed-build":
                body = body.replace("example-build", "replacement-build")
            elif fault == "unexpected-reference":
                body = f"settings={candidate}\n" + body
            elif fault == "timeout":
                deadline_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    deadline_cancelled.set()
        return body, ""

    monkeypatch.setattr(validation, "_run_doctor", doctor)
    if fault == "none":
        await validation.validate_caller_settings(instance, settings, wrapper_prefix=[])
        assert documents == [{}, {"hooks": {"PreToolUse": [None]}}, settings]
    else:
        expected = "Claude rejected" if fault == "rejected" else "Cannot validate"
        with pytest.raises(validation.CallerSettingsError, match=expected) as error:
            await asyncio.wait_for(
                validation.validate_caller_settings(instance, settings, wrapper_prefix=[]),
                timeout=1,
            )
        assert error.value.reason is (
            validation.CallerSettingsErrorReason.INVALID_SETTINGS
            if fault == "rejected"
            else validation.CallerSettingsErrorReason.VALIDATION_UNAVAILABLE
        )
        assert secret not in str(error.value)
        assert error.value.__suppress_context__
        if fault == "timeout":
            assert deadline_started.is_set()
            assert deadline_cancelled.is_set()
    assert instance.args == ["original-argument"]
    assert "disableAllHooks" not in settings
    assert secret not in caplog.text
    assert not list(tmp_path.glob("claude-settings-check-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sandboxed", "env_wrapped", "fork_executable"),
    [
        pytest.param(False, True, None, id="False"),
        pytest.param(True, True, None, id="True"),
        pytest.param(True, False, None, id="different-paths"),
        pytest.param(True, True, "bin", id="fork-bin"),
        pytest.param(True, True, "opt", id="fork-opt"),
        pytest.param(True, True, "shallow-symlink", id="fork-shallow-symlink"),
        pytest.param(True, True, "private-parent", id="fork-private-parent"),
        pytest.param(True, True, "private-symlink", id="fork-private-symlink"),
        pytest.param(True, True, "policy-root", id="fork-policy-root"),
        pytest.param(True, True, "private-read", id="fork-private-read"),
    ],
)
async def test_validator_preserves_selected_environment_wrapper_and_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sandboxed: bool,
    env_wrapped: bool,
    fork_executable: str | None,
) -> None:
    monkeypatch.setattr(terminal_module, "tmux_unavailable_reason", lambda: None)
    instance = _instance(tmp_path)
    instance.command = "env" if env_wrapped else "claude"
    selected_command = "selected-wrapper" if sandboxed and env_wrapped else "claude"
    selected_prefix = ["claude", "--"] if sandboxed and env_wrapped else []
    if env_wrapped:
        which = Mock(return_value=selected_command)
        monkeypatch.setattr(validation.shutil, "which", which)
    if fork_executable is not None:
        instance.os_env.cwd = instance.private_dir / "workspace"
        instance.os_env.cwd.mkdir()
        executable = Path("/bin/claude" if fork_executable == "bin" else "/opt/claude")
        if fork_executable == "private-parent":
            executable = instance.private_dir / "bin" / "claude"
        elif fork_executable in {"shallow-symlink", "private-symlink"}:
            target = (
                instance.private_dir / "bin" / "claude"
                if fork_executable == "private-symlink"
                else executable
            )
            executable = tmp_path / "tools" / "bin" / "claude"
            executable.parent.mkdir(parents=True)
            executable.symlink_to(target)
        selected_command = str(executable)
        which = Mock(return_value=selected_command)
        monkeypatch.setattr(validation.shutil, "which", which)
    prefix = (
        [
            *([] if sandboxed else ["-i"]),
            "-C",
            "subdir",
            "PATH=/selected/bin",
            "PRIVATE_KEY=wrapper-value",
            "CLAUDE_CONFIG_DIR=/caller-config",
            selected_command,
            *selected_prefix,
        ]
        if env_wrapped
        else []
    )
    selected_path = "/selected/bin"
    if not env_wrapped:
        runner_tools, terminal_tools = tmp_path / "runner-bin", tmp_path / "terminal-bin"
        for tools in (runner_tools, terminal_tools):
            tools.mkdir()
            (tools / "claude").touch(mode=0o700)
        monkeypatch.setenv("PATH", str(runner_tools))
        selected_path = str(terminal_tools)
        instance.env["PATH"] = selected_path
    selected_cwd = instance.os_env.cwd / "subdir" if env_wrapped else instance.os_env.cwd
    policy = SandboxPolicy(
        backend_type="linux_bwrap",
        active=True,
        read_roots=(
            [Path("/")]
            if fork_executable == "policy-root"
            else [instance.private_dir / "control"]
            if fork_executable == "private-read"
            else []
        ),
        write_roots=[instance.os_env.cwd, instance.private_dir],
        write_files=[instance.os_env.cwd / "write-file"],
        allow_network=True,
        mask_paths=[instance.os_env.cwd / "secret", instance.private_dir / "covered"],
        deny_unix_socket_paths=[instance.socket_path, instance.os_env.cwd / "other.sock"],
        credential_source_paths=[instance.os_env.cwd / "credential-file"],
        copy_on_write_roots=[instance.os_env.cwd],
        copy_on_write_namespace=(123, 456, 789),
        spawn_env_allowlist=["PATH"],
    )
    if sandboxed:
        instance.sandbox_policy = policy
    if not env_wrapped:
        startup_targets = []

        def stop_before_spawn(command, _policy, *, cwd):
            startup_targets.append(command)
            assert cwd == str(instance.private_dir)
            raise RuntimeError("stop before spawning")

        with monkeypatch.context() as startup_patch:
            startup_patch.setattr(terminal_module, "create_exec_launcher", stop_before_spawn)
            with pytest.raises(RuntimeError, match="stop before spawning"):
                await instance._launch_impl()
        assert startup_targets == [str(runner_tools / "claude")]
        selected_command = startup_targets[0]
    launches = []
    payloads = []

    def launcher(command, cloned, *, cwd, target_environment_path=None):
        launches.append((command, cloned))
        assert cwd == str(selected_cwd)
        if env_wrapped:
            assert target_environment_path is not None
            payloads.append(json.loads(Path(target_environment_path).read_text()))
        else:
            assert target_environment_path is None
        return str(tmp_path / "launcher")

    async def doctor(command, args, *, cwd, env):
        assert command == (
            str(tmp_path / "launcher") if sandboxed or env_wrapped else selected_command
        )
        assert args[: len(selected_prefix)] == selected_prefix
        assert cwd == (Path("/") if sandboxed else selected_cwd)
        assert env["PATH"] == selected_path
        if env_wrapped:
            assert "PRIVATE_KEY" not in env
            payload = payloads[-1]
            selected_env = dict(env) if payload["inherit"] else {}
            for name in payload["unset"]:
                selected_env.pop(name, None)
            selected_env.update(payload["variables"])
            selected_env.update(payload["overrides"])
            assert selected_env["PRIVATE_KEY"] == "wrapper-value"
            assert selected_env["CLAUDE_CONFIG_DIR"] == env["CLAUDE_CONFIG_DIR"]
            assert "EXCLUDED" not in selected_env
        else:
            assert "PRIVATE_KEY" not in env
        assert ("EXCLUDED" not in env) is sandboxed
        assert env["CLAUDE_CONFIG_DIR"] != "/caller-config"
        if "--help" in args:
            return _HELP, ""
        candidate = Path(args[args.index("--settings") + 1])
        issue = ""
        if json.loads(candidate.read_text()) == {"hooks": {"PreToolUse": [None]}}:
            issue = f"\nInvalid settings\n- {candidate} › hooks.PreToolUse.0: invalid\n"
        return _HEADER + issue + _COMPLETION, ""

    monkeypatch.setattr(validation, "create_exec_launcher", launcher)
    doctor_mock = AsyncMock(side_effect=doctor)
    monkeypatch.setattr(validation, "_run_doctor", doctor_mock)
    rejected = fork_executable in {
        "private-parent",
        "private-symlink",
        "policy-root",
        "private-read",
    }
    if rejected:
        with pytest.raises(validation.CallerSettingsError, match="Cannot validate"):
            await validation.validate_caller_settings(
                instance, {"attribution": False}, wrapper_prefix=prefix
            )
        doctor_mock.assert_not_awaited()
    else:
        await validation.validate_caller_settings(
            instance, {"attribution": False}, wrapper_prefix=prefix
        )
        assert doctor_mock.await_count == 4
    if fork_executable is not None:
        assert which.call_args == call(selected_command, path=selected_path)
    assert len(launches) == (4 if (sandboxed or env_wrapped) and not rejected else 0)
    assert [command for command, _ in launches] == (
        [selected_command] * 4 if (sandboxed or env_wrapped) and not rejected else []
    )
    for _, cloned in launches:
        if not sandboxed:
            assert not cloned.active
            assert cloned.backend_type == "none"
            assert "PRIVATE_KEY" not in cloned.spawn_env_allowlist
            continue
        assert not cloned.allow_network
        assert len(cloned.write_roots) == 1 and not cloned.write_files
        assert cloned.mask_paths == (
            policy.mask_paths
            if fork_executable is not None
            else [instance.os_env.cwd / "secret", instance.private_dir]
        )
        assert cloned.deny_unix_socket_paths == (
            policy.deny_unix_socket_paths
            if fork_executable is not None
            else [instance.os_env.cwd / "other.sock"]
        )
        assert cloned.credential_source_paths == policy.credential_source_paths
        assert cloned.copy_on_write_namespace == policy.copy_on_write_namespace
        assert cloned.copy_on_write_roots == policy.copy_on_write_roots
        assert "EXCLUDED" not in cloned.spawn_env_allowlist
        assert "PRIVATE_KEY" not in cloned.spawn_env_allowlist
    assert policy.allow_network and instance.socket_path in policy.deny_unix_socket_paths
    assert policy.spawn_env_allowlist == ["PATH"]
    assert not instance.socket_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "unknown-wrapper",
        "wrapped-unknown-command",
        "unsupported-env",
        "unsupported-nested-env",
        "missing-environment",
    ],
)
async def test_validator_refuses_unavailable_launch_context_without_starting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    instance = _instance(tmp_path)
    instance.command = "selected-wrapper"
    prefix = ["claude", "--"]
    if mode == "unsupported-env":
        instance.command = "env"
        prefix = ["--unsupported-option", "claude"]
    elif mode == "unsupported-nested-env":
        instance.command = "env"
        prefix = ["EXAMPLE=outer", "env", "--unsupported-option", "claude"]
    elif mode == "wrapped-unknown-command":
        instance.command = "env"
        prefix = ["EXAMPLE=wrapper-value", "selected-wrapper", "--"]
    elif mode == "missing-environment":
        instance.os_env = None
    doctor = AsyncMock()
    monkeypatch.setattr(validation, "_run_doctor", doctor)
    with pytest.raises(validation.CallerSettingsError) as error:
        await validation.validate_caller_settings(instance, {}, wrapper_prefix=prefix)
    assert error.value.reason is (
        validation.CallerSettingsErrorReason.SANDBOX_REQUIRED
        if mode in {"unknown-wrapper", "wrapped-unknown-command"}
        else validation.CallerSettingsErrorReason.VALIDATION_UNAVAILABLE
    )
    if mode in {"unknown-wrapper", "wrapped-unknown-command"}:
        assert "requires an active sandbox" in error.value.client_message
    doctor.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bwrap") is None, reason="requires Linux and bwrap"
)
@pytest.mark.parametrize("mode", ["plain", "env-chdir", "broad-read"])
async def test_validator_preserves_real_fork_workspace_and_private_control_denials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
    from omnigent.inner.terminal import create_terminal_instance

    workspace = tmp_path / "workspace"
    (workspace / "subdir").mkdir(parents=True)
    for root in (workspace, workspace / "subdir"):
        (root / "workspace-canary").write_text("readable")
        (root / "operator-hidden").write_text("private-value")
    tools = tmp_path / "tools" / "bin"
    tools.mkdir(parents=True)
    command = tools / "claude"
    command.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "assert pathlib.Path.cwd() == pathlib.Path(os.environ['EXPECTED_CWD'])\n"
        "assert pathlib.Path('workspace-canary').read_text() == 'readable'\n"
        "assert 'EXCLUDED' not in os.environ\n"
        "for name in ('PRIVATE_CANARY', 'CREDENTIAL', 'OPERATOR_MASK'):\n"
        "    try:\n"
        "        content = pathlib.Path(os.environ[name]).read_text()\n"
        "    except OSError:\n"
        "        content = ''\n"
        "    assert content != 'private-value'\n"
        "try:\n"
        "    pathlib.Path('unexpected-write').write_text('unexpected')\n"
        "except OSError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('workspace write allowed')\n"
        f"if '--help' in sys.argv: print({_HELP!r}); sys.exit(0)\n"
        "candidate = pathlib.Path(sys.argv[sys.argv.index('--settings') + 1])\n"
        "settings = json.loads(candidate.read_text())\n"
        f"print({_HEADER!r})\n"
        "if settings == {'hooks': {'PreToolUse': [None]}}:\n"
        "    print(f'Invalid settings\\n- {candidate} › hooks.PreToolUse.0: invalid')\n"
        f"print({_COMPLETION!r})\n"
    )
    command.chmod(0o700)
    wrapped = mode == "env-chdir"
    prefix = ["-C", "subdir", str(command)] if wrapped else []
    created = create_terminal_instance(
        "claude",
        "settings",
        TerminalEnvSpec(
            command="env" if wrapped else str(command),
            args=prefix,
            inherit_env=False,
            env={"PATH": os.defpath, "EXCLUDED": "private-value"},
            os_env=OSEnvSpec(
                type="caller_process",
                cwd=str(workspace),
                fork=True,
                sandbox=OSEnvSandboxSpec(
                    type="linux_bwrap",
                    allow_network=True,
                    write_paths=["."],
                    read_paths=[str(Path(__file__).resolve().parents[3]), str(Path(sys.prefix))],
                ),
            ),
        ),
    )
    instance = created.instance
    assert instance.os_env is not None and instance.sandbox_policy is not None
    selected = instance.os_env.cwd / "subdir" if wrapped else instance.os_env.cwd
    private_canary = instance.private_dir / "control-canary"
    private_canary.write_text("private-value")
    credential = tmp_path / "credential"
    credential.write_text("private-value")
    instance.env.update(
        {
            "EXPECTED_CWD": str(selected),
            "PRIVATE_CANARY": str(private_canary),
            "CREDENTIAL": str(credential),
            "OPERATOR_MASK": str(selected / "operator-hidden"),
        }
    )
    instance.sandbox_policy = replace(
        instance.sandbox_policy,
        mask_paths=[selected / "operator-hidden"],
        credential_source_paths=[credential],
        spawn_env_allowlist=[name for name in instance.env if name != "EXCLUDED"],
        read_roots=[
            *(instance.sandbox_policy.read_roots or []),
            *([instance.private_dir.parent] if mode == "broad-read" else []),
        ],
    )
    original_policy = instance.sandbox_policy.to_jsonable()
    doctor = AsyncMock(wraps=validation._run_doctor)
    monkeypatch.setattr(validation, "_run_doctor", doctor)
    try:
        if mode == "broad-read":
            with pytest.raises(ValueError, match="Cannot validate"):
                await validation.validate_caller_settings(
                    instance, {"attribution": False}, wrapper_prefix=prefix
                )
            doctor.assert_not_awaited()
        else:
            await validation.validate_caller_settings(
                instance, {"attribution": False}, wrapper_prefix=prefix
            )
            assert doctor.await_count == 4
        assert instance.sandbox_policy.to_jsonable() == original_policy
        assert not instance.socket_path.exists()
        assert not (selected / "unexpected-write").exists()
        assert private_canary.read_text() == "private-value"
    finally:
        await instance.close()


@pytest.mark.asyncio
@pytest.mark.posix_only
@pytest.mark.parametrize(
    "failure",
    [
        "success",
        "nonzero",
        "cancel",
        "double-cancel",
        "cleanup-cancel",
        "closed-pipes-descendant",
        "output-limit",
    ],
)
async def test_validator_closes_process_tree_on_completion_or_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    pid_file = tmp_path / "pid"
    descendant_pid_file = tmp_path / "descendant-pid"
    descendant_script = (
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        + ("os.close(1); os.close(2)\n" if failure == "closed-pipes-descendant" else "")
        + f"pathlib.Path({str(descendant_pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    script = (
        "import os, pathlib, subprocess, sys, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        + (
            f"sys.stdout.write('x' * {validation._MAX_OUTPUT_BYTES + 1}); sys.stdout.flush()\n"
            if failure == "output-limit"
            else ""
        )
        + (
            f"subprocess.Popen([sys.executable, '-c', {descendant_script!r}])\n"
            if failure in {"double-cancel", "closed-pipes-descendant"}
            else f"print('complete'); sys.exit({7 if failure == 'nonzero' else 0})\n"
            if failure in {"success", "nonzero", "cleanup-cancel"}
            else "time.sleep(30)\n"
        )
    )
    processes = []
    spawn = validation.asyncio.create_subprocess_exec

    async def capture_process(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(validation.asyncio, "create_subprocess_exec", capture_process)
    task = asyncio.create_task(
        validation._run_doctor(
            sys.executable, ["-c", script], cwd=tmp_path, env={"PATH": os.defpath}
        )
    )
    cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event()
    terminate = validation.terminate_subprocess
    cleaning = []
    descendant: psutil.Process | None = None

    async def delayed_cleanup(*args, **kwargs):
        cleaning.append(args[0])
        cleanup_started.set()
        await finish_cleanup.wait()
        return await terminate(*args, **kwargs)

    if failure in {"double-cancel", "cleanup-cancel", "closed-pipes-descendant"}:
        monkeypatch.setattr(validation, "terminate_subprocess", delayed_cleanup)
    try:
        await poll_until_async(pid_file.exists, timeout_s=5)
        pid = int(pid_file.read_text())
        if failure in {"double-cancel", "closed-pipes-descendant"}:
            await poll_until_async(descendant_pid_file.exists, timeout_s=5)
            descendant = psutil.Process(int(descendant_pid_file.read_text()))
        if failure in {"cancel", "double-cancel", "cleanup-cancel"}:
            if failure == "cleanup-cancel":
                await asyncio.wait_for(cleanup_started.wait(), timeout=5)
            task.cancel()
            if failure == "double-cancel":
                await asyncio.wait_for(cleanup_started.wait(), timeout=5)
                task.cancel()
            if failure in {"double-cancel", "cleanup-cancel"}:
                finish_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        elif failure == "closed-pipes-descendant":
            await asyncio.wait_for(cleanup_started.wait(), timeout=5)
            finish_cleanup.set()
            assert await asyncio.wait_for(task, timeout=5) == ("", "")
        elif failure in {"nonzero", "output-limit"}:
            with pytest.raises(validation._ValidationUnavailable):
                await asyncio.wait_for(task, timeout=5)
        else:
            assert await asyncio.wait_for(task, timeout=5) == ("complete\n", "")
        assert len(processes) == 1
        assert processes[0].returncode is not None
        assert not psutil.pid_exists(processes[0].pid)
        assert processes[0]._transport.is_closing()
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        if failure in {"double-cancel", "closed-pipes-descendant"}:
            assert descendant is not None
            await poll_until_async(
                lambda: not descendant.is_running() or descendant.status() == psutil.STATUS_ZOMBIE,
                timeout_s=5,
            )
        if failure in {"double-cancel", "cleanup-cancel", "closed-pipes-descendant"}:
            assert len(cleaning) == 1
            assert cleaning[0].returncode is not None
            assert cleaning[0]._transport.is_closing()
    finally:
        finish_cleanup.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if descendant is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                descendant.kill()
