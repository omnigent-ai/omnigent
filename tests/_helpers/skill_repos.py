"""Fixture skill repositories for marketplace tests: explicit inputs, no git, no network."""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

from omnigent.skills.git_source import SparseCheckout
from omnigent.skills.limits import DEFAULT_CLONE_LIMITS, CloneLimits


def write_skill(
    skills_dir: Path, name: str, description: str, *, label: str | None = None
) -> Path:
    """Create ``<skills_dir>/<name>/SKILL.md`` plus one supporting file; returns the directory.

    :param label: Frontmatter ``name`` when it should differ from the directory name.
    """
    skill = skills_dir / name
    skill.mkdir(parents=True)
    front = f"name: {label}\n" if label else f"name: {name}\n"
    (skill / "SKILL.md").write_text(f"---\n{front}description: {description}\n---\nDo {name}.\n")
    (skill / "notes.txt").write_text("supporting file\n")
    return skill


def skill_tarball(files: dict[str, bytes]) -> bytes:
    """A gzipped tar of *files* (``{"SKILL.md": b"..."}``), as the artifact store holds a skill."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return out.getvalue()


def copying_cloner(repo: Path):  # type: ignore[no-untyped-def]  # a Cloner
    """A cloner that copies *repo* into the destination instead of running git.

    The whole tree is copied whatever the sparse patterns, and a sparse
    request's ``expand`` is still called, as a real partial clone would. The
    returned callable records ``calls`` as ``(url, ref)`` pairs, ``tokens`` as
    the credential handed to each clone, ``sparse`` as each clone's patterns
    (``None`` for a full clone) and ``limits`` as its budgets.
    """
    calls: list[tuple[str, str | None]] = []
    tokens: list[str | None] = []
    sparse_patterns: list[tuple[str, ...] | None] = []
    budgets: list[CloneLimits] = []

    def clone(
        url: str,
        ref: str | None,
        dest: Path,
        *,
        token: str | None = None,
        sparse: SparseCheckout | None = None,
        limits: CloneLimits = DEFAULT_CLONE_LIMITS,
    ) -> None:
        calls.append((url, ref))
        tokens.append(token)
        budgets.append(limits)
        shutil.copytree(repo, dest, symlinks=True)
        if sparse is None:
            sparse_patterns.append(None)
            return
        extra = tuple(sparse.expand(dest)) if sparse.expand else ()
        sparse_patterns.append((*sparse.patterns, *extra))

    clone.calls = calls  # type: ignore[attr-defined]
    clone.tokens = tokens  # type: ignore[attr-defined]
    clone.sparse = sparse_patterns  # type: ignore[attr-defined]
    clone.limits = budgets  # type: ignore[attr-defined]
    return clone
