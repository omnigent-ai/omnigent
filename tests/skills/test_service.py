"""The fetcher: cached catalogs, packaging by name, and GitHub-only sources."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from omnigent.skills.git_source import GitSourceError
from omnigent.skills.policy import SkillPolicyError, SkillsMarketplacePolicy, SourceCredential
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


def test_package_bounds_the_whole_request(
    plain_repo: Path, fake_clone, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.skills import service

    monkeypatch.setattr(service, "MAX_INSTALL_BYTES", 10)
    fetcher = SkillFetcher(clone=fake_clone(plain_repo))
    with pytest.raises(GitSourceError, match=r"exceed .* together"):
        fetcher.package("acme/skills", None, ["review", "triage"])


def test_fetcher_clones_isolated_from_server_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.skills import service

    seen: dict[str, object] = {}

    def clone(
        url: str,
        ref: str | None,
        dest: Path,
        *,
        isolated: bool = False,
        token: str | None = None,
    ) -> None:
        seen["isolated"], seen["token"] = isolated, token
        raise GitSourceError("stop")

    monkeypatch.setattr(service, "clone_github_source", clone)
    with pytest.raises(GitSourceError):
        service.server_clone("https://github.com/a/b.git", None, Path("/nonexistent"), token="t")
    assert seen == {"isolated": True, "token": "t"}


def test_fetcher_refuses_a_source_outside_the_policy_before_cloning(
    plain_repo: Path, fake_clone
) -> None:
    clone = fake_clone(plain_repo)
    policy = SkillsMarketplacePolicy(allowed_sources=("github.com/acme/*",))
    fetcher = SkillFetcher(clone=clone, policy=policy)

    with pytest.raises(SkillPolicyError, match="not an allowed skill source"):
        fetcher.catalog("other/skills")
    with pytest.raises(SkillPolicyError):
        fetcher.package("https://github.com/other/skills", None, ["review"])
    assert clone.calls == []

    fetcher.catalog("acme/skills")
    assert clone.calls == [("https://github.com/acme/skills.git", None)]


def test_fetcher_hands_the_policys_token_to_the_clone(
    plain_repo: Path, fake_clone, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILLS_TOKEN", "ghs_secret")
    clone = fake_clone(plain_repo)
    policy = SkillsMarketplacePolicy(
        credentials=(SourceCredential(match="github.com/acme/*", token_env="SKILLS_TOKEN"),)
    )
    fetcher = SkillFetcher(clone=clone, policy=policy)

    fetcher.catalog("acme/skills")
    fetcher.package("other/skills", None, ["review"])
    assert clone.tokens == ["ghs_secret", None]
