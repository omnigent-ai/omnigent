"""Host-local configuration for Codex exec-server environments."""

from __future__ import annotations

import json
import math
import os
import shlex
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TypeAlias
from urllib.parse import urlsplit

CODEX_LOCAL_ENVIRONMENT_ID = "local"
CODEX_REMOTE_ENVIRONMENT_ID = "remote"
_CODEX_ENVIRONMENTS_FILE = "environments.toml"
_REMOTE_EXECUTION_FILE = "remote_execution.json"
_REMOTE_EXECUTION_MAX_BYTES = 16 * 1024
DEFAULT_MANAGED_EXEC_SERVER_READY_TIMEOUT_SECONDS = 600.0


class CodexRemoteExecutionConfigError(ValueError):
    """A persisted Codex remote-execution binding is unsafe or malformed."""


@dataclass(frozen=True)
class ManagedExecServerConfig:
    """Command and readiness budget for an Omnigent-owned exec server."""

    command: tuple[str, ...]
    ready_timeout_seconds: float = DEFAULT_MANAGED_EXEC_SERVER_READY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Reject commands that cannot be launched safely."""
        if not isinstance(self.command, tuple) or not self.command:
            raise CodexRemoteExecutionConfigError("managed_command must be a non-empty argv")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in self.command):
            raise CodexRemoteExecutionConfigError(
                "managed_command arguments must be strings without NUL bytes"
            )
        if not self.command[0]:
            raise CodexRemoteExecutionConfigError("managed_command executable must be non-empty")
        timeout = self.ready_timeout_seconds
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise CodexRemoteExecutionConfigError(
                "ready_timeout_seconds must be greater than zero"
            )


@dataclass(frozen=True)
class CodexRemoteExecution:
    """One complete Codex exec-server binding and its remote working directory."""

    exec_server_url: str
    cwd: str
    managed_exec_server: ManagedExecServerConfig | None = None

    def __post_init__(self) -> None:
        """Reject bindings that Codex cannot select safely."""
        try:
            parsed = urlsplit(self.exec_server_url)
            _ = parsed.port
        except ValueError as exc:
            raise CodexRemoteExecutionConfigError(
                "exec_server_url must be a valid ws:// or wss:// URL"
            ) from exc
        if (
            self.exec_server_url != self.exec_server_url.strip()
            or parsed.scheme not in {"ws", "wss"}
            or not parsed.hostname
            or parsed.fragment
        ):
            raise CodexRemoteExecutionConfigError(
                "exec_server_url must be a valid ws:// or wss:// URL"
            )
        if not self.cwd or "\x00" in self.cwd or not _absolute_remote_path(self.cwd):
            raise CodexRemoteExecutionConfigError("cwd must be a non-empty absolute path")


def codex_remote_environment_requests(
    binding: CodexRemoteExecution,
    *,
    local_cwd: str,
    local_workspace_roots: list[str],
) -> list[dict[str, object]]:
    """Select remote code execution first while retaining explicit laptop tools."""
    return [
        {
            "environmentId": CODEX_REMOTE_ENVIRONMENT_ID,
            "cwd": binding.cwd,
            "runtimeWorkspaceRoots": [binding.cwd],
        },
        {
            "environmentId": CODEX_LOCAL_ENVIRONMENT_ID,
            "cwd": local_cwd,
            "runtimeWorkspaceRoots": local_workspace_roots,
        },
    ]


def configure_codex_execution_environments(
    codex_home: Path,
    binding: CodexRemoteExecution | None,
) -> None:
    """Materialize Codex's private remote-plus-local environment registry."""
    path = codex_home / _CODEX_ENVIRONMENTS_FILE
    if binding is None:
        path.unlink(missing_ok=True)
        return

    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = (
        f"default = {json.dumps(CODEX_REMOTE_ENVIRONMENT_ID)}\n"
        "include_local = true\n\n"
        "[[environments]]\n"
        f"id = {json.dumps(CODEX_REMOTE_ENVIRONMENT_ID)}\n"
        f"url = {json.dumps(binding.exec_server_url)}\n"
    )
    fd, temporary = tempfile.mkstemp(prefix=f"{_CODEX_ENVIRONMENTS_FILE}.", dir=codex_home)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CodexExecutionMode(Enum):
    """Non-remote choices for a Codex execution binding update."""

    PRESERVE = "preserve"
    LOCAL = "local"


CodexExecutionSelection: TypeAlias = CodexRemoteExecution | CodexExecutionMode


