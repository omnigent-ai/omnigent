"""Validate invocation settings with the selected CLI's read-only startup check.

Requires the public ``--bare doctor`` protocol, verified with Claude Code
2.1.322 and 2.1.323. Capability controls fail closed for unsupported versions
or wrappers; the adapter does not infer support from a version number.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from omnigent.host.harness_startup import env_wrapper_launch
from omnigent.inner import _proc
from omnigent.inner._subprocess_lifecycle import (
    await_cleanup_task,
    close_subprocess_transport,
    terminate_subprocess,
)
from omnigent.inner.sandbox import (
    create_exec_launcher,
    set_temp_env,
    with_additional_read_roots,
    with_additional_write_roots,
    with_spawn_env_allowlist,
)
from omnigent.inner.terminal import terminal_pane_environment

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance

_VALIDATION_TIMEOUT = 15.0
_MAX_OUTPUT_BYTES = 512 * 1024
# Keep a group leader while descendants hold output pipes, without buffering
# output outside the bounded readers. Only the supervisor owns the status pipe.
_DOCTOR_SUPERVISOR = (
    "import os, signal, subprocess, sys\n"
    "signal.signal(signal.SIGTERM, lambda *_: None)\n"
    "status_fd = int(sys.argv[1])\n"
    "child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL)\n"
    "os.close(1)\n"
    "os.close(2)\n"
    "returncode = child.wait()\n"
    "os.write(status_fd, str(returncode).encode('ascii'))\n"
    "os.close(status_fd)\n"
    "sys.stdin.buffer.read()\n"
    "os._exit(0)\n"
)
_DOCTOR_COMPLETION = (
    "For a full setup checkup that can also fix issues, run /doctor in a Claude Code session."
)


class CallerSettingsErrorReason(Enum):
    """Caller settings failures with fixed, client-safe remediation."""

    INVALID_SETTINGS = "invalid_settings"
    VALIDATION_UNAVAILABLE = "validation_unavailable"
    SANDBOX_REQUIRED = "sandbox_required"
    INVALID_HOOKS = "invalid_hooks"
    INVALID_RETENTION = "invalid_retention"
    BRIDGE_HOOKS_DISABLED = "bridge_hooks_disabled"


_CALLER_SETTINGS_MESSAGES = {
    CallerSettingsErrorReason.INVALID_SETTINGS: (
        "Claude rejected the merged caller settings. Check --settings "
        "with the selected Claude CLI, then retry; the launch arguments "
        "and bridge settings were left unchanged."
    ),
    CallerSettingsErrorReason.VALIDATION_UNAVAILABLE: (
        "Cannot validate caller settings with the selected Claude CLI. "
        "The CLI and wrapper must support the read-only '--bare doctor' check; "
        "update them or check their startup and sandbox configuration, then retry."
    ),
    CallerSettingsErrorReason.SANDBOX_REQUIRED: (
        "Caller settings validation requires an active sandbox for this wrapper. "
        "Use the plain Claude CLI or a supported env wrapper, or configure a sandbox."
    ),
    CallerSettingsErrorReason.INVALID_HOOKS: (
        "Caller hooks must be an object whose event values are lists. Check --settings, "
        "then retry; the launch arguments and bridge settings were left unchanged."
    ),
    CallerSettingsErrorReason.INVALID_RETENTION: (
        "Caller cleanupPeriodDays must be a positive integer. Set it to at least 1 "
        "in --settings, then retry; the launch arguments and bridge settings "
        "were left unchanged."
    ),
    CallerSettingsErrorReason.BRIDGE_HOOKS_DISABLED: (
        "Caller disableAllHooks=true or allowManagedHooksOnly=true disables required "
        "Omnigent bridge hooks. Keep these hooks enabled in --settings, then retry; "
        "the launch arguments and bridge settings were left unchanged."
    ),
}


class CallerSettingsError(ValueError):
    """A settings failure whose client message never uses exception text."""

    def __init__(self, reason: CallerSettingsErrorReason) -> None:
        if not isinstance(reason, CallerSettingsErrorReason):
            raise TypeError("reason must be a CallerSettingsErrorReason")
        self.reason = reason
        super().__init__(self.client_message)

    @property
    def client_message(self) -> str:
        return _CALLER_SETTINGS_MESSAGES[self.reason]


class _ValidationUnavailable(Exception):
    """The selected command did not prove its startup-validation capability."""


class _InvalidSettings(Exception):
    """The startup parser reported an issue in the merged candidate."""


async def _read_output(stream: asyncio.StreamReader) -> str:
    try:
        await stream.readexactly(_MAX_OUTPUT_BYTES + 1)
    except asyncio.IncompleteReadError as exc:
        return exc.partial.decode("utf-8")
    raise _ValidationUnavailable


async def _run_doctor(
    command: str, args: Sequence[str], *, cwd: Path, env: dict[str, str]
) -> tuple[str, str]:
    """Capture bounded output without logging vendor diagnostics or supplied values."""
    with contextlib.ExitStack() as resources:
        status_reader: asyncio.StreamReader | None = None
        status_writer = None
        status_fds: tuple[int, ...] = ()
        launch = (command, *args)
        if _proc.IS_POSIX:
            status_read_fd, status_write_fd = os.pipe()
            status_file = resources.enter_context(os.fdopen(status_read_fd, "rb", buffering=0))
            status_writer = resources.enter_context(os.fdopen(status_write_fd, "wb", buffering=0))
            status_reader = asyncio.StreamReader()
            status_protocol = asyncio.StreamReaderProtocol(status_reader)
            status_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: status_protocol, status_file
            )
            resources.callback(status_transport.close)
            launch = (
                sys.executable,
                "-I",
                "-c",
                _DOCTOR_SUPERVISOR,
                str(status_writer.fileno()),
                command,
                *args,
            )
            status_fds = (status_writer.fileno(),)
        process = await asyncio.create_subprocess_exec(
            *launch,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE if _proc.IS_POSIX else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=status_fds,
            **_proc.spawn_kwargs(),
        )
        if status_writer is not None:
            status_writer.close()
        _proc.remember_process_group(process)
        assert process.stdout is not None and process.stderr is not None
        readers = [
            asyncio.create_task(_read_output(process.stdout)),
            asyncio.create_task(_read_output(process.stderr)),
        ]
        if status_reader is not None:
            readers.append(asyncio.create_task(_read_output(status_reader)))
        try:
            output = await asyncio.gather(*readers)
            status = output[2] if status_reader is not None else str(await process.wait())
            if status != "0":
                raise _ValidationUnavailable
            return output[0], output[1]
        finally:

            async def cleanup() -> None:
                try:
                    for reader in readers:
                        reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                    await terminate_subprocess(
                        process,
                        terminate_timeout=0.1,
                        kill_timeout=1.0,
                        label="Claude settings validator",
                    )
                finally:
                    if process.stdin is not None:
                        process.stdin.close()
                    close_subprocess_transport(process)

            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await await_cleanup_task(cleanup_task)
                raise


def _diagnostics(stdout: str, stderr: str, candidate: Path) -> list[str]:
    if stdout.rstrip().splitlines()[-1:] != [_DOCTOR_COMPLETION]:
        raise _ValidationUnavailable
    prefix = f"- {candidate} › "
    references = [line for line in (stdout + "\n" + stderr).splitlines() if str(candidate) in line]
    if any(not line.startswith(prefix) for line in references):
        raise _ValidationUnavailable
    if ("Invalid settings" in stdout.splitlines()) != bool(references):
        raise _ValidationUnavailable
    return references


def _doctor_identity(stdout: str) -> tuple[str, ...]:
    identity = []
    for prefix in ("Running: ", "Commit: ", "Path: "):
        values = [line for line in stdout.splitlines() if line.startswith(prefix)]
        if len(values) != 1:
            raise _ValidationUnavailable
        identity.append(values[0])
    return tuple(identity)


async def validate_caller_settings(
    instance: TerminalInstance,
    settings: Mapping[str, object],
    *,
    wrapper_prefix: Sequence[str],
) -> None:
    """Check the entire document without loading caller hooks or changing the launch.

    Active terminals retain their filesystem masks and copy-on-write namespace;
    the validation clone allows writes only to owned scratch and denies network.
    Unsandboxed launches support the plain CLI and parsed ``env`` layers only.
    """
    if instance.os_env is None:
        raise CallerSettingsError(CallerSettingsErrorReason.VALIDATION_UNAVAILABLE)
    env: dict[str, str] = terminal_pane_environment(
        inherit_env=instance.inherit_env, env=instance.env, env_unset=instance.env_unset
    )
    sandbox = instance.sandbox_policy
    if sandbox is not None and sandbox.active and sandbox.spawn_env_allowlist is not None:
        env = {name: value for name, value in env.items() if name in sandbox.spawn_env_allowlist}
    command, prefix, cwd = instance.command, list(wrapper_prefix), instance.os_env.cwd
    if sandbox is not None and sandbox.active and Path(command).name != "env":
        command = shutil.which(command) or command
    if Path(command).name == "env":
        wrapper = env_wrapper_launch(command, prefix, env)
        if wrapper is None:
            raise CallerSettingsError(CallerSettingsErrorReason.VALIDATION_UNAVAILABLE)
        command, prefix = wrapper.command, wrapper.args
        env = dict(env) if wrapper.environment.inherit else {}
        for name in wrapper.environment.unset:
            env.pop(name, None)
        env.update(wrapper.environment.variables)
        if wrapper.chdir is not None:
            cwd = cwd / wrapper.chdir
    if not (sandbox is not None and sandbox.active) and (
        Path(command).name not in {"claude", "claude.exe"} or prefix
    ):
        raise CallerSettingsError(CallerSettingsErrorReason.SANDBOX_REQUIRED)
    try:
        async with asyncio.timeout(_VALIDATION_TIMEOUT):
            with tempfile.TemporaryDirectory(
                prefix="claude-settings-check-", dir=instance.private_dir.parent
            ) as temporary:
                scratch = Path(temporary).resolve()
                candidate = scratch / "settings.json"
                candidate.touch(mode=0o600)
                launcher: Path | None = None
                try:
                    if sandbox is not None and sandbox.active:
                        private_dir = instance.private_dir.resolve()
                        workspace = instance.os_env.cwd.resolve()
                        private_workspace = workspace.is_relative_to(private_dir)
                        policy = with_additional_read_roots(
                            sandbox,
                            [
                                workspace,
                                *(path for path in sandbox.write_roots if path != private_dir),
                                *sandbox.write_files,
                            ],
                        )
                        if private_workspace:
                            # A fork lives below the private parent; mount only its workspace.
                            roots = [*(policy.read_roots or []), cwd.resolve()]
                            executable = shutil.which(command, path=env.get("PATH", os.defpath))
                            if executable is not None:
                                for path in (Path(executable), Path(executable).resolve()):
                                    roots.extend(
                                        parent
                                        for parent in (path.parent, path.parent.parent)
                                        if str(parent) != parent.anchor
                                    )
                            if any(private_dir.is_relative_to(path) for path in roots) or any(
                                path.is_relative_to(private_dir)
                                and not path.is_relative_to(workspace)
                                for path in policy.read_roots or []
                            ):
                                raise _ValidationUnavailable
                            masks = policy.mask_paths
                            sockets = policy.deny_unix_socket_paths
                        else:
                            masks = [
                                *(
                                    path
                                    for path in policy.mask_paths or []
                                    if not path.is_relative_to(private_dir)
                                ),
                                private_dir,
                            ]
                            sockets = [
                                path
                                for path in policy.deny_unix_socket_paths or []
                                if not path.is_relative_to(private_dir)
                            ]
                        policy = with_additional_write_roots(
                            replace(
                                policy,
                                write_roots=[],
                                write_files=[],
                                allow_network=False,
                                egress_relay_port=None,
                                egress_socket_path=None,
                                git_ssh=None,
                                git_ssh_socket_path=None,
                                tcp_egress=None,
                                tcp_egress_socket_path=None,
                                credential_proxy=None,
                                mask_paths=masks,
                                deny_unix_socket_paths=sockets,
                                mask_scan_skip_roots=[
                                    path
                                    for path in policy.mask_scan_skip_roots or []
                                    if not private_workspace or path != private_dir
                                ],
                            ),
                            [scratch],
                        )
                    else:
                        policy = None
                    identity: tuple[str, ...] | None = None
                    for index, document in enumerate(
                        (None, {}, {"hooks": {"PreToolUse": [None]}}, settings)
                    ):
                        run_dir = scratch / str(index)
                        run_dir.mkdir(mode=0o700)
                        run_env: dict[str, str] = {
                            **env,
                            "HOME": str(run_dir / "home"),
                            "USERPROFILE": str(run_dir / "home"),
                            "XDG_CONFIG_HOME": str(run_dir / "config"),
                            "XDG_CACHE_HOME": str(run_dir / "cache"),
                            "XDG_DATA_HOME": str(run_dir / "data"),
                            "CLAUDE_CONFIG_DIR": str(run_dir / "claude"),
                            "DISABLE_AUTOUPDATER": "1",
                            "DISABLE_TELEMETRY": "1",
                            "DISABLE_ERROR_REPORTING": "1",
                            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                            "NO_COLOR": "1",
                            "FORCE_COLOR": "0",
                        }
                        set_temp_env(run_env, run_dir)
                        launch_command = command
                        host_cwd = cwd
                        if policy is not None:
                            launcher = Path(
                                create_exec_launcher(
                                    command,
                                    with_spawn_env_allowlist(policy, list(run_env)),
                                    cwd=str(cwd),
                                )
                            )
                            launch_command = str(launcher)
                            if policy.copy_on_write_namespace:
                                host_cwd = Path("/")
                        args = [*prefix, "--bare", "--setting-sources", ""]
                        if document is None:
                            args.extend(["doctor", "--help"])
                        else:
                            candidate.write_text(
                                json.dumps(document, allow_nan=False), encoding="utf-8"
                            )
                            args.extend(["--settings", str(candidate), "doctor"])
                        stdout, stderr = await _run_doctor(
                            launch_command, args, cwd=host_cwd, env=run_env
                        )
                        if launcher is not None:
                            launcher.unlink(missing_ok=True)
                            launcher = None
                        if document is None:
                            help_text = " ".join(stdout.split())
                            if not all(
                                text in help_text
                                for text in (
                                    "Usage: claude doctor",
                                    "Reads settings files",
                                    "fix issues, run /doctor in a session.",
                                )
                            ):
                                raise _ValidationUnavailable
                            continue
                        diagnostics = _diagnostics(stdout, stderr, candidate)
                        observed_identity = _doctor_identity(stdout)
                        if identity is None:
                            identity = observed_identity
                        elif observed_identity != identity:
                            raise _ValidationUnavailable
                        if index == 1 and diagnostics:
                            raise _ValidationUnavailable
                        if index == 2 and not any(
                            line.startswith(f"- {candidate} › hooks.PreToolUse.0:")
                            for line in diagnostics
                        ):
                            raise _ValidationUnavailable
                        if index == 3 and diagnostics:
                            raise _InvalidSettings
                finally:
                    if launcher is not None:
                        launcher.unlink(missing_ok=True)
    except asyncio.CancelledError:
        raise
    except _InvalidSettings:
        raise CallerSettingsError(CallerSettingsErrorReason.INVALID_SETTINGS) from None
    except Exception:  # noqa: BLE001 - vendor errors must not expose caller settings
        raise CallerSettingsError(CallerSettingsErrorReason.VALIDATION_UNAVAILABLE) from None
