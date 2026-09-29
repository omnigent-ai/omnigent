"""Discover plugin skills from Codex's installed-plugin inventory."""

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
    codex_home: Path, skills_filter: str | list[str], *, cwd: Path | None = None
) -> list[SkillSpec]:
    """Ask Codex which plugin versions are installed, then parse their skills."""
    from omnigent.inner.codex_executor import _find_codex_cli

    if skills_filter == "none" or skills_filter == []:
        return []
    cache = codex_home / "plugins" / "cache"
    if not cache.is_dir() or (codex_path := _find_codex_cli()) is None:
        return []
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
        _log.debug("Codex plugin inventory unavailable: %s", exc)
        return []
    plugins = inventory.get("installed") if isinstance(inventory, dict) else None
    if not isinstance(plugins, list):
        return []
    filter_names = set(skills_filter) if isinstance(skills_filter, list) else None
    out: list[SkillSpec] = []
    for plugin in plugins:
        if not isinstance(plugin, dict) or plugin.get("enabled") is not True:
            continue
        parts = [plugin.get(key) for key in ("marketplaceName", "name", "version")]
        if not all(
            isinstance(part, str) and part not in {"", ".", ".."} and Path(part).name == part
            for part in parts
        ):
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
                name = f"{namespace}:{spec.name}"
                if filter_names is None or spec.name in filter_names or name in filter_names:
                    out.append(replace(spec, name=name))
            for detail in skipped:
                _log.warning("Plugin %r: skipped skill: %s", namespace, detail)
    return out
