"""Fixture repositories for the skills package: plain and marketplace layouts."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest


def write_skill(
    skills_dir: Path, name: str, description: str, *, label: str | None = None
) -> Path:
    skill = skills_dir / name
    skill.mkdir(parents=True)
    front = f"name: {label}\n" if label else f"name: {name}\n"
    (skill / "SKILL.md").write_text(f"---\n{front}description: {description}\n---\nDo {name}.\n")
    (skill / "notes.txt").write_text("supporting file\n")
    return skill


@pytest.fixture
def plain_repo(tmp_path: Path) -> Path:
    """``skills/`` at the root with two skills, one namespaced."""
    repo = tmp_path / "plain"
    write_skill(repo / "skills", "review", "Review a diff", label="Code Review")
    write_skill(repo / "skills" / "team", "triage", "Triage issues")
    return repo


@pytest.fixture
def marketplace_repo(tmp_path: Path) -> Path:
    """A Claude Code marketplace with one in-repo plugin and one external plugin."""
    repo = tmp_path / "market"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "acme",
                "plugins": [
                    {"name": "docs", "source": "./plugins/docs", "description": "Docs tools"},
                    {"name": "remote", "source": {"source": "github", "repo": "acme/other"}},
                    {"name": "escape", "source": "../outside"},
                ],
            }
        )
    )
    write_skill(repo / "plugins" / "docs" / "skills", "changelog", "Write a changelog")
    # A root-level skill with the same name as a plugin skill: root wins.
    write_skill(repo / "skills", "changelog", "Root changelog")
    return repo


@pytest.fixture
def fake_clone(tmp_path: Path) -> Callable[[Path], Callable[[str, str | None, Path], None]]:
    """Build a cloner that copies a fixture repo instead of running git."""

    def make(repo: Path):
        calls: list[tuple[str, str | None]] = []

        def clone(url: str, ref: str | None, dest: Path) -> None:
            calls.append((url, ref))
            shutil.copytree(repo, dest, symlinks=True)

        clone.calls = calls  # type: ignore[attr-defined]
        return clone

    return make
