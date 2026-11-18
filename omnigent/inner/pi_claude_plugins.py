"""Expose enabled Claude Code plugins to managed Pi agent dirs.

Pi loads skills, packages and prompt templates from its settings, so the managed
``settings.json`` gains entries for the plugins Claude Code has enabled. Commands
become generated prompt templates because Claude's ``${CLAUDE_PLUGIN_ROOT}`` must
be substituted. Entries are only appended. Anything the user's own Pi settings name
or exclude, including through an enclosing directory or a glob, is left alone.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
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
_WalkedFile: TypeAlias = tuple[tuple[str, ...], Path]

# Directory under the managed agent dir holding generated command prompts.
PI_CLAUDE_PLUGIN_RESOURCES_DIR = "claude-plugins"
_PLUGIN_ROOT_VARIABLE = "${CLAUDE_PLUGIN_ROOT}"
MAX_PLUGIN_COMMANDS = 512
MAX_COMMAND_BYTES = 256 * 1024
_MAX_WALK_DIRS = 256
_MAX_WALK_DEPTH = 8
# Generated names must fit a common filesystem's single-component limit.
_MAX_FILENAME_BYTES = 255
# A plugin name becomes part of a generated filename, so it must be one safe component.
_PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Errors from resolving a path that a user or plugin controls: symlink loops and NUL bytes.
_PATH_ERRORS = (OSError, RuntimeError, ValueError)


@dataclass(frozen=True)
class _Claims:
    """What one of the user's Pi resource arrays already decided.

    :param named: Resolved paths the user lists, as include (``+``), exclude (``-``)
        or plain entries. Any of them means the user decided that path.
    :param patterns: ``!`` glob exclusions the user wrote.
    """

    named: frozenset[str]
    patterns: tuple[str, ...]

    def decides(self, path: Path, base: Path) -> bool:
        """Whether the user named *path* or an enclosing directory below *base*, or matched a glob.

        :param path: A resolved path.
        :param base: The outermost directory whose ancestors are still checked.
        """
        for candidate in (path, *path.parents):
            if str(candidate) in self.named:
                return True
            if candidate == base:
                break
        text = str(path)
        return any(fnmatch.fnmatchcase(text, pattern) for pattern in self.patterns)


def _safe_resolve(path: str) -> str | None:
    """Resolved path text, or ``None`` when the path cannot be resolved."""
    try:
        return str(Path(path).expanduser().resolve())
    except _PATH_ERRORS:
        return None


def _entry_source(entry: object) -> str | None:
    """The source string of a Pi resource entry, which may be an object form."""
    source = entry.get("source") if isinstance(entry, dict) else entry
    return source if isinstance(source, str) and source else None


def _claims(entries: object) -> _Claims:
    """Claims from a user's resource array; a value that is not a list claims nothing."""
    if not isinstance(entries, list):
        return _Claims(frozenset(), ())
    named: set[str] = set()
    patterns: list[str] = []
    for entry in entries:
        source = _entry_source(entry)
        if source is None:
            continue
        if source.startswith("!"):
            patterns.append(source[1:])
            continue
        # Relative user entries resolve from Pi's agent dir, which the caller already
        # relocated to absolute paths; resolving them here would use the process cwd.
        listed = Path(source.lstrip("+-")).expanduser()
        if not listed.is_absolute():
            continue
        resolved = _safe_resolve(str(listed))
        if resolved is not None:
            named.add(resolved)
    return _Claims(frozenset(named), tuple(patterns))


def _pi_manifest(root: Path) -> Mapping[str, object] | None:
    """Return the ``pi`` object of the plugin's ``package.json``, when it declares one."""
    manifest = _read_json(root / "package.json")
    pi = manifest.get("pi") if manifest else None
    return pi if isinstance(pi, dict) else None


def _enabled_plugins() -> list[tuple[str, Path, bool]]:
    """Enabled Claude plugins as ``(name, install root, declares Pi)``.

    Installation records are read the way the MCP inventory reads them: the first
    usable install path per plugin, whatever its Claude scope. Only user-level
    enablement is read; project-level ``enabledPlugins`` are not.
    """
    ctx = replace(
        skill_source_context_from_env(roots=(), harness="claude-native"),
        is_native=True,
    )
    plugins: list[tuple[str, Path, bool]] = []
    for key, root in _plugin_install_paths(ctx, _enabled_plugin_keys(ctx)).items():
        plugins.append((key.split("@", 1)[0], root, _pi_manifest(root) is not None))
    return plugins


def _bounded_files(root: Path, wanted: Callable[[str], bool]) -> list[_WalkedFile]:
    """Files under *root* whose name satisfies *wanted*, as ``(relative dirs, file)``.

    Symlinks are never followed. The walk stops after :data:`_MAX_WALK_DIRS`
    directories, at :data:`_MAX_WALK_DEPTH` levels, or after :data:`MAX_PLUGIN_COMMANDS`
    matches, so a large or looping tree cannot delay every launch.
    """
    found: list[_WalkedFile] = []
    queue: deque[tuple[tuple[str, ...], Path]] = deque([((), root)])
    visited = 0
    while queue and visited < _MAX_WALK_DIRS and len(found) < MAX_PLUGIN_COMMANDS:
        relative, directory = queue.popleft()
        visited += 1
        try:
            entries = sorted(directory.iterdir())
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
    return found


