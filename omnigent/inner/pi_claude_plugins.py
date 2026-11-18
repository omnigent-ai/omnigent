"""Expose enabled Claude Code plugins to managed Pi agent dirs.

Pi loads skills, packages and prompt templates from its settings, so the managed
``settings.json`` gains entries for the plugins Claude Code has enabled. Commands
become generated prompt templates because Claude's ``${CLAUDE_PLUGIN_ROOT}`` must
be substituted. Entries are only appended; a path the user already names, or
excludes, is left alone.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
from collections.abc import Iterable, Mapping
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

# Directory under the managed agent dir holding generated command prompts.
PI_CLAUDE_PLUGIN_RESOURCES_DIR = "claude-plugins"
_PLUGIN_ROOT_VARIABLE = "${CLAUDE_PLUGIN_ROOT}"
MAX_PLUGIN_COMMANDS = 512
_MAX_COMMAND_DEPTH = 8
# A plugin name becomes part of a generated filename, so it must be one safe component.
_PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _pi_manifest(root: Path) -> Mapping[str, object] | None:
    """Return the ``pi`` object of the plugin's ``package.json``, when it declares one."""
    manifest = _read_json(root / "package.json")
    pi = manifest.get("pi") if manifest else None
    return pi if isinstance(pi, dict) else None


def _enabled_plugins() -> list[tuple[str, Path, bool]]:
    """Enabled user-scope Claude plugins as ``(name, install root, declares Pi)``."""
    ctx = replace(
        skill_source_context_from_env(roots=(), harness="claude-native"),
        is_native=True,
    )
    plugins: list[tuple[str, Path, bool]] = []
    for key, root in _plugin_install_paths(ctx, _enabled_plugin_keys(ctx)).items():
        plugins.append((key.split("@", 1)[0], root, _pi_manifest(root) is not None))
    return plugins


def _command_files(
    directory: Path, relative: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Path]]:
    """Markdown files under *directory* as ``(path parts, file)``, skipping symlinks."""
    if len(relative) > _MAX_COMMAND_DEPTH:
        return []
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        _logger.warning("Skipping unreadable Claude commands dir %s: %s", directory, exc)
        return []
    found: list[tuple[tuple[str, ...], Path]] = []
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            found.extend(_command_files(entry, (*relative, entry.name)))
        elif entry.is_file() and entry.name.endswith(".md"):
            found.append(((*relative, entry.stem), entry))
    return found


def _write_commands(name: str, root: Path, prompts_dir: Path) -> bool:
    """Write the plugin's ``commands/**/*.md`` as ``<name>:<command>.md`` prompt templates.

    Files are read only when they resolve inside the plugin root, so a symlink cannot
    expose content from elsewhere on the host.

    :returns: ``True`` when at least one prompt was written.
    """
    commands_dir = root / "commands"
    if commands_dir.is_symlink() or not commands_dir.is_dir():
        return False
    base = root.resolve()
    if not commands_dir.resolve().is_relative_to(base):
        _logger.warning("Skipping Claude plugin %s: commands/ links outside the plugin", name)
        return False
    written = False
    for parts, source in _command_files(commands_dir)[:MAX_PLUGIN_COMMANDS]:
        if not source.resolve().is_relative_to(base):
            continue
        target = prompts_dir / f"{':'.join((name, *parts))}.md"
        if target.exists():
            # Another marketplace's plugin with the same name already claimed this command.
            _logger.info("Skipping Claude command %s: name already provided", target.name)
            continue
        try:
            body = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _logger.warning("Skipping Claude command %s: %s", source, exc)
            continue
        prompts_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(body.replace(_PLUGIN_ROOT_VARIABLE, str(root)), encoding="utf-8")
        written = True
    return written


def _entry_source(entry: object) -> str | None:
    """The source string of a Pi resource entry, which may be an object form."""
    source = entry.get("source") if isinstance(entry, dict) else entry
    return source if isinstance(source, str) and source else None


def _user_claims(entries: list[object]) -> tuple[set[str], list[str]]:
    """Paths the user names (include or exclude) and the glob exclusions they wrote."""
    named: set[str] = set()
    patterns: list[str] = []
    for entry in entries:
        source = _entry_source(entry)
        if source is None:
            continue
        if source[0] == "!":
            patterns.append(source[1:])
        else:
            named.add(_resolved(source.lstrip("+-")))
    return named, patterns


def _resolved(path: str) -> str:
    return str(Path(path).expanduser().resolve())


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
    named, patterns = _user_claims(current)
    for addition in additions:
        resolved = _resolved(addition)
        if resolved in named or any(fnmatch.fnmatchcase(resolved, p) for p in patterns):
            continue
        named.add(resolved)
        current.append(addition)
    return current


def add_claude_plugin_resources(settings: _Settings, managed_dir: Path) -> _Settings:
    """Add enabled Claude plugin packages, skills and commands to Pi *settings*.

    Plugins whose ``package.json`` declares a ``pi`` manifest load through Pi's
    package mechanism, which also governs their skills. Other plugins contribute
    their ``skills/`` directory. Commands are written under *managed_dir* and
    regenerated on every call, so disabled or removed plugins leave nothing behind.

    :param settings: The managed settings after user relocation, before overlays.
    :param managed_dir: The managed agent dir the settings are written into.
    :returns: ``settings`` with the plugin entries appended.
    """
    generated = managed_dir / PI_CLAUDE_PLUGIN_RESOURCES_DIR
    shutil.rmtree(generated, ignore_errors=True)
    if not resolve_feature_flags().enabled(Feature.PI_CLAUDE_PLUGINS):
        return settings
    packages: list[str] = []
    skills: list[str] = []
    has_commands = False
    user_packages = settings.get("packages")
    package_named, _ = _user_claims(user_packages if isinstance(user_packages, list) else [])
    for name, root, declares_pi in _enabled_plugins():
        if not _PLUGIN_NAME.fullmatch(name):
            _logger.warning("Not bridging Claude plugin with unsafe name %r", name)
            continue
        if declares_pi:
            packages.append(str(root))
        elif (root / "skills").is_dir() and _resolved(str(root)) not in package_named:
            # A user package entry for this plugin decides its skills, including none.
            skills.append(str(root / "skills"))
        if os.name != "nt":
            # ``:`` in the command name is not a legal filename on Windows.
            has_commands |= _write_commands(name, root, generated / "prompts")
        elif (root / "commands").is_dir():
            _logger.info("Not bridging Claude plugin commands on Windows: %s", name)
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
