"""Expose enabled Claude Code plugins to managed Pi agent dirs.

Enabled plugins add their Pi packages, skill directories and generated command prompts
to the managed ``settings.json``. Entries are only appended, and a resource the user
already lists by the same resolved path is not added again. Excluding one resource of a
plugin is not supported yet; disabling the plugin in Claude Code drops all of its resources.
"""

from __future__ import annotations

import itertools
import logging
import os
import re
import shutil
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import TypeAlias

from omnigent.server.feature_flags import Feature, resolve_feature_flags
from omnigent.spec.skill_sources import (
    _enabled_plugin_keys,
    _plugin_install_paths,
    _read_json,
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
_MAX_WALK_DIRS = 256
_MAX_WALK_DEPTH = 8
_MAX_ENTRIES_PER_DIR = 1024
# Generated names must fit a common filesystem's single-component limit.
_MAX_FILENAME_BYTES = 255
# A plugin name becomes part of a generated filename, so it must be one safe component.
_PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Symlink loops, NUL bytes, and unknown ``~user`` prefixes raise these.
_PATH_ERRORS = (OSError, RuntimeError, ValueError)


def _resolve(value: str | Path, *, user_entry: bool = False) -> Path | None:
    """Resolved absolute path for *value*, or ``None`` when it cannot be determined.

    This is the one place paths are expanded or resolved. A user entry is expanded and
    must be absolute, since a relative entry is Pi's to resolve. Failures are warned
    about and skipped, so they never abort a launch.
    """
    try:
        path = Path(value).expanduser() if user_entry else Path(value)
        if user_entry and not path.is_absolute():
            return None
        return path.resolve()
    except _PATH_ERRORS as exc:
        _logger.warning("Ignoring Pi resource path %r: %s", str(value)[:200], exc)
        return None


def _listed_paths(entries: object) -> set[Path]:
    """Resolved paths of a user's plain entries; ``+``, ``-`` and ``!`` entries are not paths."""
    if not isinstance(entries, list):
        return set()
    listed: set[Path] = set()
    for entry in entries:
        source = entry.get("source") if isinstance(entry, dict) else entry
        resolved = _resolve(source, user_entry=True) if isinstance(source, str) else None
        if resolved is not None:
            listed.add(resolved)
    return listed


def _enabled_plugins() -> list[tuple[str, Path, bool]]:
    """Enabled Claude plugins as ``(name, install root, declares Pi)``.

    Installation records are read the way the MCP inventory reads them. Only user-level
    enablement is read; project-level ``enabledPlugins`` are not.
    """
    ctx = replace(skill_source_context_from_env(roots=(), harness="claude-native"), is_native=True)
    plugins: list[tuple[str, Path, bool]] = []
    for key, root in _plugin_install_paths(ctx, _enabled_plugin_keys(ctx)).items():
        manifest = _read_json(root / "package.json") or {}
        declares_pi = isinstance(manifest.get("pi"), dict)
        plugins.append((key.split("@", 1)[0], root, declares_pi))
    return plugins


def _walk(root: Path, wanted: Callable[[str], bool], limit: int) -> list[_Walked]:
    """Regular files under *root* whose name satisfies *wanted*, as ``(relative dirs, file)``.

    Symlinks and special files are not followed. Budgets bound the work: directories
    visited, depth, entries read from one directory (before sorting), and matches.
    """
    found: list[_Walked] = []
    queue: deque[tuple[tuple[str, ...], Path]] = deque([((), root)])
    visited = 0
    while queue and visited < _MAX_WALK_DIRS and len(found) < limit:
        relative, directory = queue.popleft()
        visited += 1
        try:
            entries = sorted(itertools.islice(directory.iterdir(), _MAX_ENTRIES_PER_DIR))
        except OSError as exc:
            _logger.warning("Skipping unreadable Claude plugin dir %s: %s", directory, exc)
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if len(relative) < _MAX_WALK_DEPTH:
                    queue.append(((*relative, entry.name), entry))
            elif entry.is_file() and wanted(entry.name):
                found.append((relative, entry))
                if len(found) >= limit:
                    break
    return found


def _skill_dirs(skills_dir: Path, listed: set[Path]) -> list[str]:
    """Skill directories under *skills_dir* the user does not list; a listed root claims all."""
    if skills_dir.is_symlink() or not skills_dir.is_dir() or _resolve(skills_dir) in listed:
        return []
    directories = [
        _resolve(skill_md.parent)
        for _, skill_md in _walk(skills_dir, lambda n: n == "SKILL.md", MAX_PLUGIN_FILES)
    ]
    return [str(d) for d in directories if d is not None and d not in listed]


def _write_command(
    name: str, root: Path, parts: tuple[str, ...], source: Path, prompts_dir: Path
) -> bool:
    """Write one command prompt named ``<plugin>:<command>``; returns ``False`` if skipped."""
    command = ":".join((name, *parts))
    try:
        if len(f"{command}.md".encode()) > _MAX_FILENAME_BYTES:
            _logger.warning("Skipping Claude command %s: generated name is too long", command)
            return False
        if not source.resolve().is_relative_to(root):
            return False
        if source.stat().st_size > MAX_COMMAND_BYTES:
            _logger.warning("Skipping Claude command %s: file is too large", command)
            return False
        target = prompts_dir / f"{command}.md"
        if target.exists():
            # Another marketplace's plugin with the same name already claimed this command.
            _logger.info("Skipping Claude command %s: name already provided", command)
            return False
        body = source.read_text(encoding="utf-8")
        prompts_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(body.replace(_PLUGIN_ROOT_VARIABLE, str(root)), encoding="utf-8")
    except _PATH_ERRORS as exc:
        _logger.warning("Skipping Claude command %s: %s", command, exc)
        return False
    return True


def _write_commands(name: str, root: Path, prompts_dir: Path, listed: set[Path]) -> bool:
    """Write the plugin's ``commands/**/*.md``; returns whether any prompt was written.

    A commands directory or command file the user's ``prompts`` setting lists by resolved
    path is not written again. Files are read only from inside the plugin root.
    """
    commands_dir = root / "commands"
    if commands_dir.is_symlink() or not commands_dir.is_dir() or _resolve(commands_dir) in listed:
        return False
    written = False
    for relative, source in _walk(commands_dir, lambda n: n.endswith(".md"), MAX_PLUGIN_FILES):
        resolved = _resolve(source)
        if resolved is None or resolved in listed:
            continue
        written |= _write_command(name, root, (*relative, source.stem), source, prompts_dir)
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
