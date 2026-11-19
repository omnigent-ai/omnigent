"""Expose enabled Claude Code plugins to managed Pi agent dirs.

Enabled plugins add their Pi packages, skill directories and generated command prompts
to the managed ``settings.json``. Entries are only appended, and a resource the user
already lists by the same resolved path is not added again. Excluding one resource of a
plugin is not supported yet; disabling the plugin in Claude Code drops all of its resources.

Command names follow the existing plugin command discovery: ``<plugin>:<path under
commands/>``, where a frontmatter ``name`` replaces the filename. Unlike that discovery,
only lowercase ``.md`` files are bridged, and a ``skill.md`` inside a directory is a plain
command rather than a skill, and ``user-invocable`` is not honored.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import shutil
import stat
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import TypeAlias

import yaml

from omnigent.server.feature_flags import Feature, resolve_feature_flags
from omnigent.spec.skill_sources import (
    _FRONTMATTER_RE,
    _enabled_plugin_keys,
    _quote_description_with_colon,
    skill_source_context_from_env,
)

_logger = logging.getLogger(__name__)

_Settings: TypeAlias = dict[str, object]
_Walked: TypeAlias = tuple[tuple[str, ...], Path]

# Directory under the managed agent dir holding generated command prompts.
PI_CLAUDE_PLUGIN_RESOURCES_DIR = "claude-plugins"
_PLUGIN_ROOT_VARIABLE = "${CLAUDE_PLUGIN_ROOT}"
MAX_PLUGIN_FILES = 512
MAX_COMMAND_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_SKILL_BYTES = 1024 * 1024
_MAX_WALK_DIRS = 256
_MAX_WALK_DEPTH = 8
_MAX_ENTRIES_PER_DIR = 1024
# Generated names must fit a common filesystem's single-component limit.
_MAX_FILENAME_BYTES = 255
# A plugin name becomes part of a generated filename, so it must be one safe component.
_PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Symlink loops, NUL bytes, and unknown ``~user`` prefixes raise these.
_PATH_ERRORS = (OSError, RuntimeError, ValueError)
# The user's own Claude config is checked for readability before the shared readers open it.
_USER_CONFIG_FILES = (
    ("settings.json", False),
    ("settings.local.json", False),
    ("managed_plugins.json", True),
)


def _read_bytes(path: Path, limit: int, *, follow_symlinks: bool = False) -> bytes | None:
    """The bytes of the regular file at *path*, or ``None`` with a warning.

    The single bounded reader for this module. The file is opened without blocking, so
    a FIFO or device cannot stall the caller, and the opened descriptor is checked to be
    a regular file of at most *limit* bytes. Symlinks are refused unless
    *follow_symlinks* is set, which is only for the user's own Claude config.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if not follow_symlinks:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _logger.warning("Skipping Pi resource file %s: %s", path, exc)
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                _logger.warning("Skipping %s: not a regular file", path)
                return None
            data = handle.read(limit + 1)
    except OSError as exc:
        _logger.warning("Skipping Pi resource file %s: %s", path, exc)
        return None
    if len(data) > limit:
        _logger.warning("Skipping %s: larger than %d bytes", path, limit)
        return None
    return data


def _read_text(path: Path, limit: int, *, follow_symlinks: bool = False) -> str | None:
    """Like :func:`_read_bytes`, decoded as UTF-8; ``None`` when unreadable or not text."""
    data = _read_bytes(path, limit, follow_symlinks=follow_symlinks)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        _logger.warning("Skipping %s: not UTF-8 text", path)
        return None


def _read_json_object(path: Path, limit: int, *, follow_symlinks: bool = False) -> dict | None:
    """A JSON object read through :func:`_read_bytes`, or ``None``."""
    text = _read_text(path, limit, follow_symlinks=follow_symlinks)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        _logger.warning("Skipping %s: not valid JSON", path)
        return None
    return data if isinstance(data, dict) else None


