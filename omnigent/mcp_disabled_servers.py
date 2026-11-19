"""Host-owned MCP disable state, applied only to explicitly active launches.

Codex names are read in-process from prepared private, trusted project and CLI
config layers; launch never waits for a discovery subprocess. If that best-effort
reading disagrees with Codex and it exits before initialize, callers retry once
without the added overrides. Disabled servers may then run, signalled by a warning.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import tomllib
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, overload

import tomlkit

from omnigent.config import global_config_path
from omnigent.harnesses.codex_native.launch_args import _merge_tables

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
_read_failures: set[tuple[Path, str, str]] = set()
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
            or any(
                ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF
                for character in name
            )
        ):
            raise DisabledServerWriteError("invalid_name")
    if key.harness != "claude" and key.plugin is not None:
        raise DisabledServerWriteError("invalid_name")


def _log_read_failure(
    path: Path, reason: str, *, event: str = "mcp_disabled_servers_read_failed"
) -> None:
    with _read_failure_lock:
        if (path, reason, event) in _read_failures:
            return
        _read_failures.add((path, reason, event))
    _logger.warning("%s path=%s reason=%s", event, path, reason)


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


def read_disabled_servers() -> frozenset[ServerKey]:
    """Read a preference snapshot; unreadable storage behaves as empty."""
    return frozenset(_read(global_config_path().parent / FILENAME))


def is_disabled(key: ServerKey) -> bool:
    """Return normalized saved state; unreadable storage behaves as empty."""
    return key in read_disabled_servers()


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


def _read_codex_config(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        _log_read_failure(
            path, type(error).__name__, event="mcp_disabled_codex_config_read_failed"
        )
        return {}


def _codex_cli_overrides(overrides: Sequence[str], config_path: Path) -> dict[str, Any]:
    """Build Codex's single CLI layer, assigning each dotted path in order."""
    result: dict[str, Any] = {}
    for override in overrides:
        key, separator, value = override.partition("=")
        try:
            if not separator:
                raise ValueError("Missing assignment")
            try:
                parsed = tomllib.loads(f"value={value}")["value"]
            except ValueError:
                # Codex accepts unquoted strings when a value is not valid TOML.
                parsed = value.strip()
            # Codex splits the key literally; TOML quoting applies only to the value.
            *parents, name = key.strip().split(".")
            target = result
            for parent in parents:
                if not isinstance(target.get(parent), dict):
                    target[parent] = {}
                target = target[parent]
            target[name] = parsed
        except (ValueError, KeyError, TypeError, RecursionError):
            # Do not log override values: they may contain credentials.
            _log_read_failure(
                config_path, "InvalidOverride", event="mcp_disabled_codex_config_read_failed"
            )
    return result


