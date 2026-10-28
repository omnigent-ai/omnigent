"""Index a fetched repository into the skills it offers.

Two layouts are understood: a plain skills repo (``skills/``, ``.agents/skills``
or ``.claude/skills`` at the root) and a Claude Code marketplace
(``.claude-plugin/marketplace.json`` listing plugins, each with its own
``skills/``). Only plugins whose source is a path inside the checkout are
indexed; a plugin that points at another repository is reported, not followed.

The checkout is untrusted: nothing here follows a symlink, every path is
checked to stay inside the checkout, and a ``SKILL.md`` is parsed only when it
is a regular file of bounded size.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from omnigent.skills.limits import (
    MAX_CATALOG_ENTRIES,
    MAX_DESCRIPTION_CHARS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_MANIFEST_BYTES,
    MAX_NAME_CHARS,
    MAX_PATH_CHARS,
    MAX_SKILL_MD_BYTES,
)
from omnigent.spec.parser import _parse_skill

MARKETPLACE_MANIFEST = ".claude-plugin/marketplace.json"
SKILL_ROOTS = ("skills", ".agents/skills", ".claude/skills")
# Portable command names become directory names in the launch overlay.
_NAME_RE = re.compile(rf"[A-Za-z0-9_-]{{1,{MAX_NAME_CHARS}}}")


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


def _inside(root: Path, candidate: Path) -> bool:
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _regular_file(path: Path, limit: int) -> bool:
    """A real file under *limit* bytes; never a link, a device or a FIFO."""
    try:
        st = path.lstat()
    except OSError:
        return False
    import stat as _stat

    return _stat.S_ISREG(st.st_mode) and st.st_size <= limit


def _subdirs(directory: Path) -> list[Path]:
    """Real subdirectories of *directory*, in name order; links are not followed."""
    try:
        with os.scandir(directory) as it:
            return sorted(
                (Path(entry.path) for entry in it if entry.is_dir(follow_symlinks=False)),
                key=lambda p: p.name,
            )
    except OSError:
        return []


def _candidate_skill_dirs(skills_dir: Path, root: Path, skipped: list[str]) -> list[Path]:
    """``skills/<name>`` and ``skills/<namespace>/<name>`` directories holding a SKILL.md."""
    if skills_dir.is_symlink() or not skills_dir.is_dir() or not _inside(root, skills_dir):
        if skills_dir.is_symlink():
            skipped.append(f"{skills_dir.name}: skills directory is a link; not followed")
        return []
    out: list[Path] = []
    for child in _subdirs(skills_dir):
        if (child / "SKILL.md").exists() or (child / "SKILL.md").is_symlink():
            out.append(child)
            continue
        out.extend(
            grandchild for grandchild in _subdirs(child) if (grandchild / "SKILL.md").exists()
        )
    return out


def _plugin_dirs(root: Path, skipped: list[str]) -> list[tuple[str, Path]]:
    """Marketplace plugins whose source is a directory inside *root*."""
    manifest = root / MARKETPLACE_MANIFEST
    if not _regular_file(manifest, MAX_MANIFEST_BYTES):
        if manifest.is_symlink():
            skipped.append(f"{MARKETPLACE_MANIFEST}: is a link; not followed")
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
        candidate = root / source
        if candidate.is_symlink() or not candidate.is_dir() or not _inside(root, candidate):
            skipped.append(f"plugin {name!r}: source {source!r} is not a directory in the repo")
            continue
        out.append((name, candidate))
    return out


class _Budget:
    """How many candidate manifests may still be opened; a hostile tree stops here."""

    def __init__(self, candidates: int) -> None:
        self.remaining = candidates
        self.exhausted = False

    def take(self) -> bool:
        if self.remaining <= 0:
            self.exhausted = True
            return False
        self.remaining -= 1
        return True


def _entries_under(
    skills_dir: Path, root: Path, plugin: str | None, skipped: list[str], budget: _Budget
) -> Iterator[CatalogEntry]:
    """Yield entries lazily; nothing is read once *budget* runs out."""
    for skill_dir in _candidate_skill_dirs(skills_dir, root, skipped):
        if not budget.take():
            return
        rel = skill_dir.relative_to(root).as_posix()
        manifest = skill_dir / "SKILL.md"
        if manifest.is_symlink() or not _inside(root, manifest):
            skipped.append(f"{rel}: SKILL.md is a link; not followed")
            continue
        if not _regular_file(manifest, MAX_SKILL_MD_BYTES):
            skipped.append(f"{rel}: SKILL.md is missing, not a regular file, or too large")
            continue
        try:
            spec = _parse_skill(manifest)
        except Exception as exc:  # noqa: BLE001 — a bad SKILL.md drops one entry, not the catalog
            skipped.append(f"{rel}: {exc}")
            continue
        if not _NAME_RE.fullmatch(spec.name):
            skipped.append(f"{rel}: invalid command name")
            continue
        if len(spec.description) > MAX_DESCRIPTION_CHARS or len(rel) > MAX_PATH_CHARS:
            skipped.append(f"{rel}: description or path is too long")
            continue
        display_name = spec.display_name
        if display_name is not None and len(display_name) > MAX_DISPLAY_NAME_CHARS:
            skipped.append(f"{rel}: display name is too long")
            continue
        yield CatalogEntry(
            name=spec.name,
            display_name=display_name,
            description=spec.description,
            path=str(PurePosixPath(rel)),
            plugin=plugin,
        )


def index_checkout(root: Path, *, skipped: list[str] | None = None) -> list[CatalogEntry]:
    """Every skill under *root*, root-level layouts first, then marketplace plugins.

    The first occurrence of a name wins, so a root skill shadows a plugin's
    skill of the same name. Problems that drop an entry are appended to
    *skipped* as human-readable lines. At most :data:`MAX_CATALOG_ENTRIES`
    manifests are opened, so a repository of ten thousand skills costs the
    same as one of five hundred.
    """
    notes: list[str] = [] if skipped is None else skipped
    seen: set[str] = set()
    out: list[CatalogEntry] = []
    budget = _Budget(MAX_CATALOG_ENTRIES)

    def add(entries: Iterator[CatalogEntry]) -> None:
        for entry in entries:
            if entry.name in seen:
                continue
            seen.add(entry.name)
            out.append(entry)

    for rel in SKILL_ROOTS:
        add(_entries_under(root / rel, root, None, notes, budget))
    if not budget.exhausted:
        for plugin, plugin_dir in _plugin_dirs(root, notes):
            add(_entries_under(plugin_dir / "skills", root, plugin, notes, budget))
    if budget.exhausted:
        notes.append(f"catalog truncated at {MAX_CATALOG_ENTRIES} skills")
    return out
