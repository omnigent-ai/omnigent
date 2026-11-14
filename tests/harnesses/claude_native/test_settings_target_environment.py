"""Settings environment handoff using inert executable files and mocked processes."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from omnigent.harnesses.claude_native import settings_validation as validation
from omnigent.inner import sandbox as sb

_SENTINEL = "inert-target-value with whitespace"
_HELP = (
    "Usage: claude doctor [options]\nReads settings files\n"
    "For a full setup checkup that can also fix issues, run /doctor in a session.\n"
)
_HEADER = "Claude Code doctor\nRunning: fixture\nCommit: fixture-build\nPath: /fixture/claude\n"
_COMPLETION = (
    "For a full setup checkup that can also fix issues, run /doctor in a Claude Code session.\n"
)


def _context(tmp_path: Path, *, sandboxed: bool, form: str):
    workspace, private = tmp_path / "workspace", tmp_path / "terminal"
    workspace.mkdir()
    (workspace / "subdir").mkdir()
    private.mkdir(mode=0o700)
    tools = tmp_path / "selected" / "bin"
    tools.mkdir(parents=True)
    command = "selected-wrapper" if sandboxed else "claude"
    executable = tools / command
    executable.touch(mode=0o700)
    cli_prefix = ["claude", "--"] if sandboxed else []
    instance = SimpleNamespace(
        command="env",
        args=["original-argument"],
        private_dir=private,
        socket_path=private / "tmux.sock",
        os_env=SimpleNamespace(cwd=workspace),
        inherit_env=False,
        env_unset=[],
        env={"PATH": "/bootstrap/bin", "KEEP": "bootstrap", "REMOVE": "discard"},
        sandbox_policy=None,
    )
    if sandboxed:
        instance.sandbox_policy = sb.SandboxPolicy(
            backend_type="linux_bwrap",
            active=True,
            read_roots=[],
            write_roots=[workspace, private],
            write_files=[workspace / "write-file"],
            allow_network=True,
            mask_paths=[workspace / "secret", private / "covered"],
            deny_unix_socket_paths=[instance.socket_path, workspace / "other.sock"],
            credential_source_paths=[workspace / "credential-file"],
            copy_on_write_roots=[workspace],
            copy_on_write_namespace=(123, 456, 789),
            spawn_env_allowlist=["PATH", "KEEP", "REMOVE"],
        )
    variables = {
        "PATH": str(tools),
        "EXAMPLE_TARGET_ONLY": _SENTINEL,
        "HOME": "/caller-home",
        "TMPDIR": "/caller-temp",
        "DISABLE_AUTOUPDATER": "0",
        "OMNIGENT_LAUNCHER_SPAWN_WRAPPED": "inert-wrapper-marker",
        "PYTHONPATH": "/fixture/unused-python-path",
        "LD_PRELOAD": "/fixture/unused-loader-value",
    }
    operands = [*(f"{name}={value}" for name, value in variables.items()), command, *cli_prefix]
    options = ["-u", "REMOVE", "-C", "subdir"]
    if form == "reset":
        prefix = ["-i", *options, *operands]
    elif form == "nested-unset":
        prefix = [
            "-C",
            "subdir",
            "REMOVE=outer",
            "EXAMPLE_TARGET_ONLY=outer",
            "env",
            "-u",
            "REMOVE",
            "-u",
            "EXAMPLE_TARGET_ONLY",
            *operands,
        ]
    elif form == "nested-reset":
        prefix = ["-C", "subdir", "DISCARDED=outer", "env", "-i", *operands]
    elif form == "split":
        prefix = [*options, "-S", shlex.join(operands)]
    else:
        prefix = [*options, *operands]
    return SimpleNamespace(
        instance=instance,
        prefix=prefix,
        original_prefix=list(prefix),
        executable=executable,
        cli_prefix=cli_prefix,
        cwd=workspace / "subdir",
        variables=variables,
        inherit=form not in {"reset", "nested-reset"},
        sandboxed=sandboxed,
        launches=[],
        payloads=[],
        payload_paths=[],
        launcher_paths=[],
        bootstrap=[],
        finals=[],
        documents=[],
        original_policy=copy.deepcopy(instance.sandbox_policy),
    )


def _install_doubles(context, monkeypatch: pytest.MonkeyPatch, *, handoff=False, fault=None):
    def launcher(command, policy, *, cwd, target_environment_path=None):
        assert command == str(context.executable)
        assert cwd == str(context.cwd)
        assert target_environment_path is not None
        payload = Path(target_environment_path)
        assert payload.stat().st_mode & 0o777 == 0o600
        assert payload.parent.stat().st_mode & 0o777 == 0o700
        assert (
            any(payload.is_relative_to(root) for root in policy.write_roots) or not policy.active
        )
        context.payload_paths.append(payload)
        context.payloads.append(json.loads(payload.read_text()))
        path = payload.parent / "inert-launcher"
        path.touch(mode=0o700)
        context.launcher_paths.append(path)
        context.launches.append((command, policy))
        if fault == "launcher":
            raise RuntimeError("inert launcher failure")
        return str(path)

    async def doctor(command, args, *, cwd, env):
        assert command == str(context.launcher_paths[-1])
        assert cwd == (Path("/") if context.sandboxed else context.cwd)
        assert env["PATH"] == "/bootstrap/bin"
        assert "EXAMPLE_TARGET_ONLY" not in env
        assert "OMNIGENT_LAUNCHER_SPAWN_WRAPPED" not in env
        assert "PYTHONPATH" not in env and "LD_PRELOAD" not in env
        assert _SENTINEL not in json.dumps([command, args, env])
        assert args[: len(context.cli_prefix)] == context.cli_prefix
        context.bootstrap.append(dict(env))
        if fault == "doctor":
            raise RuntimeError("inert doctor failure")
        if fault == "cancel":
            raise asyncio.CancelledError
        if handoff:
            policy = context.launches[-1][1]
            scratch = context.payload_paths[-1].parent / "launcher-scratch"
            scratch.mkdir(mode=0o700)
            final = Mock(return_value=SimpleNamespace(returncode=0))

            def activate_without_target_environment(_policy):
                assert "EXAMPLE_TARGET_ONLY" not in os.environ

            activate = Mock(side_effect=activate_without_target_environment)
            with monkeypatch.context() as inner:
                inner.setattr(sb, "activate_sandbox", activate)
                inner.setattr(
                    sb, "get_backend", Mock(side_effect=AssertionError("unexpected backend"))
                )
                inner.setattr(sb, "create_private_tmpdir", Mock(return_value=scratch))
                inner.setattr(sb, "cleanup_private_tmpdir", Mock())
                inner.setattr(sb.subprocess, "run", final)
                inner.setattr(sb.os, "execvp", Mock(side_effect=AssertionError("unexpected exec")))
                bootstrap = dict(env)
                if policy.active:
                    bootstrap[sb._LAUNCHER_WRAPPED_ENV] = "1"
                with patch.dict(os.environ, bootstrap, clear=True):
                    encoded = base64.urlsafe_b64encode(
                        json.dumps(policy.to_jsonable()).encode()
                    ).decode()
                    assert (
                        sb.run_launcher(
                            encoded,
                            str(context.executable),
                            list(args),
                            cwd=str(context.cwd),
                            target_environment_path=str(context.payload_paths[-1]),
                        )
                        == 0
                    )
                    assert "EXAMPLE_TARGET_ONLY" not in os.environ
            activate.assert_called_once()
            context.finals.append(final.call_args.kwargs["env"])
        if "--help" in args:
            return _HELP, ""
        candidate = Path(args[args.index("--settings") + 1])
        document = json.loads(candidate.read_text())
        context.documents.append(document)
        issue = (
            f"Invalid settings\n- {candidate} › hooks.PreToolUse.0: invalid\n"
            if document == {"hooks": {"PreToolUse": [None]}}
            else ""
        )
        return _HEADER + issue + _COMPLETION, ""

    monkeypatch.setattr(validation, "create_exec_launcher", Mock(side_effect=launcher))
    monkeypatch.setattr(validation, "_run_doctor", AsyncMock(side_effect=doctor))


def _check_result(context) -> None:
    assert len(context.launches) == len(context.payloads) == len(context.bootstrap) == 4
    for (_, policy), payload, bootstrap in zip(
        context.launches, context.payloads, context.bootstrap, strict=True
    ):
        assert payload["inherit"] is context.inherit
        assert payload["variables"] == context.variables
        assert payload["overrides"]["HOME"] == bootstrap["HOME"]
        assert payload["overrides"]["TMPDIR"] == bootstrap["TMPDIR"]
        assert payload["overrides"]["DISABLE_AUTOUPDATER"] == "1"
        assert "EXAMPLE_TARGET_ONLY" not in policy.spawn_env_allowlist
        assert "PYTHONPATH" not in policy.spawn_env_allowlist
        if context.sandboxed:
            assert not policy.allow_network and not policy.write_files
            assert len(policy.write_roots) == 1
            assert (
                policy.copy_on_write_namespace == context.original_policy.copy_on_write_namespace
            )
            assert (
                policy.credential_source_paths == context.original_policy.credential_source_paths
            )
        else:
            assert not policy.active and policy.backend_type == "none"
    assert context.instance.sandbox_policy == context.original_policy
    assert context.prefix == context.original_prefix
    assert context.instance.args == ["original-argument"]
    assert context.documents == [{}, {"hooks": {"PreToolUse": [None]}}, {"attribution": False}]
    assert all(not path.exists() for path in [*context.payload_paths, *context.launcher_paths])


@pytest.mark.parametrize("sandboxed", [False, True])
@pytest.mark.parametrize("form", ["inherit", "reset", "nested-unset", "nested-reset", "split"])
async def test_validator_keeps_wrapper_values_out_of_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sandboxed: bool,
    form: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = _context(tmp_path, sandboxed=sandboxed, form=form)
    _install_doubles(context, monkeypatch)
    await validation.validate_caller_settings(
        context.instance, {"attribution": False}, wrapper_prefix=context.prefix
    )
    _check_result(context)
    assert _SENTINEL not in caplog.text


@pytest.mark.parametrize(
    ("sandboxed", "form"), [(True, "inherit"), (True, "nested-reset"), (False, "reset")]
)
async def test_validator_payload_reaches_mocked_final_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandboxed: bool, form: str
) -> None:
    context = _context(tmp_path, sandboxed=sandboxed, form=form)
    _install_doubles(context, monkeypatch, handoff=True)
    await validation.validate_caller_settings(
        context.instance, {"attribution": False}, wrapper_prefix=context.prefix
    )
    _check_result(context)
    assert len(context.finals) == 4
    for final, bootstrap in zip(context.finals, context.bootstrap, strict=True):
        assert final["EXAMPLE_TARGET_ONLY"] == _SENTINEL
        assert final["PATH"] == str(context.executable.parent)
        assert final["HOME"] == bootstrap["HOME"]
        assert final["TMPDIR"] == bootstrap["TMPDIR"]
        assert final["DISABLE_AUTOUPDATER"] == "1"
        assert "REMOVE" not in final and "DISCARDED" not in final
        assert (final.get("KEEP") == "bootstrap") is context.inherit


@pytest.mark.parametrize(
    "mode", ["absolute", "relative-path", "empty-path", "unset-path", "relative-command"]
)
async def test_validator_selects_executable_using_selected_cwd_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    context = _context(tmp_path, sandboxed=True, form="reset")
    command = "selected-wrapper"
    selected_path = str(context.executable.parent)
    if mode == "relative-path":
        selected_path = "tools"
    elif mode == "empty-path":
        selected_path = ""
    elif mode == "relative-command":
        command = "./tools/selected-wrapper"
    assignments = [] if mode == "unset-path" else [f"PATH={selected_path}"]
    context.prefix = ["-i", "-C", "subdir", *assignments, command]
    context.cli_prefix = []
    context.original_prefix = list(context.prefix)
    expected_path = (
        str(context.cwd / "tools")
        if mode == "relative-path"
        else str(context.cwd)
        if mode == "empty-path"
        else os.defpath
        if mode == "unset-path"
        else selected_path
    )
    expected_command = (
        str(context.cwd / "tools" / "selected-wrapper") if mode == "relative-command" else command
    )
    lookup = Mock(return_value=str(context.executable))
    monkeypatch.setattr(validation.shutil, "which", lookup)
    _install_doubles(context, monkeypatch)
    await validation.validate_caller_settings(
        context.instance, {"attribution": False}, wrapper_prefix=context.prefix
    )
    lookup.assert_called_once_with(expected_command, path=expected_path)
    assert len(context.launches) == 4
    assert all(not path.exists() for path in context.payload_paths)


@pytest.mark.parametrize("fault", ["launcher", "doctor", "cancel"])
async def test_validator_cleans_environment_payload_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    context = _context(tmp_path, sandboxed=True, form="inherit")
    _install_doubles(context, monkeypatch, fault=fault)
    expected = asyncio.CancelledError if fault == "cancel" else validation.CallerSettingsError
    with pytest.raises(expected) as error:
        await validation.validate_caller_settings(
            context.instance, {"attribution": False}, wrapper_prefix=context.prefix
        )
    assert len(context.payload_paths) == 1
    assert all(not path.exists() for path in [*context.payload_paths, *context.launcher_paths])
    assert not list(tmp_path.glob("claude-settings-check-*"))
    assert _SENTINEL not in str(error.value)
