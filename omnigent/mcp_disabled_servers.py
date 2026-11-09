"""Host-owned MCP disable state, applied only to explicitly active launches."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, overload

from omnigent.config import global_config_path
from omnigent.inner._proc import pipe_supervised_command, remember_process_group, spawn_kwargs
from omnigent.inner._subprocess_lifecycle import close_subprocess_transport, terminate_subprocess

HarnessKey = Literal["claude", "codex"]
ExecutorId = Literal["claude-native", "claude-sdk", "codex-native", "codex"]
HARNESS_EXECUTORS: Final[Mapping[HarnessKey, tuple[ExecutorId, ...]]] = {
    "claude": ("claude-native", "claude-sdk"),
    "codex": ("codex-native", "codex"),
}
FILENAME: Final = "mcp-disabled-servers.json"
RELAUNCH_WITHOUT_OVERRIDES_EVENT: Final = "mcp_disabled_relaunch_without_overrides"
_MAX_BYTES = 64 * 1024
_MAX_NAME_LENGTH = 1024
_TOML_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_write_lock = threading.Lock()
_read_failure_lock = threading.Lock()
_read_failures: set[tuple[Path, str]] = set()
_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServerKey:
    harness: HarnessKey
    server: str
    plugin: str | None = None


class DisabledServerWriteError(ValueError):
    """A rejected state update, with a stable host-facing error code."""

    code: Literal["unsupported_harness", "reserved_name", "invalid_name", "io"]

    def __init__(
        self, code: Literal["unsupported_harness", "reserved_name", "invalid_name", "io"]
    ):
        self.code = code
        super().__init__(f"Cannot update disabled MCP servers: {code}")


def _validate(key: ServerKey) -> None:
    if not isinstance(key.harness, str) or key.harness not in HARNESS_EXECUTORS:
        raise DisabledServerWriteError("unsupported_harness")
    if key.server == "omnigent":
        raise DisabledServerWriteError("reserved_name")
    for name in (key.server, *((key.plugin,) if key.plugin is not None else ())):
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > _MAX_NAME_LENGTH
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise DisabledServerWriteError("invalid_name")
    if key.harness != "claude" and key.plugin is not None:
        raise DisabledServerWriteError("invalid_name")


def _log_read_failure(path: Path, reason: str) -> None:
    with _read_failure_lock:
        if (path, reason) in _read_failures:
            return
        _read_failures.add((path, reason))
    _logger.warning("mcp_disabled_servers_read_failed path=%s reason=%s", path, reason)


def _read(path: Path, *, for_write: bool = False) -> list[ServerKey]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("oversized")
        document = json.loads(raw)
        if (
            not isinstance(document, dict)
            or type(document.get("version")) is not int
            or document.get("version") != 1
            or not isinstance(document.get("disabled"), list)
        ):
            raise ValueError("invalid format")
        result: list[ServerKey] = []
        for entry in document["disabled"]:
            if not isinstance(entry, dict):
                continue
            try:
                harness, server, plugin = (
                    entry.get("harness"),
                    entry.get("server"),
                    entry.get("plugin"),
                )
                if harness not in HARNESS_EXECUTORS or not isinstance(server, str):
                    continue
                if plugin is not None and not isinstance(plugin, str):
                    continue
                key = ServerKey(harness, server, plugin)
                _validate(key)
            except (DisabledServerWriteError, TypeError):
                continue
            if key not in result:
                result.append(key)
        return result
    except (OSError, ValueError, RecursionError) as error:
        _log_read_failure(path, type(error).__name__)
        if for_write and not isinstance(error, FileNotFoundError):
            raise DisabledServerWriteError("io") from error
        return []


def set_disabled(key: ServerKey, disabled: bool) -> None:
    """Atomically update exactly one key, preserving unknown-to-inventory entries."""
    _validate(key)
    if os.name != "posix":
        raise DisabledServerWriteError("io")
    import fcntl

    temporary: str | None = None
    try:
        path = global_config_path().parent / FILENAME
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                entries = _read(path, for_write=True)
                if disabled and key not in entries:
                    entries.append(key)
                elif not disabled:
                    entries = [entry for entry in entries if entry != key]
                document = {
                    "version": 1,
                    "disabled": [
                        {
                            "harness": entry.harness,
                            "server": entry.server,
                            **({"plugin": entry.plugin} if entry.plugin is not None else {}),
                        }
                        for entry in entries
                    ],
                }
                payload = (json.dumps(document, indent=2) + "\n").encode()
                if len(payload) > _MAX_BYTES:
                    raise DisabledServerWriteError("io")
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=f".{FILENAME}.", delete=False
                ) as stream:
                    temporary = stream.name
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                temporary = None
    except OSError as error:
        raise DisabledServerWriteError("io") from error
    finally:
        if temporary is not None:
            with suppress(OSError):
                Path(temporary).unlink(missing_ok=True)


def is_disabled(key: ServerKey) -> bool:
    """Return normalized saved state; unreadable storage behaves as empty."""
    return key in _read(global_config_path().parent / FILENAME)


def _active_entries(
    harness: HarnessKey, active_harnesses: Collection[str] | None
) -> list[ServerKey]:
    if not active_harnesses or harness not in active_harnesses:
        return []
    return [key for key in _read(global_config_path().parent / FILENAME) if key.harness == harness]


def _claude_name(key: ServerKey) -> str:
    return f"plugin:{key.plugin}:{key.server}" if key.plugin else key.server


@overload
def apply_claude_settings(
    settings: dict[str, object],
    *,
    executor: Literal["claude-native"],
    active_harnesses: Collection[str] | None,
) -> dict[str, object]: ...


@overload
def apply_claude_settings(
    settings: str | None,
    *,
    executor: Literal["claude-sdk"],
    active_harnesses: Collection[str] | None,
) -> str | None: ...


def apply_claude_settings(
    settings: dict[str, object] | str | None,
    *,
    executor: Literal["claude-native", "claude-sdk"],
    active_harnesses: Collection[str] | None,
) -> dict[str, object] | str | None:
    """Append Claude deny entries without modifying the caller's settings.

    The executor selects native dict settings versus the SDK's JSON-string form.
    """
    entries = _active_entries("claude", active_harnesses)
    if not entries:
        return settings
    try:
        parsed = json.loads(settings) if isinstance(settings, str) else settings
    except (ValueError, RecursionError):
        return settings
    if parsed is None and settings is None:
        parsed = {}
    if not isinstance(parsed, dict):
        return settings
    denied = parsed.get("deniedMcpServers", [])
    if not isinstance(denied, list):
        return settings
    additions = []
    for key in entries:
        entry = {"serverName": _claude_name(key)}
        if entry not in denied and entry not in additions:
            additions.append(entry)
    if not additions:
        return settings
    merged = {**parsed, "deniedMcpServers": [*denied, *additions]}
    return merged if executor == "claude-native" else json.dumps(merged)


def claude_loaded_anyway(
    loaded_names: Iterable[str], *, active_harnesses: Collection[str] | None
) -> tuple[str, ...]:
    """Identify disabled names in an SDK init event, in saved order."""
    entries = _active_entries("claude", active_harnesses)
    loaded = set(loaded_names)
    return tuple(_claude_name(key) for key in entries if _claude_name(key) in loaded)


class CodexConfigProbe(Protocol):
    async def effective_servers(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout_s: float
    ) -> Mapping[str, bool]: ...


class SubprocessCodexProbe:
    """Ask Codex to resolve its own layered config without starting MCP servers."""

    def __init__(self, *, launch_args: Sequence[str] = ()) -> None:
        self._launch_args = tuple(launch_args)

    async def effective_servers(
        self, argv: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout_s: float
    ) -> Mapping[str, bool]:
        if not argv:
            raise ValueError("Missing Codex launch command")
        process = None
        try:
            async with asyncio.timeout(timeout_s):
                process = await asyncio.create_subprocess_exec(
                    *pipe_supervised_command(argv[0], *self._launch_args, *argv[1:]),
                    env=env,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    **spawn_kwargs(),
                )
                remember_process_group(process)
                stdout, _ = await process.communicate()
            if process.returncode != 0:
                raise ValueError("Codex MCP list failed")
            rows = json.loads(stdout)
            if not isinstance(rows, list):
                raise ValueError("Codex MCP list is not an array")
            result = {}
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not isinstance(row.get("name"), str)
                    or not isinstance(row.get("enabled"), bool)
                ):
                    raise ValueError("Invalid Codex MCP list entry")
                result[row["name"]] = row["enabled"]
            return result
        finally:
            if process is not None:
                try:
                    await terminate_subprocess(
                        process, label="Codex MCP probe", terminate_timeout=0.5, kill_timeout=0.5
                    )
                finally:
                    close_subprocess_transport(process)


@dataclass(frozen=True, slots=True)
class CodexLaunchPlan:
    config_overrides: tuple[str, ...]
    fallback_config_overrides: tuple[str, ...]
    outcome: Literal["none", "applied", "probe_failed"]
    dropped: tuple[str, ...]

    def overrides_after_early_exit(self) -> tuple[str, ...] | None:
        """Return the baseline for one retry after any pre-initialize exit.

        Disabled servers may run on retry; callers signal this with the warning event.
        """
        return self.fallback_config_overrides if self.outcome == "applied" else None


async def plan_codex_launch(
    launch_path: str,
    config_overrides: Sequence[str],
    *,
    executor: Literal["codex-native", "codex"],
    active_harnesses: Collection[str] | None,
    env: Mapping[str, str],
    cwd: Path,
    probe: CodexConfigProbe | None = None,
    timeout_s: float = 3.0,
) -> CodexLaunchPlan:
    """Plan overrides only for names in Codex's effective launch configuration."""
    original = tuple(config_overrides)
    entries = _active_entries("codex", active_harnesses)
    if not entries:
        return CodexLaunchPlan(original, original, "none", ())
    argv = [launch_path]
    for override in original:
        argv.extend(["-c", override])
    argv.extend(["mcp", "list", "--json"])
    try:
        servers = await (probe or SubprocessCodexProbe()).effective_servers(
            argv, env=env, cwd=cwd, timeout_s=timeout_s
        )
    except Exception:
        _logger.warning("mcp_disabled_preflight_failed executor=%s", executor, exc_info=True)
        return CodexLaunchPlan(original, original, "probe_failed", ())
    overrides = []
    dropped = []
    for key in entries:
        if key.server not in servers:
            dropped.append(key.server)
            continue
        name = key.server if _TOML_BARE_KEY.fullmatch(key.server) else json.dumps(key.server)
        overrides.append(f"mcp_servers.{name}.enabled=false")
    if dropped:
        _logger.warning("mcp_disabled_servers_not_reported names=%s", tuple(dropped))
    return CodexLaunchPlan(
        (*original, *overrides), original, "applied" if overrides else "none", tuple(dropped)
    )