def _write_command(
    name: str,
    root: Path,
    parts: tuple[str, ...],
    source: Path,
    prompts_dir: Path,
    claims: _Claims,
) -> bool:
    """Write one command prompt; returns ``False`` when it is skipped."""
    command = ":".join((name, *parts))
    if len(f"{command}.md".encode()) > _MAX_FILENAME_BYTES:
        _logger.warning("Skipping Claude command %s: generated name is too long", command)
        return False
    try:
        base = root.resolve()
        resolved = source.resolve()
        if not resolved.is_relative_to(base) or claims.decides(resolved, base):
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


def _write_commands(name: str, root: Path, prompts_dir: Path, claims: _Claims) -> bool:
    """Write the plugin's ``commands/**/*.md`` as ``<name>:<command>.md`` prompt templates.

    Commands the user's ``prompts`` setting names or excludes are not written. Files are
    read only when they resolve inside the plugin root, so a symlink cannot expose
    content from elsewhere on the host.

    :returns: ``True`` when at least one prompt was written.
    """
    commands_dir = root / "commands"
    if commands_dir.is_symlink() or not commands_dir.is_dir():
        return False
    if not commands_dir.resolve().is_relative_to(root.resolve()):
        _logger.warning("Skipping Claude plugin %s: commands/ links outside the plugin", name)
        return False
    written = False
    for relative, source in _bounded_files(commands_dir, lambda n: n.endswith(".md")):
        parts = (*relative, source.stem)
        written |= _write_command(name, root, parts, source, prompts_dir, claims)
    return written


def _skill_dirs(skills_dir: Path, base: Path, claims: _Claims) -> list[str]:
    """Skill directories (those holding ``SKILL.md``) the user has not decided against.

    Each skill directory is added on its own, so a user exclusion of one skill does
    not depend on how Pi treats exclusions inside a directory entry.
    """
    if skills_dir.is_symlink() or not skills_dir.is_dir():
        return []
    try:
        if not skills_dir.resolve().is_relative_to(base):
            return []
    except _PATH_ERRORS:
        return []
    directories: list[str] = []
    for _, skill_md in _bounded_files(skills_dir, lambda n: n == "SKILL.md"):
        resolved = _safe_resolve(str(skill_md.parent))
        if resolved is None:
            continue
        skill_dir = Path(resolved)
        if not claims.decides(skill_dir, base):
            directories.append(resolved)
    return directories


def _append_unclaimed(existing: object, additions: Iterable[str]) -> list[object] | None:
    """Return *existing* with *additions* appended, skipping any the user already decided.

    :returns: The new list, or ``None`` when the user's value is not a list.
    """
    if existing is None:
        current: list[object] = []
    elif isinstance(existing, list):
        current = list(existing)
    else:
        _logger.warning("Not adding Claude plugin resources: Pi setting is not a list")
        return None
    claims = _claims(current)
    seen: set[str] = set()
    for addition in additions:
        resolved = _safe_resolve(addition)
        if resolved is None or resolved in seen:
            continue
        path = Path(resolved)
        if claims.decides(path, Path(path.anchor)):
            continue
        seen.add(resolved)
        current.append(addition)
    return current


def add_claude_plugin_resources(settings: _Settings, managed_dir: Path) -> _Settings:
    """Add enabled Claude plugin packages, skills and commands to Pi *settings*.

    Plugins whose ``package.json`` declares a ``pi`` manifest load through Pi's
    package mechanism, which also governs their skills. Other plugins contribute
    each skill directory. Commands are written under *managed_dir* and regenerated
    on every call, so disabled or removed plugins leave nothing behind.

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
    claims = {kind: _claims(settings.get(kind)) for kind in ("packages", "skills", "prompts")}
    packages: list[str] = []
    skills: list[str] = []
    has_commands = False
    for name, root, declares_pi in plugins:
        try:
            if not _PLUGIN_NAME.fullmatch(name):
                _logger.warning("Not bridging Claude plugin with unsafe name %r", name)
                continue
            if declares_pi:
                if not claims["packages"].decides(root, root):
                    packages.append(str(root))
            elif not claims["packages"].decides(root, root):
                # A user package entry for this plugin decides its skills, including none.
                skills.extend(_skill_dirs(root / "skills", root, claims["skills"]))
            if os.name != "nt":
                # ``:`` in the command name is not a legal filename on Windows.
                has_commands |= _write_commands(
                    name, root, generated / "prompts", claims["prompts"]
                )
            elif (root / "commands").is_dir():
                _logger.info("Not bridging Claude plugin commands on Windows: %s", name)
        except _PATH_ERRORS:
            _logger.warning("Skipping Claude plugin %s: unusable path", name, exc_info=True)
    additions: dict[str, list[str]] = {"packages": packages, "skills": skills}
    if has_commands:
        additions["prompts"] = [str(generated / "prompts")]
    updated = dict(settings)
    for kind, paths in additions.items():
        if not paths:
            continue
        result = _append_unclaimed(settings.get(kind), paths)
        if result is not None:
            updated[kind] = result
    return updated