def _absolute_remote_path(value: str) -> bool:
    """Accept absolute POSIX or Windows paths without interpreting them locally."""
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def resolve_execution_selection(
    *,
    exec_server_url: str | None,
    exec_cwd: str | None,
    exec_server_command: str | None,
    exec_server_ready_timeout: float | None,
    local_execution: bool,
) -> CodexExecutionSelection:
    """Convert CLI flags into one complete execution selection."""
    if local_execution and (
        exec_server_url is not None
        or exec_cwd is not None
        or exec_server_command is not None
        or exec_server_ready_timeout is not None
    ):
        raise ValueError("--local-execution is mutually exclusive with remote execution options")
    if exec_server_url is None and exec_cwd is not None:
        raise ValueError("--exec-server-url is required when --exec-cwd is set")
    if exec_server_url is not None and exec_cwd is None:
        raise ValueError("--exec-cwd is required when --exec-server-url is set")
    if exec_server_command is not None and (exec_server_url is None or exec_cwd is None):
        raise ValueError("--exec-server-command requires --exec-server-url and --exec-cwd")
    if exec_server_ready_timeout is not None and exec_server_command is None:
        raise ValueError("--exec-server-ready-timeout requires --exec-server-command")
    managed_exec_server: ManagedExecServerConfig | None = None
    if exec_server_command is not None:
        try:
            command = tuple(shlex.split(exec_server_command))
        except ValueError as exc:
            raise ValueError(f"--exec-server-command is invalid: {exc}") from exc
        if not command:
            raise ValueError("--exec-server-command must be non-empty")
        managed_exec_server = ManagedExecServerConfig(
            command=command,
            ready_timeout_seconds=(
                exec_server_ready_timeout
                if exec_server_ready_timeout is not None
                else DEFAULT_MANAGED_EXEC_SERVER_READY_TIMEOUT_SECONDS
            ),
        )
    if exec_server_url is not None and exec_cwd is not None:
        return CodexRemoteExecution(
            exec_server_url=exec_server_url,
            cwd=exec_cwd,
            managed_exec_server=managed_exec_server,
        )
    return CodexExecutionMode.LOCAL if local_execution else CodexExecutionMode.PRESERVE


def read_remote_execution(bridge_dir: Path) -> CodexRemoteExecution | None:
    """Read one remote binding; malformed presence fails closed."""
    path = bridge_dir / _REMOTE_EXECUTION_FILE
    try:
        with path.open("rb") as handle:
            payload = handle.read(_REMOTE_EXECUTION_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CodexRemoteExecutionConfigError(
            f"remote execution configuration is unreadable: {exc}"
        ) from exc
    if len(payload) > _REMOTE_EXECUTION_MAX_BYTES:
        raise CodexRemoteExecutionConfigError("remote execution configuration is too large")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CodexRemoteExecutionConfigError(
            "remote execution configuration is malformed"
        ) from exc
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise CodexRemoteExecutionConfigError("remote execution configuration is malformed")
    url = raw.get("exec_server_url")
    cwd = raw.get("cwd")
    if not isinstance(url, str) or not isinstance(cwd, str):
        raise CodexRemoteExecutionConfigError("remote execution configuration is malformed")
    raw_command = raw.get("managed_command")
    if raw_command is not None and (
        not isinstance(raw_command, list) or not all(isinstance(arg, str) for arg in raw_command)
    ):
        raise CodexRemoteExecutionConfigError("remote execution configuration is malformed")
    raw_timeout = raw.get("ready_timeout_seconds")
    if raw_command is None and raw_timeout is not None:
        raise CodexRemoteExecutionConfigError("remote execution configuration is malformed")
    if raw_timeout is not None and (
        isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float))
    ):
        raise CodexRemoteExecutionConfigError("remote execution configuration is malformed")
    managed_exec_server = None
    if raw_command is not None:
        managed_exec_server = ManagedExecServerConfig(
            command=tuple(raw_command),
            ready_timeout_seconds=(
                raw_timeout
                if raw_timeout is not None
                else DEFAULT_MANAGED_EXEC_SERVER_READY_TIMEOUT_SECONDS
            ),
        )
    return CodexRemoteExecution(
        exec_server_url=url,
        cwd=cwd,
        managed_exec_server=managed_exec_server,
    )


def write_remote_execution(bridge_dir: Path, binding: CodexRemoteExecution) -> None:
    """Atomically persist one private remote binding."""
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = bridge_dir / _REMOTE_EXECUTION_FILE
    fd, temporary = tempfile.mkstemp(prefix=f"{_REMOTE_EXECUTION_FILE}.", dir=bridge_dir)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            payload: dict[str, object] = {
                "schema": 1,
                "exec_server_url": binding.exec_server_url,
                "cwd": binding.cwd,
            }
            managed = binding.managed_exec_server
            if managed is not None:
                payload["managed_command"] = list(managed.command)
                payload["ready_timeout_seconds"] = managed.ready_timeout_seconds
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def clear_remote_execution(bridge_dir: Path) -> None:
    """Return a session to local execution."""
    try:
        (bridge_dir / _REMOTE_EXECUTION_FILE).unlink()
    except FileNotFoundError:
        return


def project_remote_execution(
    current: CodexRemoteExecution | None,
    selection: CodexExecutionSelection,
) -> CodexRemoteExecution | None:
    """Project the persisted binding after applying one CLI selection."""
    if selection is CodexExecutionMode.PRESERVE:
        return current
    if selection is CodexExecutionMode.LOCAL:
        return None
    return selection


def apply_execution_selection(
    bridge_dir: Path,
    selection: CodexExecutionSelection,
) -> None:
    """Apply a validated selection without changing preserved state."""
    if selection is CodexExecutionMode.PRESERVE:
        read_remote_execution(bridge_dir)
        return
    if selection is CodexExecutionMode.LOCAL:
        clear_remote_execution(bridge_dir)
        return
    write_remote_execution(bridge_dir, selection)
