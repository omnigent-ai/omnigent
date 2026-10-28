"""Fixture repositories for the skills package: plain and marketplace layouts."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests._helpers.skill_repos import copying_cloner, write_skill


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
def fake_clone() -> Callable[[Path], Callable[..., None]]:
    """Build a cloner that copies a fixture repo instead of running git."""
    return copying_cloner
