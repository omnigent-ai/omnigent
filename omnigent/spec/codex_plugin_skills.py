"""Discover plugin skills from Codex's inventory or loaded-skill catalog."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import cast

import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import _discover_skills, _parse_skill
from omnigent.spec.types import SkillSpec

_log = logging.getLogger(__name__)


def _is_path_segment(part: object) -> bool:
    """Whether *part* names exactly one directory level under the plugin cache."""
    return isinstance(part, str) and part not in {"", ".", ".."} and Path(part).name == part


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


def _inventory_plugins(codex_home: Path, cwd: Path | None) -> list[object] | None:
    """Return the installed entries of ``codex plugin list --json``, or ``None`` if unreadable."""
    from omnigent.inner.codex_executor import _find_codex_cli

    if (codex_path := _find_codex_cli()) is None:
        _log.debug("Codex plugin inventory unavailable: codex CLI not found")
        return None
    try:
        result = subprocess.run(
            [codex_path, "plugin", "list", "--json"],
            cwd=cwd or codex_home,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        inventory = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _log.info("Codex plugin inventory unavailable: %s", exc)
        return None
    plugins = inventory.get("installed") if isinstance(inventory, dict) else None
    return plugins if isinstance(plugins, list) else []


def _loaded_plugin_skills(
    codex_home: Path, skills_filter: str | list[str], cwd: Path | None
) -> list[SkillSpec]:
    """Use Codex's effective configuration and version selection when inventory fails."""
    from omnigent.inner.codex_executor import _find_codex_cli

    if (codex_path := _find_codex_cli()) is None:
        return []
    from omnigent.harnesses.codex_native.skill_discovery import loaded_codex_skills

    try:
        skills = loaded_codex_skills(codex_path, codex_home, cwd or codex_home)
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        _log.info("Codex loaded skills unavailable: %s", exc)
        return []
    cache = (codex_home / "plugins" / "cache").resolve()
    filter_names = set(skills_filter) if isinstance(skills_filter, list) else None
    out: list[SkillSpec] = []
    for skill in skills:
        name, path = skill.get("name"), skill.get("path")
        if skill.get("enabled") is not True or not isinstance(name, str):
            continue
        if not isinstance(path, str) or not Path(path).resolve().is_relative_to(cache):
            continue
        if (
            filter_names is not None
            and name not in filter_names
            and name.partition(":")[2] not in filter_names
        ):
            continue
        try:
            spec = _parse_skill(Path(path))
        except (OmnigentError, OSError, yaml.YAMLError) as exc:
            _log.warning("Could not read loaded Codex skill %r: %s", name, exc)
            continue
        out.append(replace(spec, name=name, display_name=None))
    return out


def discover_codex_plugin_skills(
    codex_home: Path, skills_filter: str | list[str], *, cwd: Path | None = None
) -> list[SkillSpec]:
    """Ask Codex which plugin versions are installed, then parse their skills."""
    if skills_filter == "none" or skills_filter == []:
        return []
    cache = codex_home / "plugins" / "cache"
    if not cache.is_dir():
        return []
    plugins = _inventory_plugins(codex_home, cwd)
    if plugins is None:
        return _loaded_plugin_skills(codex_home, skills_filter, cwd)
    filter_names = set(skills_filter) if isinstance(skills_filter, list) else None
    out: list[SkillSpec] = []
    for plugin in plugins:
        if not isinstance(plugin, dict) or plugin.get("enabled") is not True:
            continue
        parts = [plugin.get(key) for key in ("marketplaceName", "name", "version")]
        if not all(_is_path_segment(part) for part in parts):
            continue
        root = cache.joinpath(*cast(list[str], parts))
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
                _log.warning(
                    "Plugin %r: could not read skills under %s: %s", namespace, skills_dir, exc
                )
                continue
            for spec in specs:
                # Codex keys plugin skills by frontmatter name, not directory.
                base = spec.display_name or spec.name
                name = f"{namespace}:{base}"
                if filter_names is None or base in filter_names or name in filter_names:
                    out.append(replace(spec, name=name, display_name=None))
            for detail in skipped:
                _log.warning("Plugin %r: skipped skill: %s", namespace, detail)
    return out
