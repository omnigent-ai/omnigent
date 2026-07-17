"""Cross-harness skill discovery, identity, and reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from omnigent.spec.types import SkillSpec

LocationScope = Literal["bundle", "workspace", "personal"]
SkillTrust = Literal["current", "all-host"]

_LOCATION_RANK: dict[LocationScope, int] = {
    "bundle": 0,
    "workspace": 1,
    "personal": 2,
}
_SOURCE_KIND_RANK = {"bundled": 0, "generic": 1, "vendor": 2, "plugin": 3}


@dataclass(frozen=True)
class SkillCandidate:
    """One discovered skill definition with stable provenance."""

    provider: str
    location_scope: LocationScope
    source_kind: str
    origin_path: Path
    source_coords: str
    namespace: str
    invocation_name: str
    managed: bool
    tree_digest: str
    skill: SkillSpec

    @property
    def precedence_key(self) -> tuple[int, int, str, str, str]:
        """Return the deterministic winner ordering for same-name skills."""
        return (
            _LOCATION_RANK[self.location_scope],
            _SOURCE_KIND_RANK.get(self.source_kind, 99),
            self.provider,
            self.source_coords,
            self.origin_path.as_posix(),
        )


@dataclass(frozen=True)
class SkillEntry:
    """The selected definition and retained same-name alternatives."""

    winner: SkillCandidate
    canonical_id: str
    shadowed: tuple[SkillCandidate, ...] = ()

    @property
    def skill(self) -> SkillSpec:
        """Return the selected skill payload."""
        return self.winner.skill


def tree_digest(skill_dir: Path | None) -> str:
    """Hash the full skill tree using stable relative paths and bytes."""
    digest = hashlib.sha256()
    if skill_dir is None or not skill_dir.exists():
        return digest.hexdigest()
    root = skill_dir.resolve()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        if path.is_symlink():
            digest.update(b"L")
            digest.update(path.readlink().as_posix().encode())
        elif path.is_file():
            digest.update(b"F")
            digest.update(path.read_bytes())
        elif path.is_dir():
            digest.update(b"D")
    return digest.hexdigest()


def canonical_skill_id(candidate: SkillCandidate) -> str:
    """Build a stable registry id from canonical coordinates and alias."""
    encoded = json.dumps(
        [candidate.source_coords, candidate.invocation_name],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    suffix = hashlib.sha256(encoded).hexdigest()[:16]
    return f"{candidate.invocation_name}:{suffix}"


def display_path(candidate: SkillCandidate) -> str:
    """
    Return a concise, root-anchored source path for a skill candidate.

    This is the *user-facing* provenance shown in the catalog — the path a
    reader can actually locate, not the ambiguous ``Workspace`` / ``Personal``
    scope word. It is derived purely from ``source_coords`` (already root-aware:
    see ``omnigent.spec.skill_sources._source_coords``), so it stays stable and
    testable without touching the filesystem:

    - ``bundle:<agent>:<rel>``            → ``"Included with agent"``
    - ``<provider>:home:<rel>``           → ``"~/<rel>"``     (e.g. ``~/.codex/skills/foo``)
    - ``<provider>:workspace:<idx>:<rel>``→ ``"<rel>"``       (e.g. ``.claude/skills/foo``)
    - ``<provider>:path:<abs>``           → the absolute path (last-resort, unrooted)

    The absolute ``origin_path`` remains available for the detail's Advanced
    section; this helper is what the list + the detail headline render.
    """
    coords = candidate.source_coords
    if candidate.location_scope == "bundle":
        return "Included with agent"
    parts = coords.split(":")
    # parts[0] is the provider; the root label is parts[1] (with an extra index
    # segment for the workspace form).
    if len(parts) >= 2:
        root = parts[1]
        if root == "home":
            rel = ":".join(parts[2:])
            return f"~/{rel}" if rel else "~"
        if root == "workspace":
            # <provider>:workspace:<idx>:<rel> — the rel is everything past idx.
            return ":".join(parts[3:]) if len(parts) >= 4 else coords
        if root == "path":
            # Unrooted: the coords carry the absolute path verbatim.
            return ":".join(parts[2:]) if len(parts) >= 3 else coords
    # Unrecognized shape — fall back to the absolute path so we never render a
    # bare scope word as the source.
    return candidate.origin_path.as_posix()


class SkillRegistry:
    """Immutable-snapshot registry for one session or catalog request."""

    def __init__(self, candidates: Iterable[SkillCandidate] = ()) -> None:
        self._candidates = tuple(candidates)
        self._entries: tuple[SkillEntry, ...] | None = None

    @classmethod
    def from_candidates(
        cls,
        candidates: Iterable[SkillCandidate],
        *,
        active_provider: str | None,
        skill_trust: SkillTrust = "current",
        skills_filter: str | list[str] = "all",
    ) -> SkillRegistry:
        """Apply trust and host filters before deterministic precedence."""
        if skill_trust not in ("current", "all-host"):
            raise ValueError(f"unsupported skill_trust: {skill_trust!r}")
        allowed_names = set(skills_filter) if isinstance(skills_filter, list) else None
        filtered: list[SkillCandidate] = []
        for candidate in candidates:
            if candidate.location_scope != "bundle":
                if skill_trust == "current" and candidate.provider != active_provider:
                    continue
                if skills_filter == "none":
                    continue
                if allowed_names is not None and candidate.invocation_name not in allowed_names:
                    continue
            filtered.append(candidate)
        return cls(filtered)

    def reconcile(self) -> list[SkillEntry]:
        """Select one deterministic winner per invocation name."""
        if self._entries is None:
            by_name: dict[str, list[SkillCandidate]] = {}
            first_index: dict[str, int] = {}
            seen_identity: set[tuple[str, str]] = set()
            seen_dirs: set[Path] = set()
            for index, candidate in enumerate(self._candidates):
                identity = (candidate.source_coords, candidate.invocation_name)
                if identity in seen_identity:
                    continue
                skill_dir = candidate.skill.skill_dir
                if skill_dir is not None:
                    resolved_dir = skill_dir.resolve()
                    if resolved_dir in seen_dirs:
                        continue
                    seen_dirs.add(resolved_dir)
                seen_identity.add(identity)
                by_name.setdefault(candidate.invocation_name, []).append(candidate)
                first_index.setdefault(candidate.invocation_name, index)
            entries: list[SkillEntry] = []
            for name in sorted(by_name, key=lambda item: first_index[item]):
                ordered = sorted(by_name[name], key=lambda item: item.precedence_key)
                winner = ordered[0]
                entries.append(
                    SkillEntry(
                        winner=winner,
                        canonical_id=canonical_skill_id(winner),
                        shadowed=tuple(ordered[1:]),
                    )
                )
            self._entries = tuple(entries)
        return [entry for entry in self._entries if entry.winner.skill.user_invocable]

    def list(self) -> list[SkillEntry]:
        """Return reconciled entries."""
        return self.reconcile()

    def get_entry(self, canonical_id: str) -> SkillEntry | None:
        """Return a reconciled entry by stable id."""
        return next(
            (entry for entry in self.reconcile() if entry.canonical_id == canonical_id),
            None,
        )

    def get(self, canonical_id: str) -> SkillSpec | None:
        """Return a selected skill by stable id."""
        entry = self.get_entry(canonical_id)
        return entry.skill if entry is not None else None

    def get_by_name(self, name: str) -> SkillSpec | None:
        """Return the selected skill by invocation name."""
        entry = next(
            (item for item in self.reconcile() if item.winner.invocation_name == name),
            None,
        )
        return entry.skill if entry is not None else None

    def skills(self) -> list[SkillSpec]:
        """Return selected skills in deterministic name order."""
        return [entry.skill for entry in self.reconcile()]


def write_registry_manifest(path: Path, registry: SkillRegistry) -> None:
    """Atomically serialize selected skill payloads for relay subprocesses."""
    payload = {
        entry.winner.invocation_name: {
            "id": entry.canonical_id,
            "description": entry.winner.skill.description,
            "content": entry.winner.skill.content,
            "digest": entry.winner.tree_digest,
        }
        for entry in registry.list()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_registry_manifest(path: Path) -> dict[str, dict[str, str]]:
    """Read a registry manifest, returning an empty mapping on bad input."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        name: value
        for name, value in payload.items()
        if isinstance(name, str) and isinstance(value, dict)
    }
