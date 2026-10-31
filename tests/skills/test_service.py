"""The fetcher: cached catalogs, packaging by name, and GitHub-only sources."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from omnigent.skills.git_source import GitSourceError
from omnigent.skills.limits import (
    CLONE_TIMEOUT_S,
    MAX_CHECKOUT_BYTES,
    MAX_CHECKOUT_ENTRIES,
    CloneLimits,
)
from omnigent.skills.policy import SkillPolicyError, SkillsMarketplacePolicy, SourceCredential
from omnigent.skills.service import SkillFetcher, resolve_source


def test_resolve_source_prefers_an_explicit_ref_and_names_the_restriction() -> None:
    # Parsing itself is covered with the shared helper; this is the service's part.
    assert resolve_source("https://github.com/acme/skills/tree/v2", "main").ref == "main"
    assert resolve_source("https://github.com/acme/skills/tree/v2").ref == "v2"
    with pytest.raises(GitSourceError, match="Only GitHub"):
        resolve_source("https://gitlab.com/acme/skills")


def test_resolve_source_keeps_the_linked_directory() -> None:
    # A /tree/<ref>/<dir> link was reduced to the repository, so its directory
    # was silently replaced by the root catalog.
    linked = resolve_source("https://github.com/acme/mono/tree/main/team-a/skills")
    assert (linked.url, linked.ref, linked.path) == (
        "https://github.com/acme/mono.git",
        "main",
        "team-a/skills",
    )
    assert resolve_source("acme/mono", path="/team-b/").path == "team-b"
    assert resolve_source("acme/mono/tree/main/team-a", path="").path == ""
    with pytest.raises(GitSourceError, match="not a directory inside"):
        resolve_source("acme/mono", path="team-a/../../etc")
    with pytest.raises(GitSourceError, match="limited to 200"):
        resolve_source("acme/mono", path="a" * 201)


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


def test_catalog_cache_is_bounded_and_drops_expired_entries_first(
    plain_repo: Path, fake_clone, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.skills import service

    clock = {"now": 1_000.0}
    monkeypatch.setattr(service.time, "monotonic", lambda: clock["now"])
    clone = fake_clone(plain_repo)
    fetcher = SkillFetcher(clone=clone, ttl_s=100, max_cached=2)

    for index in range(3):
        fetcher.catalog(f"acme/r{index}")
    # Any signed-in user can preview repositories; retention never exceeds the cap.
    assert fetcher.cached_catalogs == 2
    fetcher.catalog("acme/r2")
    assert len(clone.calls) == 3  # still cached
    fetcher.catalog("acme/r0")
    assert len(clone.calls) == 4  # evicted, so cloned again

    clock["now"] += 200
    fetcher.catalog("acme/r9")
    assert fetcher.cached_catalogs == 1  # the expired ones went before the cap was applied


def test_package_clones_once_and_tars_each_named_skill(plain_repo: Path, fake_clone) -> None:
    clone = fake_clone(plain_repo)
    fetcher = SkillFetcher(clone=clone)

    commit, packaged = fetcher.package("acme/skills", None, ["triage", "review"])

    assert commit is None  # the fixture copy is not a git checkout
    assert [p.entry.name for p in packaged] == ["triage", "review"]
    assert packaged[1].entry.display_name == "Code Review"
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

    fetcher = SkillFetcher(clone=fake_clone(plain_repo))
    sizes = [
        len(fetcher.package("acme/skills", None, [name])[1][0].tarball)
        for name in ("review", "triage")
    ]
    # Either skill alone fits; the two together do not.
    monkeypatch.setattr(service, "MAX_INSTALL_BYTES", max(sizes))
    assert len(fetcher.package("acme/skills", None, ["triage"])[1]) == 1
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
        max_bytes: int | None = None,
        max_entries: int | None = None,
        sparse: object = None,
        timeout_s: float | None = None,
    ) -> None:
        seen.update(
            isolated=isolated,
            token=token,
            max_bytes=max_bytes,
            max_entries=max_entries,
            timeout_s=timeout_s,
        )
        raise GitSourceError("stop")

    monkeypatch.setattr(service, "clone_github_source", clone)
    with pytest.raises(GitSourceError):
        service.server_clone("https://github.com/a/b.git", None, Path("/nonexistent"), token="t")
    assert seen == {
        "isolated": True,
        "token": "t",
        "max_bytes": MAX_CHECKOUT_BYTES,
        "max_entries": MAX_CHECKOUT_ENTRIES,
        "timeout_s": CLONE_TIMEOUT_S,
    }

    # An operator's limits replace each budget.
    limits = CloneLimits(checkout_bytes=2 * MAX_CHECKOUT_BYTES, checkout_entries=7, timeout_s=9)
    with pytest.raises(GitSourceError):
        service.server_clone(
            "https://github.com/a/b.git", None, Path("/nonexistent"), limits=limits
        )
    assert (seen["max_bytes"], seen["max_entries"], seen["timeout_s"]) == (
        2 * MAX_CHECKOUT_BYTES,
        7,
        9,
    )


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


def test_a_source_directory_is_the_catalog_root_and_clones_stay_sparse(
    tmp_path: Path, fake_clone
) -> None:
    from tests._helpers.skill_repos import write_skill

    repo = tmp_path / "mono"
    write_skill(repo / "skills", "root-only", "Root")
    write_skill(repo / "team-a" / "skills", "lint", "Lint")
    write_skill(repo / "team-a" / "skills", "format", "Format")
    clone = fake_clone(repo)
    fetcher = SkillFetcher(clone=clone)

    catalog = fetcher.catalog("https://github.com/acme/mono/tree/main/team-a")
    assert (catalog.path, [e.name for e in catalog.entries]) == ("team-a", ["format", "lint"])
    # The catalog checks out manifests only, under the directory.
    assert clone.sparse[0] == ("/team-a/**/SKILL.md", "/team-a/.claude-plugin/marketplace.json")
    # The repository root is a different catalog, cached separately.
    assert [e.name for e in fetcher.catalog("acme/mono", "main").entries] == ["root-only"]

    # An install adds only the chosen skill's directory to the sparse checkout.
    _commit, packaged = fetcher.package("acme/mono", "main", ["lint"], path="team-a")
    assert [p.entry.path for p in packaged] == ["team-a/skills/lint"]
    assert clone.sparse[-1][-1] == "/team-a/skills/lint/"
    # An update repackages from the skill's own directory.
    _commit, again = fetcher.package("acme/mono", "main", ["lint"], path="team-a/skills/lint")
    assert again[0].entry.name == "lint"
    with pytest.raises(GitSourceError, match=r"No such skill in .*\(team-a\): root-only"):
        fetcher.package("acme/mono", "main", ["root-only"], path="team-a")


def test_a_linked_directory_that_escapes_the_checkout_is_not_indexed(
    tmp_path: Path, fake_clone
) -> None:
    from tests._helpers.skill_repos import write_skill

    outside = tmp_path / "outside"
    write_skill(outside / "skills", "secret", "Outside the repository")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "team-a").symlink_to(outside, target_is_directory=True)
    # Only the first segment is a link; the directory itself would index cleanly.
    fetcher = SkillFetcher(clone=fake_clone(repo))
    catalog = fetcher.catalog("acme/repo", path="team-a/skills/secret")
    assert catalog.entries == () and "not a directory" in catalog.skipped[0]
