"""Index a fetched repository into the skills it offers.

Two layouts are understood: a plain skills repo (``skills/``, ``.agents/skills``
or ``.claude/skills`` at the root) and a Claude Code marketplace
(``.claude-plugin/marketplace.json`` listing plugins, each with its own
``skills/``). Only plugins whose source is a path inside the checkout are
indexed; a plugin that points at another repository is reported, not followed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from omnigent.spec.parser import _discover_skills

MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"
SKILL_ROOTS = ("skills", ".agents/skills", ".claude/skills")
MAX_CATALOG_ENTRIES = 500
# Portable command names become directory names in the launch overlay.
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,255}")


@dataclass(frozen=True)
class CatalogEntry:
    """One skill a repository offers.

    :param name: Invocation identifier, the skill's directory name.
    :param display_name: Human-facing label from the frontmatter, or ``None``.
    :param description: One-line summary from the frontmatter.
    :param path: The skill directory, relative to the checkout root, POSIX form.
    :param plugin: The marketplace plugin that carries it, or ``None`` for a
        root-level skill.
    """

    name: str
    display_name: str | None
    description: str
    path: str
    plugin: str | None = None


def _plugin_dirs(root: Path, skipped: list[str]) -> list[tuple[str, Path]]:
    """Marketplace plugins whose source is a directory inside *root*."""
    manifest = root / MARKETPLACE_MANIFEST
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        skipped.append(f"{MARKETPLACE_MANIFEST}: unreadable ({exc})")
        return []
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        skipped.append(f"{MARKETPLACE_MANIFEST}: no plugins list")
        return []
    out: list[tuple[str, Path]] = []
    resolved_root = root.resolve()
    for item in plugins:
        if not isinstance(item, dict):
            continue
        name, source = item.get("name"), item.get("source")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            skipped.append("marketplace plugin with a missing or invalid name")
            continue
        if not isinstance(source, str):
            skipped.append(f"plugin {name!r}: source is another repository; not followed")
            continue
        candidate = (root / source).resolve()
        if not candidate.is_relative_to(resolved_root) or not candidate.is_dir():
            skipped.append(f"plugin {name!r}: source {source!r} is not a directory in the repo")
            continue
        out.append((name, candidate))
    return out


def _entries_under(
    skills_dir: Path, root: Path, plugin: str | None, skipped: list[str]
) -> list[CatalogEntry]:
    out: list[CatalogEntry] = []
    for spec in _discover_skills(skills_dir, skipped=skipped):
        if spec.skill_dir is None or spec.skill_dir.is_symlink():
            continue
        if not _NAME_RE.fullmatch(spec.name):
            skipped.append(f"skill with an invalid command name: {spec.skill_dir}")
            continue
        rel = PurePosixPath(spec.skill_dir.resolve().relative_to(root.resolve()).as_posix())
        out.append(
            CatalogEntry(
                name=spec.name,
                display_name=spec.display_name,
                description=spec.description,
                path=str(rel),
                plugin=plugin,
            )
        )
    return out


def index_checkout(root: Path, *, skipped: list[str] | None = None) -> list[CatalogEntry]:
    """Every skill under *root*, root-level layouts first, then marketplace plugins.

    The first occurrence of a name wins, so a root skill shadows a plugin's
    skill of the same name. Problems that drop an entry are appended to
    *skipped* as human-readable lines.
    """
    notes: list[str] = [] if skipped is None else skipped
    seen: set[str] = set()
    out: list[CatalogEntry] = []

    def add(entries: list[CatalogEntry]) -> None:
        for entry in entries:
            if entry.name in seen:
                continue
            if len(out) >= MAX_CATALOG_ENTRIES:
                notes.append(f"catalog truncated at {MAX_CATALOG_ENTRIES} skills")
                return
            seen.add(entry.name)
            out.append(entry)

    for rel in SKILL_ROOTS:
        add(_entries_under(root / rel, root, None, notes))
    for plugin, plugin_dir in _plugin_dirs(root, notes):
        add(_entries_under(plugin_dir / "skills", root, plugin, notes))
    return out


def skill_markdown(skill_dir: Path) -> str:
    """The skill's ``SKILL.md`` text."""
    return (skill_dir / "SKILL.md").read_text(encoding="utf-8")
