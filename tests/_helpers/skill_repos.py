"""Fixture skill repositories for marketplace tests: explicit inputs, no git, no network."""

from __future__ import annotations

import shutil
from pathlib import Path


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


def copying_cloner(repo: Path):  # type: ignore[no-untyped-def]  # a Cloner
    """A cloner that copies *repo* into the destination instead of running git.

    The returned callable records ``calls`` as ``(url, ref)`` pairs and
    ``tokens`` as the credential handed to each clone.
    """
    calls: list[tuple[str, str | None]] = []
    tokens: list[str | None] = []

    def clone(url: str, ref: str | None, dest: Path, *, token: str | None = None) -> None:
        calls.append((url, ref))
        tokens.append(token)
        shutil.copytree(repo, dest, symlinks=True)

    clone.calls = calls  # type: ignore[attr-defined]
    clone.tokens = tokens  # type: ignore[attr-defined]
    return clone
