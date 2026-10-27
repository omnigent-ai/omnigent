"""The fetcher: cached catalogs, packaging by name, and GitHub-only sources."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from omnigent.skills.git_source import GitSourceError
from omnigent.skills.service import SkillFetcher, resolve_source


def test_resolve_source_accepts_shorthand_and_urls_only_for_github() -> None:
    assert resolve_source("acme/skills") == ("https://github.com/acme/skills.git", None)
    assert resolve_source("https://github.com/acme/skills/tree/v2", None) == (
        "https://github.com/acme/skills.git",
        "v2",
    )
    assert resolve_source("acme/skills", "main")[1] == "main"
    with pytest.raises(GitSourceError, match="Only GitHub"):
        resolve_source("https://gitlab.com/acme/skills")


def test_catalog_is_cached_per_source_until_refreshed(plain_repo: Path, fake_clone) -> None:
    clone = fake_clone(plain_repo)
    fetcher = SkillFetcher(clone=clone)

    first = fetcher.catalog("acme/skills")
    again = fetcher.catalog("acme/skills")
    assert again is first
    assert [e.name for e in first.entries] == ["review", "triage"]
    assert clone.calls == [("https://github.com/acme/skills.git", None)]

    fetcher.catalog("acme/skills", refresh=True)
    fetcher.catalog("acme/skills", "v2")
    assert len(clone.calls) == 3


def test_package_clones_once_and_tars_each_named_skill(plain_repo: Path, fake_clone) -> None:
    clone = fake_clone(plain_repo)
    fetcher = SkillFetcher(clone=clone)

    commit, packaged = fetcher.package("acme/skills", None, ["triage", "review"])

    assert commit is None  # the fixture copy is not a git checkout
    assert [p.entry.name for p in packaged] == ["triage", "review"]
    assert packaged[1].markdown.startswith("---\nname: Code Review")
    with tarfile.open(fileobj=io.BytesIO(packaged[0].tarball), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["SKILL.md", "notes.txt"]
    assert len(clone.calls) == 1


def test_package_names_the_missing_skill(plain_repo: Path, fake_clone) -> None:
    fetcher = SkillFetcher(clone=fake_clone(plain_repo))
    with pytest.raises(GitSourceError, match=r"No such skill .*: nope"):
        fetcher.package("acme/skills", None, ["review", "nope"])
