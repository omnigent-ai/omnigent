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


def _inventory_failure(exc: Exception) -> tuple[str, str]:
    """Summarize a failed ``codex plugin list`` for the menu and keep its output for the log.

    Catalog warnings reach session editors, so the summary names only the
    command and how it failed; stderr and exception text can carry host paths
    and stay in the host log.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        return f"`codex plugin list` exited {exc.returncode}", " ".join(
            str(exc.stderr or "").split()
        )
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"`codex plugin list` timed out after {exc.timeout:g}s", ""
    if isinstance(exc, ValueError):
        return "`codex plugin list` returned unreadable output", str(exc)
    return "`codex plugin list` could not be run", str(exc)


def _without_inventory(summary: str, detail: str, warnings: list[str] | None) -> list[SkillSpec]:
    """Omit plugin skills, telling the host log and the caller's menu why."""
    message = f"Codex plugin skills unavailable: {summary}"
    if detail:
        _log.warning("%s: %s", message, detail)
    else:
        _log.warning("%s", message)
    if warnings is not None:
        warnings.append(f"{message}; see the host log for details." if detail else f"{message}.")
    return []


def discover_codex_plugin_skills(
    codex_home: Path,
    skills_filter: str | list[str],
    *,
    cwd: Path | None = None,
    warnings: list[str] | None = None,
) -> list[SkillSpec]:
    """Ask Codex which plugin versions are installed, then parse their skills.

    When the inventory cannot be read, plugin skills are omitted and, if
    ``warnings`` is given, a user-facing explanation is appended to it so
    the caller can show the catalog as incomplete rather than empty.
    """
    from omnigent.inner.codex_executor import _find_codex_cli

    if skills_filter == "none" or skills_filter == []:
        return []
    cache = codex_home / "plugins" / "cache"
    if not cache.is_dir():
        return []
    if (codex_path := _find_codex_cli()) is None:
        return _without_inventory("`codex` was not found on the host", "", warnings)
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
        return _without_inventory(*_inventory_failure(exc), warnings)
    plugins = inventory.get("installed") if isinstance(inventory, dict) else None
    if not isinstance(plugins, list):
        return _without_inventory(
            "`codex plugin list` returned an unexpected inventory", "", warnings
        )
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
                # Codex keys plugin skills by frontmatter name, not directory.
                base = spec.display_name or spec.name
                name = f"{namespace}:{base}"
                if filter_names is None or base in filter_names or name in filter_names:
                    out.append(replace(spec, name=name, display_name=None))
            for detail in skipped:
                _log.warning("Plugin %r: skipped skill: %s", namespace, detail)
    return out