def _resolve(value: str | Path, *, user_entry: bool = False) -> Path | None:
    """Resolved absolute path for *value*, or ``None`` when it cannot be determined.

    This is the one place paths are expanded or resolved. A user entry is expanded and
    must be absolute, since a relative entry is Pi's to resolve. Failures are warned
    about and skipped, so they never abort a launch.
    """
    if "\0" in str(value):
        _logger.warning("Ignoring Pi resource path %r: contains a NUL byte", str(value)[:200])
        return None
    try:
        path = Path(value).expanduser() if user_entry else Path(value)
        if user_entry and not path.is_absolute():
            return None
        return path.resolve()
    except _PATH_ERRORS as exc:
        _logger.warning("Ignoring Pi resource path %r: %s", str(value)[:200], exc)
        return None


def _entry_source(entry: object) -> str | None:
    """The source string of a Pi resource entry, which may be an object form."""
    source = entry.get("source") if isinstance(entry, dict) else entry
    return source if isinstance(source, str) and source else None


def _listed_paths(entries: object) -> set[Path]:
    """Resolved paths of a user's plain entries; ``+``, ``-`` and ``!`` entries are not paths."""
    if not isinstance(entries, list):
        return set()
    listed: set[Path] = set()
    for entry in entries:
        source = _entry_source(entry)
        resolved = _resolve(source, user_entry=True) if source is not None else None
        if resolved is not None:
            listed.add(resolved)
    return listed


