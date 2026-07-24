"""Safe per-session native projection of reconciled skill winners."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import tempfile
from pathlib import Path

from omnigent.spec.skill_registry import SkillEntry

_MANIFEST = ".omnigent-skill-manifest.json"


def projection_root_for_harness(harness: str, session_dir: Path) -> Path:
    """Return the Omnigent-owned projection root for a harness."""
    if harness == "codex-native":
        return session_dir / "codex-home" / "skills"
    if harness == "claude-native":
        return session_dir / "claude-plugin" / "skills"
    return session_dir / "skill-projection" / harness


def _desired_manifest(entries: list[SkillEntry]) -> dict[str, dict[str, str]]:
    desired: dict[str, dict[str, str]] = {}
    for entry in entries:
        skill_dir = entry.winner.skill.skill_dir
        if skill_dir is None:
            continue
        desired[entry.winner.invocation_name] = {
            "source": str(skill_dir.resolve()),
            "digest": entry.winner.tree_digest,
            "id": entry.canonical_id,
        }
    return desired


def materialize_for_harness(
    entries: list[SkillEntry],
    harness: str,
    session_dir: Path,
) -> bool:
    """Atomically reconcile native skill links; return whether state changed."""
    root = projection_root_for_harness(harness, session_dir)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = root.parent / f".{root.name}.lock"
    desired = _desired_manifest(entries)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        current: dict[str, dict[str, str]] = {}
        manifest_path = root / _MANIFEST
        with contextlib.suppress(OSError, ValueError, TypeError):
            loaded = json.loads(manifest_path.read_text())
            if isinstance(loaded, dict):
                current = loaded
        if current == desired and root.is_dir():
            return False

        temp = Path(tempfile.mkdtemp(prefix=f".{root.name}.", dir=root.parent))
        backup = root.parent / f".{root.name}.old-{os.getpid()}"
        try:
            for name, item in sorted(desired.items()):
                os.symlink(item["source"], temp / name, target_is_directory=True)
            (temp / _MANIFEST).write_text(json.dumps(desired, indent=2, sort_keys=True) + "\n")
            if backup.exists():
                shutil.rmtree(backup)
            if root.exists():
                os.replace(root, backup)
            os.replace(temp, root)
            shutil.rmtree(backup, ignore_errors=True)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
            if backup.exists() and not root.exists():
                os.replace(backup, root)
        return True


def cleanup_projection(harness: str, session_dir: Path) -> None:
    """Remove only the Omnigent-owned projection for a deleted session."""
    shutil.rmtree(projection_root_for_harness(harness, session_dir), ignore_errors=True)
