"""Expose enabled Claude Code plugins to managed Pi agent dirs.

Pi loads skills, packages and prompt templates from its settings, so the managed
``settings.json`` gains entries for the plugins Claude Code has enabled. Commands
become generated prompt templates because Claude's ``${CLAUDE_PLUGIN_ROOT}`` must
be substituted. Entries are only appended. Anything the user's own Pi settings name
or exclude, through the path itself, an enclosing directory, or a glob, is left alone.
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
# Symlink loops, NUL bytes, and unknown ``~user`` prefixes raise these.
_PATH_ERRORS = (OSError, RuntimeError, ValueError)


def _resolve_path(value: str | Path, *, user_entry: bool = False) -> Path | None:
    """The resolved absolute form of *value*, or ``None`` when it cannot be determined.

    This is the only place paths are expanded or resolved. A user entry is expanded
    (``~``) and must then be absolute: a relative entry is Pi's to resolve against its
    agent dir, which the caller has already made absolute. Anything that fails to
    resolve is skipped with a warning, so it never aborts a launch.
    """
    try:
        path = Path(value).expanduser() if user_entry else Path(value)
        if user_entry and not path.is_absolute():
            return None
        return path.resolve()
    except _PATH_ERRORS as exc:
        _logger.warning("Ignoring Pi resource path %r: %s", str(value)[:200], exc)
        return None


def _is_within(child: Path, ancestor: Path) -> bool:
    """Whether *child* is *ancestor* or lies below it. Both must be resolved."""
    return child == ancestor or ancestor in child.parents


@dataclass(frozen=True)
class _Claims:
    """What one of the user's Pi resource arrays already decided.

    :param named: Resolved paths the user lists, as include (``+``), exclude (``-``)
        or plain entries. Any of them means the user decided that path.
    :param patterns: Resolved ``!`` glob exclusions the user wrote.
    """

    named: frozenset[Path]
    patterns: tuple[str, ...]

    def decides(self, path: Path, *, below: bool = False) -> bool:
        """Whether the user's entries decide *path* (a resolved path).

        A claim decides *path* when it names *path* or any ancestor of it, or when a
        glob matches *path* or an ancestor. With *below*, a named path inside *path*
        also decides it, so a directory the user partly excludes is not bridged whole.
        """
        lineage = (path, *path.parents)
        if any(candidate in self.named for candidate in lineage):
            return True
        if below and any(_is_within(named, path) for named in self.named):
            return True
        texts = [str(candidate) for candidate in lineage]
        return any(
            fnmatch.fnmatchcase(text, pattern) for text in texts for pattern in self.patterns
        )


def _entry_source(entry: object) -> str | None:
    """The source string of a Pi resource entry, which may be an object form."""
    source = entry.get("source") if isinstance(entry, dict) else entry
    return source if isinstance(source, str) and source else None


def _claims(entries: object) -> _Claims:
    """Claims from a user's resource array; a value that is not a list claims nothing."""
    if not isinstance(entries, list):
        return _Claims(frozenset(), ())
    named: set[Path] = set()
    patterns: list[str] = []
    for entry in entries:
        source = _entry_source(entry)
        if source is None:
            continue
        if source.startswith("!"):
            pattern = _resolve_path(source[1:], user_entry=True)
            if pattern is not None:
                patterns.append(str(pattern))
            continue
        listed = _resolve_path(source.lstrip("+-"), user_entry=True)
        if listed is not None:
            named.add(listed)
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
    directories, at :data:`_MAX_WALK_DEPTH` levels, or as soon as
    :data:`MAX_PLUGIN_COMMANDS` matches are collected, including within one directory.
    """
    found: list[_WalkedFile] = []
    queue: deque[tuple[tuple[str, ...], Path]] = deque([((), root)])
    visited = 0
    while queue and visited < _MAX_WALK_DIRS:
        relative, directory = queue.popleft()
        visited += 1
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            _logger.warning("Skipping unreadable Claude plugin dir %s: %s", directory, exc)
            continue
        for entry in entries:
            if len(found) >= MAX_PLUGIN_COMMANDS:
                return found
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
    base = _resolve_path(root)
    resolved = _resolve_path(source)
    if base is None or resolved is None or not _is_within(resolved, base):
        return False
    if claims.decides(resolved):
        return False
    try:
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

    Commands the user's ``prompts`` setting decides are not written. Files are read
    only when they resolve inside the plugin root, so a symlink cannot expose content
    from elsewhere on the host.

    :returns: ``True`` when at least one prompt was written.
    """
    commands_dir = root / "commands"
    if commands_dir.is_symlink() or not commands_dir.is_dir():
        return False
    base = _resolve_path(root)
    resolved_dir = _resolve_path(commands_dir)
    if base is None or resolved_dir is None or not _is_within(resolved_dir, base):
        _logger.warning("Skipping Claude plugin %s: commands/ links outside the plugin", name)
        return False
    written = False
    for relative, source in _bounded_files(commands_dir, lambda n: n.endswith(".md")):
        parts = (*relative, source.stem)
        written |= _write_command(name, root, parts, source, prompts_dir, claims)
    return written


def _skill_dirs(skills_dir: Path, base: Path, claims: _Claims) -> list[str]:
    """Skill directories (those holding ``SKILL.md``) the user's settings do not decide.

    Each skill directory is added on its own. A skill is skipped when the user decides
    the directory, an enclosing directory, or anything inside it, because Pi's handling
    of an exclusion inside a directory entry is not relied on.
    """
    if skills_dir.is_symlink() or not skills_dir.is_dir():
        return []
    resolved_dir = _resolve_path(skills_dir)
    if resolved_dir is None or not _is_within(resolved_dir, base):
        return []
    directories: list[str] = []
    for _, skill_md in _bounded_files(skills_dir, lambda n: n == "SKILL.md"):
        skill_dir = _resolve_path(skill_md.parent)
        if skill_dir is None or not _is_within(skill_dir, base):
            continue
        if claims.decides(skill_dir, below=True):
            continue
        contents = (_resolve_path(f) for _, f in _bounded_files(skill_md.parent, lambda _: True))
        if any(claims.decides(path) for path in contents if path is not None):
            continue
        directories.append(str(skill_dir))
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
    seen: set[Path] = set()
    for addition in additions:
        path = _resolve_path(addition)
        if path is None or path in seen or claims.decides(path, below=True):
            continue
        seen.add(path)
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
            resolved_root = _resolve_path(root)
            if resolved_root is None:
                continue
            if declares_pi:
                if not claims["packages"].decides(resolved_root, below=True):
                    packages.append(str(resolved_root))
            elif not claims["packages"].decides(resolved_root, below=True):
                # A user package entry for this plugin decides its skills, including none.
                skills.extend(_skill_dirs(root / "skills", resolved_root, claims["skills"]))
            if os.name != "nt":
                # ``:`` in the command name is not a legal filename on Windows.
                has_commands |= _write_commands(
                    name, resolved_root, generated / "prompts", claims["prompts"]
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
