"""Indexing a checkout: plain and marketplace layouts, precedence, and what is skipped."""

from __future__ import annotations

import shutil
from pathlib import Path

from omnigent.skills.catalog import index_checkout


def test_plain_repo_lists_root_and_namespaced_skills(plain_repo: Path) -> None:
    entries = index_checkout(plain_repo)
    assert [(e.name, e.description, e.display_name, e.path, e.plugin) for e in entries] == [
        ("review", "Review a diff", "Code Review", "skills/review", None),
        ("triage", "Triage issues", None, "skills/team/triage", None),
    ]


def test_marketplace_indexes_in_repo_plugins_and_reports_the_rest(marketplace_repo: Path) -> None:
    skipped: list[str] = []
    entries = index_checkout(marketplace_repo, skipped=skipped)
    # The root skill shadows the plugin's same-named skill.
    assert [(e.name, e.description, e.plugin) for e in entries] == [
        ("changelog", "Root changelog", None),
    ]
    assert any("'remote'" in note and "another repository" in note for note in skipped)
    assert any("'escape'" in note and "not a directory" in note for note in skipped)


def test_marketplace_plugin_skills_are_attributed(marketplace_repo: Path) -> None:
    # Without the shadowing root skill, the plugin's own skill is listed under it.
    shutil.rmtree(marketplace_repo / "skills")
    entries = index_checkout(marketplace_repo)
    assert [(e.name, e.plugin, e.path) for e in entries] == [
        ("changelog", "docs", "plugins/docs/skills/changelog"),
    ]


def test_empty_or_missing_layouts_index_nothing(tmp_path: Path) -> None:
    assert index_checkout(tmp_path) == []