def _claude_user_dir() -> Path:
    """Claude Code's user config dir: ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def _user_config_is_readable(claude: Path) -> bool:
    """Whether the user's Claude config files that the shared readers open are regular files.

    A special file such as a FIFO there would block those readers, so the whole bridge is
    skipped with a warning instead.
    """
    for relative, in_plugins in _USER_CONFIG_FILES:
        path = (claude / "plugins" if in_plugins else claude) / relative
        try:
            if not path.exists():
                continue
            mode = os.stat(path).st_mode
        except OSError as exc:
            _logger.warning("Not bridging Claude plugins: %s is unreadable: %s", path, exc)
            return False
        if not stat.S_ISREG(mode):
            _logger.warning("Not bridging Claude plugins: %s is not a regular file", path)
            return False
    return True


def _user_install_path(key: str, entries: list, plugins_root: Path) -> Path | None:
    """The user-scoped install path of *key*, inside the plugins root, or ``None``.

    Records for another scope or project are skipped, so a user-enabled key cannot load
    an install that belongs to a different project.
    """
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("scope", "user") != "user" or entry.get("projectPath"):
            _logger.warning("Not bridging Claude plugin %s: installation is not user-scoped", key)
            continue
        raw = entry.get("installPath")
        if not isinstance(raw, str) or not raw:
            continue
        candidate = Path(raw)
        resolved = _resolve(candidate if candidate.is_absolute() else plugins_root / candidate)
        if resolved is not None and resolved.is_relative_to(plugins_root):
            return resolved
        _logger.warning("Not bridging Claude plugin %s: install path is outside the plugins", key)
    return None


def _enabled_plugins() -> list[tuple[str, Path, bool]]:
    """Enabled user-scope Claude plugins as ``(name, install root, declares Pi)``.

    Only the user's own enablement is read; project-level ``enabledPlugins`` are not.
    """
    claude = _claude_user_dir()
    if not _user_config_is_readable(claude):
        return []
    plugins_root = (claude / "plugins").resolve()
    installed = _read_json_object(
        claude / "plugins" / "installed_plugins.json", MAX_MANIFEST_BYTES, follow_symlinks=True
    )
    records = installed.get("plugins") if installed else None
    if not isinstance(records, dict):
        return []
    ctx = replace(skill_source_context_from_env(roots=(), harness="claude-native"), is_native=True)
    enabled = _enabled_plugin_keys(ctx)
    plugins: list[tuple[str, Path, bool]] = []
    for key, entries in records.items():
        if key not in enabled or not isinstance(entries, list):
            continue
        root = _user_install_path(key, entries, plugins_root)
        if root is None:
            continue
        manifest = _read_json_object(root / "package.json", MAX_MANIFEST_BYTES)
        declares_pi = bool(manifest) and isinstance(manifest.get("pi"), dict)
        plugins.append((key.split("@", 1)[0], root, declares_pi))
    return plugins


def _walk(root: Path, wanted: Callable[[str], bool], limit: int) -> list[_Walked]:
    """Regular files under *root* whose name satisfies *wanted*, as ``(relative dirs, file)``.

    Symlinks and special files are not followed. The listing of each directory is
    streamed through :func:`os.scandir` and cut at :data:`_MAX_ENTRIES_PER_DIR` before
    sorting, so a huge directory is never fully enumerated. The directory, depth, and
    match budgets bound the rest of the walk.
    """
    found: list[_Walked] = []
    queue: deque[tuple[tuple[str, ...], Path]] = deque([((), root)])
    visited = 0
    while queue and visited < _MAX_WALK_DIRS and len(found) < limit:
        relative, directory = queue.popleft()
        visited += 1
        try:
            with os.scandir(directory) as scan:
                listed = sorted(itertools.islice(scan, _MAX_ENTRIES_PER_DIR), key=lambda e: e.name)
        except OSError as exc:
            _logger.warning("Skipping unreadable Claude plugin dir %s: %s", directory, exc)
            continue
        for entry in listed:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if len(relative) < _MAX_WALK_DEPTH:
                    queue.append(((*relative, entry.name), Path(entry.path)))
            elif entry.is_file() and wanted(entry.name):
                found.append((relative, Path(entry.path)))
                if len(found) >= limit:
                    break
    return found


def _skill_dirs(skills_dir: Path, listed: set[Path]) -> list[str]:
    """Skill directories under *skills_dir* the user does not list; a listed root claims all."""
    if skills_dir.is_symlink() or not skills_dir.is_dir() or _resolve(skills_dir) in listed:
        return []
    directories: list[str] = []
    for _, skill_md in _walk(skills_dir, lambda n: n == "SKILL.md", MAX_PLUGIN_FILES):
        if _read_bytes(skill_md, MAX_SKILL_BYTES) is None:
            continue
        skill_dir = _resolve(skill_md.parent)
        if skill_dir is not None and skill_dir not in listed:
            directories.append(str(skill_dir))
    return directories


def _command_name(text: str, segments: tuple[str, ...]) -> str:
    """The command name for *segments*, where a frontmatter ``name`` replaces the last one.

    Matches the existing plugin command discovery in ``omnigent.spec.skill_sources``.
    """
    *parents, stem = segments
    name = stem
    match = _FRONTMATTER_RE.match(text)
    if match:
        try:
            frontmatter = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            try:
                frontmatter = yaml.safe_load(_quote_description_with_colon(match.group(1)))
            except yaml.YAMLError:
                frontmatter = None
        raw = frontmatter.get("name") if isinstance(frontmatter, dict) else None
        if isinstance(raw, str) and raw.strip():
            name = raw.strip()
    return ":".join((*parents, name))


def _write_command(name: str, root: Path, suffix: str, text: str, prompts_dir: Path) -> bool:
    """Write one command prompt named ``<plugin>:<suffix>``; returns ``False`` if skipped."""
    command = f"{name}:{suffix}"
    if len(f"{command}.md".encode()) > _MAX_FILENAME_BYTES:
        _logger.warning("Skipping Claude command %s: generated name is too long", command)
        return False
    target = prompts_dir / f"{command}.md"
    try:
        if target.exists():
            # Another marketplace's plugin with the same name already claimed this command.
            _logger.info("Skipping Claude command %s: name already provided", command)
            return False
        prompts_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(text.replace(_PLUGIN_ROOT_VARIABLE, str(root)), encoding="utf-8")
    except _PATH_ERRORS as exc:
        _logger.warning("Skipping Claude command %s: %s", command, exc)
        return False
    return True


def _write_commands(name: str, root: Path, prompts_dir: Path, listed: set[Path]) -> bool:
    """Write the plugin's ``commands/**/*.md`` as ``<plugin>:<command>`` prompt templates.

    A commands directory, or a command file, that the user's ``prompts`` setting lists by
    resolved path is not written again. Files are read only through :func:`_read_text`
    from inside the plugin root.

    :returns: ``True`` when at least one prompt was written.
    """
    commands_dir = root / "commands"
    if commands_dir.is_symlink() or not commands_dir.is_dir() or _resolve(commands_dir) in listed:
        return False
    written = False
    for relative, source in _walk(commands_dir, lambda n: n.endswith(".md"), MAX_PLUGIN_FILES):
        resolved = _resolve(source)
        if resolved is None or resolved in listed or not resolved.is_relative_to(root):
            continue
        text = _read_text(source, MAX_COMMAND_BYTES)
        if text is None:
            continue
        suffix = _command_name(text, (*relative, source.stem))
        written |= _write_command(name, root, suffix, text, prompts_dir)
    return written


def _append_unclaimed(existing: object, additions: Iterable[str]) -> list[object] | None:
    """Return *existing* with *additions* appended, skipping any the user already lists.

    :returns: The new list, or ``None`` when the user's value is not a list.
    """
    if existing is None:
        current: list[object] = []
    elif isinstance(existing, list):
        current = list(existing)
    else:
        _logger.warning("Not adding Claude plugin resources: Pi setting is not a list")
        return None
    listed = _listed_paths(current)
    for addition in additions:
        path = _resolve(addition)
        if path is None or path in listed:
            continue
        listed.add(path)
        current.append(addition)
    return current


def add_claude_plugin_resources(settings: _Settings, managed_dir: Path) -> _Settings:
    """Add enabled Claude plugin packages, skills and commands to Pi *settings*.

    A plugin whose ``package.json`` declares a ``pi`` manifest loads as a Pi package,
    which also governs its skills. Other plugins contribute each skill directory.
    Commands are regenerated under *managed_dir* on every call, so disabled or removed
    plugins leave nothing behind.

    The feature flag is read from this process's environment, which is the runner or
    host that launches Pi; a server-only setting does not reach it.

    :param settings: The managed settings after user relocation, before overlays.
    :param managed_dir: The managed agent dir the settings are written into.
    :returns: ``settings`` with the plugin entries appended.
    """
    generated = managed_dir / PI_CLAUDE_PLUGIN_RESOURCES_DIR
    shutil.rmtree(generated, ignore_errors=True)
    if not resolve_feature_flags().enabled(Feature.PI_CLAUDE_PLUGINS):
        return settings
    try:
        plugins = _enabled_plugins()
    except _PATH_ERRORS:
        _logger.exception("Not adding Claude plugin resources to the Pi agent dir")
        return settings
    if not plugins:
        return settings
    listed = {
        kind: _listed_paths(settings.get(kind)) for kind in ("packages", "skills", "prompts")
    }
    packages: list[str] = []
    skills: list[str] = []
    has_commands = False
    for name, root, declares_pi in plugins:
        try:
            resolved = _resolve(root)
            if not _PLUGIN_NAME.fullmatch(name) or resolved is None:
                _logger.warning("Not bridging Claude plugin %r", name)
                continue
            if resolved not in listed["packages"]:
                if declares_pi:
                    packages.append(str(resolved))
                else:
                    # A package the user lists for this plugin already brings its skills.
                    skills.extend(_skill_dirs(root / "skills", listed["skills"]))
            # ``:`` in a command name is not a legal filename on Windows.
            if os.name != "nt":
                has_commands |= _write_commands(
                    name, resolved, generated / "prompts", listed["prompts"]
                )
        except _PATH_ERRORS:
            _logger.warning("Skipping Claude plugin %s: unusable path", name, exc_info=True)
    additions: dict[str, list[str]] = {"packages": packages, "skills": skills}
    if has_commands:
        additions["prompts"] = [str(generated / "prompts")]
    updated = dict(settings)
    for kind, paths in additions.items():
        if paths:
            result = _append_unclaimed(settings.get(kind), paths)
            if result is not None:
                updated[kind] = result
    return updated
