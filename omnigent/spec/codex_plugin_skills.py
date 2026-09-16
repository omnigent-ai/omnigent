"""Discover enabled Codex plugins without loading every cached version."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from functools import cmp_to_key
from pathlib import Path
from typing import TypeAlias

import tomllib
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import _discover_skills, _parse_skill
from omnigent.spec.types import SkillSpec

_log = logging.getLogger(__name__)
_CACHE_SEGMENT = re.compile(r"[A-Za-z0-9._+-]+")
_SEMVER = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)
_Identifiers: TypeAlias = tuple[tuple[bool, int, str], ...]
_VersionKey: TypeAlias = tuple[tuple[int, ...], bool, _Identifiers, _Identifiers]


def _version_key(version: str) -> _VersionKey | None:
    """Parse SemVer, keeping arbitrary prerelease identifiers and build metadata."""
    match = _SEMVER.fullmatch(version)
    if match is None:
        return None
    prerelease = match[4] or ""
    if any(p.isdigit() and len(p) > 1 and p.startswith("0") for p in prerelease.split(".")):
        return None

    def identifiers(value: str) -> _Identifiers:
        return tuple(
            (False, int(part), "") if part.isdigit() else (True, 0, part)
            for part in value.split(".")
            if part
        )

    return (
        tuple(int(match[i]) for i in (1, 2, 3)),
        not prerelease,
        identifiers(prerelease),
        identifiers(match[5] or ""),
    )


def _compare_versions(left: Path, right: Path) -> int:
    """Match Codex's SemVer comparison, falling back to lexical version order."""
    left_key, right_key = _version_key(left.name), _version_key(right.name)
    if left_key is not None and right_key is not None:
        return (left_key > right_key) - (left_key < right_key)
    return (left.name > right.name) - (left.name < right.name)


def _active_plugin_root(root: Path) -> Path | None:
    """Codex's plugin store prefers ``local``, otherwise the greatest version."""
    try:
        versions = [
            child
            for child in root.iterdir()
            if _CACHE_SEGMENT.fullmatch(child.name) and not child.is_symlink() and child.is_dir()
        ]
    except OSError:
        return None
    local = root / "local"
    if local in versions:
        return local
    return max(versions, key=cmp_to_key(_compare_versions), default=None)


def _skill_roots(root: Path, paths: object) -> list[Path]:
    """Resolve manifest skill paths and Codex's migrated command skills."""
    if isinstance(paths, str):
        paths = [paths]
    roots: set[Path] = set()
    if isinstance(paths, list):
        for path in paths:
            if not isinstance(path, str) or not path.startswith("./") or path == "./":
                continue
            relative = Path(path[2:])
            if relative.is_absolute() or ".." in relative.parts:
                continue
            resolved = (root / relative).resolve()
            if resolved.is_relative_to(root.resolve()):
                roots.add(resolved)
    if not roots:
        roots.add(root / "skills")
    roots.add(root / ".codex-plugin" / "migrated-command-skills")
    return sorted(roots)


def discover_codex_plugin_skills(
    codex_home: Path, skills_filter: str | list[str]
) -> list[SkillSpec]:
    """Read configured plugins from the same Codex home used by the session."""
    if skills_filter == "none" or skills_filter == []:
        return []
    try:
        config = tomllib.loads((codex_home / "config.toml").read_text())
    except (OSError, ValueError):
        return []
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return []
    filter_names = set(skills_filter) if isinstance(skills_filter, list) else None
    out: list[SkillSpec] = []
    for key, settings in plugins.items():
        if not isinstance(settings, dict) or settings.get("enabled", True) is not True:
            continue
        parts = key.split("@")
        if len(parts) != 2 or any(
            part in {".", ".."} or not _CACHE_SEGMENT.fullmatch(part) for part in parts
        ):
            continue
        plugin, marketplace = parts
        root = _active_plugin_root(codex_home / "plugins" / "cache" / marketplace / plugin)
        if root is None:
            continue
        manifest_path = root / ".codex-plugin" / "plugin.json"
        if not manifest_path.is_file():
            manifest_path = root / ".claude-plugin" / "plugin.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue
        namespace = manifest.get("name") if isinstance(manifest, dict) else None
        if not isinstance(namespace, str) or not namespace:
            continue
        for skills_dir in _skill_roots(root, manifest.get("skills")):
            skipped: list[str] = []
            try:
                specs = (
                    [_parse_skill(skills_dir / "SKILL.md")]
                    if (skills_dir / "SKILL.md").is_file()
                    else _discover_skills(skills_dir, skipped=skipped)
                )
            except (OmnigentError, OSError, yaml.YAMLError) as exc:
                _log.warning("Plugin %r: could not read skills under %s: %s", key, skills_dir, exc)
                continue
            for spec in specs:
                name = f"{namespace}:{spec.name}"
                if filter_names is None or spec.name in filter_names or name in filter_names:
                    out.append(replace(spec, name=name))
            for detail in skipped:
                _log.warning("Plugin %r: skipped skill: %s", key, detail)
    return out
