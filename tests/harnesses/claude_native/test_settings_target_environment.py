"""Settings environment handoff using inert executable files and mocked processes."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.harnesses.claude_native import settings_validation as validation
from tests.harnesses.claude_native._settings_validation_helpers import (
    DOCTOR_COMPLETION as _COMPLETION,
)
from tests.harnesses.claude_native._settings_validation_helpers import (
    DOCTOR_HEADER as _HEADER,
)
from tests.harnesses.claude_native._settings_validation_helpers import (
    DOCTOR_HELP as _HELP,
)
from tests.harnesses.claude_native._settings_validation_helpers import validator_policy

_SENTINEL = "inert-target-value with whitespace"


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
        env={
            "PATH": "/bootstrap/bin",
            "KEEP": "bootstrap",
            "REMOVE": "discard",
            "EXCLUDED": "bootstrap-only",
        },
        sandbox_policy=None,
    )
    if sandboxed:
        instance.sandbox_policy = validator_policy(
            workspace,
            private,
            instance.socket_path,
            read_roots=[],
            spawn_env_allowlist=["PATH", "KEEP", "REMOVE"],
        )
    variables = {
        "PATH": str(tools),
        "EXAMPLE_TARGET_ONLY": _SENTINEL,
        "HOME": "/caller-home",
        "CLAUDE_CONFIG_DIR": "/caller-config",
        "PRIVATE_KEY": "wrapper-value",
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
        command=command,
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
        documents=[],
        original_policy=copy.deepcopy(instance.sandbox_policy),
    )


def _install_doubles(context, monkeypatch: pytest.MonkeyPatch):
    observations = SimpleNamespace(launches=[], doctors=[])

    def launcher(command, policy, *, cwd, target_environment_path=None):
        payload = Path(target_environment_path) if target_environment_path is not None else None
        observations.launches.append(
            SimpleNamespace(
                command=command,
                cwd=cwd,
                payload=payload,
                mode=payload.stat().st_mode & 0o777 if payload is not None else None,
                parent_mode=payload.parent.stat().st_mode & 0o777 if payload is not None else None,
                writable=payload is not None
                and (
                    any(payload.is_relative_to(root) for root in policy.write_roots)
                    or not policy.active
                ),
            )
        )
        if payload is not None:
            context.payload_paths.append(payload)
            context.payloads.append(json.loads(payload.read_text()))
        path = (
            payload.parent if payload is not None else context.instance.private_dir
        ) / "inert-launcher"
        path.touch(mode=0o700)
        context.launcher_paths.append(path)
        context.launches.append((command, policy))
        return str(path)

    async def doctor(command, args, *, cwd, env):
        observations.doctors.append(
            SimpleNamespace(command=command, args=list(args), cwd=cwd, env=dict(env))
        )
        context.bootstrap.append(dict(env))
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

    observations.launcher = Mock(side_effect=launcher)
    observations.doctor = AsyncMock(side_effect=doctor)
    monkeypatch.setattr(validation, "create_exec_launcher", observations.launcher)
    monkeypatch.setattr(validation, "_run_doctor", observations.doctor)
    return observations


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
    observations = _install_doubles(context, monkeypatch)
    await validation.validate_caller_settings(
        context.instance, {"attribution": False}, wrapper_prefix=context.prefix
    )
    assert len(context.launches) == len(context.payloads) == len(context.bootstrap) == 4
    assert len(observations.launches) == len(observations.doctors) == 4
    shared = bool(context.original_policy and context.original_policy.copy_on_write_namespace)
    expected_command = (
        str(Path(os.defpath.split(os.pathsep)[0]) / "env") if shared else str(context.executable)
    )
    expected_prefix = (
        ["--", context.command, *context.cli_prefix] if shared else context.cli_prefix
    )
    for index, ((command, policy), payload, bootstrap, launch, doctor) in enumerate(
        zip(
            context.launches,
            context.payloads,
            context.bootstrap,
            observations.launches,
            observations.doctors,
            strict=True,
        )
    ):
        assert command == launch.command == expected_command
        assert launch.cwd == str(context.cwd)
        assert launch.payload is not None and launch.mode == 0o600 and launch.parent_mode == 0o700
        assert launch.writable
        assert doctor.command == str(context.launcher_paths[index])
        assert doctor.cwd == (Path("/") if shared else context.cwd)
        assert doctor.args[: len(expected_prefix)] == expected_prefix
        assert bootstrap["PATH"] == "/bootstrap/bin"
        assert not {
            "EXAMPLE_TARGET_ONLY",
            "PRIVATE_KEY",
            "OMNIGENT_LAUNCHER_SPAWN_WRAPPED",
            "PYTHONPATH",
            "LD_PRELOAD",
        }.intersection(bootstrap)
        assert ("EXCLUDED" not in bootstrap) is context.sandboxed
        assert _SENTINEL not in json.dumps([doctor.command, doctor.args, bootstrap])
        assert payload["inherit"] is context.inherit
        assert payload["variables"] == context.variables
        for name in ("HOME", "TMPDIR", "CLAUDE_CONFIG_DIR"):
            assert payload["overrides"][name] == bootstrap[name]
        assert bootstrap["CLAUDE_CONFIG_DIR"] != "/caller-config"
        assert payload["overrides"]["DISABLE_AUTOUPDATER"] == "1"
        assert not {"EXAMPLE_TARGET_ONLY", "PRIVATE_KEY", "PYTHONPATH"}.intersection(
            policy.spawn_env_allowlist
        )
        if context.sandboxed:
            assert not policy.allow_network and not policy.write_files
            assert len(policy.write_roots) == 1
            assert policy.mask_paths == [
                context.instance.os_env.cwd / "secret",
                context.instance.private_dir,
            ]
            assert policy.deny_unix_socket_paths == [context.instance.os_env.cwd / "other.sock"]
            assert (
                policy.copy_on_write_namespace == context.original_policy.copy_on_write_namespace
            )
            assert policy.copy_on_write_roots == context.original_policy.copy_on_write_roots
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
    assert _SENTINEL not in caplog.text


@pytest.mark.parametrize(
    "mode", ["absolute", "relative-path", "empty-path", "unset-path", "relative-command"]
)
@pytest.mark.parametrize("shared", [False, True], ids=["host-filesystem", "shared-filesystem"])
async def test_validator_selects_executable_using_selected_cwd_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, shared: bool
) -> None:
    context = _context(tmp_path, sandboxed=True, form="reset")
    if not shared:
        context.instance.sandbox_policy.copy_on_write_namespace = None
        context.original_policy = copy.deepcopy(context.instance.sandbox_policy)
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
    context.command = command
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
    lookup = Mock(
        return_value=(
            str(Path(os.defpath.split(os.pathsep)[0]) / "env")
            if shared
            else str(context.executable)
        )
    )
    if shared:
        context.executable.unlink()
    monkeypatch.setattr(validation.shutil, "which", lookup)
    observations = _install_doubles(context, monkeypatch)
    await validation.validate_caller_settings(
        context.instance, {"attribution": False}, wrapper_prefix=context.prefix
    )
    if shared:
        lookup.assert_called_once_with("env", path=os.defpath)
    else:
        lookup.assert_called_once_with(expected_command, path=expected_path)
    assert len(context.launches) == len(observations.doctors) == 4
    for index, (launch, doctor) in enumerate(
        zip(observations.launches, observations.doctors, strict=True)
    ):
        assert launch.command == (
            str(Path(os.defpath.split(os.pathsep)[0]) / "env")
            if shared
            else str(context.executable)
        )
        assert launch.cwd == str(context.cwd)
        assert launch.payload is not None and launch.mode == 0o600 and launch.parent_mode == 0o700
        assert launch.writable
        assert doctor.command == str(context.launcher_paths[index])
        assert doctor.cwd == (Path("/") if shared else context.cwd)
        assert doctor.args[: len(["--", command] if shared else [])] == (
            ["--", command] if shared else []
        )
        assert doctor.env["PATH"] == "/bootstrap/bin"
        assert not {
            "EXAMPLE_TARGET_ONLY",
            "PRIVATE_KEY",
            "OMNIGENT_LAUNCHER_SPAWN_WRAPPED",
            "PYTHONPATH",
            "LD_PRELOAD",
            "EXCLUDED",
        }.intersection(doctor.env)
        assert _SENTINEL not in json.dumps([doctor.command, doctor.args, doctor.env])
    assert all(not path.exists() for path in context.payload_paths)


@pytest.mark.parametrize("fault", ["launcher", "doctor", "cancel"])
async def test_validator_cleans_environment_payload_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    context = _context(tmp_path, sandboxed=True, form="inherit")
    observations = _install_doubles(context, monkeypatch)
    if fault == "launcher":
        normal_launcher = observations.launcher.side_effect

        def fail_launcher(*args, **kwargs):
            normal_launcher(*args, **kwargs)
            raise RuntimeError("inert launcher failure")

        observations.launcher.side_effect = fail_launcher
    else:
        observations.doctor.side_effect = (
            RuntimeError("inert doctor failure") if fault == "doctor" else asyncio.CancelledError
        )
    expected = asyncio.CancelledError if fault == "cancel" else validation.CallerSettingsError
    with pytest.raises(expected) as error:
        await validation.validate_caller_settings(
            context.instance, {"attribution": False}, wrapper_prefix=context.prefix
        )
    assert len(context.payload_paths) == 1
    (launch,) = observations.launches
    assert launch.command == str(Path(os.defpath.split(os.pathsep)[0]) / "env")
    assert launch.cwd == str(context.cwd)
    assert launch.payload is not None and launch.mode == 0o600 and launch.parent_mode == 0o700
    assert launch.writable
    if fault == "launcher":
        observations.doctor.assert_not_awaited()
    else:
        observations.doctor.assert_awaited_once()
        call = observations.doctor.call_args
        assert call.args[0] == str(context.launcher_paths[0])
        assert call.kwargs["cwd"] == Path("/")
        assert call.kwargs["env"]["PATH"] == "/bootstrap/bin"
        assert "EXAMPLE_TARGET_ONLY" not in call.kwargs["env"]
    assert all(not path.exists() for path in [*context.payload_paths, *context.launcher_paths])
    assert not list(tmp_path.glob("claude-settings-check-*"))
    assert _SENTINEL not in str(error.value)