def _codex_git_root(cwd: Path) -> Path | None:
    """Resolve the trust root, including linked worktrees, without invoking git."""
    for directory in (cwd, *cwd.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            if (dot_git / "HEAD").exists():
                return directory
        elif dot_git.is_file() and not dot_git.is_symlink():
            try:
                pointer = dot_git.read_text().strip()
                if not pointer.startswith("gitdir:"):
                    return None
                git_dir = (directory / pointer.removeprefix("gitdir:").strip()).resolve()
                common = (git_dir / (git_dir / "commondir").read_text().strip()).resolve()
                registered = Path((git_dir / "gitdir").read_text().strip())
                if (
                    git_dir.parent.name == "worktrees"
                    and common == git_dir.parent.parent
                    and registered.name == ".git"
                    and registered.parent.resolve() == directory.resolve()
                    and (common.parent / ".git").resolve() == common
                ):
                    return common.parent
            except (OSError, ValueError, RuntimeError, UnicodeError):
                return None
            return None
    return None


def _codex_layer_trusted(projects: Any, *candidates: Path | None) -> bool:
    if not isinstance(projects, dict):
        return False
    for candidate in candidates:
        if candidate is None:
            continue
        for key in (str(candidate.resolve()), str(candidate)):
            project = projects.get(key)
            level = project.get("trust_level") if isinstance(project, dict) else None
            if level in ("trusted", "untrusted"):
                return level == "trusted"
    return False


def _codex_server_names(codex_home: Path, cwd: Path, config_overrides: Sequence[str]) -> set[str]:
    """Read already-materialized profile MCPs, then trusted project and CLI layers."""
    config_path = codex_home / "config.toml"
    config = _read_codex_config(config_path)
    cli = _codex_cli_overrides(config_overrides, config_path)
    discovery: dict[str, Any] = {}
    _merge_tables(discovery, config)
    _merge_tables(discovery, cli)
    markers = discovery.get("project_root_markers", [".git"])
    if not isinstance(markers, list) or any(not isinstance(marker, str) for marker in markers):
        _log_read_failure(
            config_path, "InvalidProjectRootMarkers", event="mcp_disabled_codex_config_read_failed"
        )
        markers = []
    directories = (cwd, *cwd.parents)
    root = cwd
    for directory in directories:
        if any(
            (directory / marker).exists()
            and (
                marker != ".git"
                or not (directory / marker).is_dir()
                or (directory / marker / "HEAD").exists()
            )
            for marker in markers
        ):
            root = directory
            break
    git_root = _codex_git_root(cwd)
    projects = discovery.get("projects", {})
    layers = list(directories[: directories.index(root) + 1])
    for directory in reversed(layers):
        # Trust is checked per layer: a trusted cwd does not trust its parents.
        trusted = _codex_layer_trusted(projects, directory, root, git_root)
        project_home = directory / ".codex"
        if trusted and project_home.resolve() != codex_home.resolve():
            _merge_tables(config, _read_codex_config(project_home / "config.toml"))
    _merge_tables(config, cli)
    servers = config.get("mcp_servers", {})
    return (
        {name for name, server in servers.items() if isinstance(server, dict)}
        if isinstance(servers, dict)
        else set()
    )


@dataclass(frozen=True, slots=True)
class CodexLaunchPlan:
    config_overrides: tuple[str, ...]
    fallback_config_overrides: tuple[str, ...]
    outcome: Literal["none", "applied"]
    dropped: tuple[str, ...]

    def overrides_after_early_exit(self) -> tuple[str, ...] | None:
        """Return the baseline for one retry after any pre-initialize exit.

        Disabled servers may run on retry; callers signal this with the warning event.
        """
        return self.fallback_config_overrides if self.outcome == "applied" else None


def plan_codex_launch(
    config_overrides: Sequence[str],
    *,
    executor: Literal["codex-native", "codex"],
    active_harnesses: Collection[str] | None,
    codex_home: Path,
    cwd: Path,
) -> CodexLaunchPlan:
    """Plan synchronously from prepared config files; never spawn or await a probe.

    Call after profile/MCP preparation in the session's private CODEX_HOME and
    before building argv. Unreadable layers are skipped with one warning each.
    """
    original = tuple(config_overrides)
    entries = _active_entries("codex", active_harnesses)
    if not entries:
        return CodexLaunchPlan(original, original, "none", ())
    try:
        servers = _codex_server_names(codex_home.resolve(), cwd.absolute(), original)
    except (OSError, ValueError, RuntimeError, RecursionError) as error:
        _log_read_failure(
            codex_home / "config.toml",
            type(error).__name__,
            event="mcp_disabled_codex_config_read_failed",
        )
        servers = set()
    applied = [key.server for key in entries if key.server in servers]
    dropped = [key.server for key in entries if key.server not in servers]
    overrides = []
    if any(not _TOML_BARE_KEY.fullmatch(name) for name in applied):
        # Codex splits dotted paths literally. Rebuild its CLI MCP table so
        # adding quoted names preserves existing CLI transports.
        cli_servers = _codex_cli_overrides(original, codex_home / "config.toml").get(
            "mcp_servers", {}
        )
        for name in applied:
            cli_servers.setdefault(name, {})["enabled"] = False
        table = tomlkit.inline_table()
        table.update(cli_servers)
        overrides.append(f"mcp_servers={table.as_string()}")
    else:
        overrides.extend(f"mcp_servers.{name}.enabled=false" for name in applied)
    if dropped:
        _logger.warning(
            "mcp_disabled_servers_not_reported names=%s",
            tuple(dropped),
            extra={"executor": executor},
        )
    return CodexLaunchPlan(
        (*original, *overrides), original, "applied" if overrides else "none", tuple(dropped)
    )
