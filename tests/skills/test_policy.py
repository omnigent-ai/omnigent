"""The operator's ``skills_marketplace:`` policy: parsing, source globs and scoped tokens."""

from __future__ import annotations

import logging

import pytest

from omnigent.skills.policy import (
    ConfiguredSource,
    SkillPolicyError,
    SkillsMarketplacePolicy,
    SourceCredential,
    pattern_matches,
    repository_key,
)

RAW = {
    "sources": [
        {"url": "https://github.com/acme/skills", "ref": "v1.4.0", "name": "Acme"},
        "acme/more",
        {"url": "https://gitlab.com/x/y"},
        {"ref": "main"},
    ],
    "allowed_sources": ["github.com/acme/*", "/GitHub.ACME.internal/*/*", "", "a/b/c/d"],
    "allow_url_install": False,
    "credentials": [
        {"match": "github.acme.internal/*/*", "token_env": "SKILLS_GHE_TOKEN"},
        {"match": "github.com/acme/*"},
    ],
}


def test_from_config_keeps_usable_entries_and_logs_the_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        policy = SkillsMarketplacePolicy.from_config(RAW)

    assert [(s.url, s.ref, s.name) for s in policy.sources] == [
        ("https://github.com/acme/skills.git", "v1.4.0", "Acme"),
        ("https://github.com/acme/more.git", None, None),
    ]
    assert policy.allowed_sources == ("github.com/acme/*", "github.acme.internal/*/*")
    assert policy.allow_url_install is False
    assert [(c.match, c.token_env) for c in policy.credentials] == [
        ("github.acme.internal/*/*", "SKILLS_GHE_TOKEN")
    ]
    skipped = [r.getMessage() for r in caplog.records if "skills_marketplace" in r.getMessage()]
    assert len(skipped) == 5


@pytest.mark.parametrize("raw", [None, "nope", {"allow_url_install": "yes", "sources": "x"}])
def test_an_unusable_block_falls_back_to_allowing_everything(raw: object) -> None:
    policy = SkillsMarketplacePolicy.from_config(raw)
    assert policy.allows("https://github.com/anyone/anything.git")
    assert policy.allow_url_install and not policy.sources and not policy.credentials


@pytest.mark.parametrize(
    ("pattern", "key", "expected"),
    [
        ("github.com/acme/*", "github.com/acme/skills", True),
        ("github.com/acme/*", "github.com/other/skills", False),
        ("github.com/acme/sk*", "github.com/acme/skills", True),
        ("github.com/acme/skills", "github.com/acme/skills-extra", False),
        # A shorter pattern covers the rest; ``*`` never crosses a slash.
        ("github.acme.internal", "github.acme.internal/team/skills", True),
        ("github.com/*", "github.com/acme/skills", True),
        ("*/acme/skills", "github.com/acme/skills", True),
        ("github.com/*", "github.acme.internal/acme/skills", False),
    ],
)
def test_patterns_match_segment_by_segment(pattern: str, key: str, expected: bool) -> None:
    assert pattern_matches(pattern, key) is expected


def test_allowed_sources_bind_only_when_set() -> None:
    assert SkillsMarketplacePolicy().allows("https://github.com/a/b.git")
    policy = SkillsMarketplacePolicy(allowed_sources=("github.com/acme/*",))
    assert policy.allows("https://github.com/acme/skills.git")
    assert not policy.allows("https://github.com/other/skills.git")
    assert not policy.allows("git@github.com:acme/skills.git")
    with pytest.raises(SkillPolicyError, match="not an allowed skill source"):
        policy.require_allowed("https://github.com/other/skills.git")


def test_a_token_goes_only_to_a_source_both_lists_allow(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    policy = SkillsMarketplacePolicy(
        allowed_sources=("github.acme.internal/*/*",),
        credentials=(
            SourceCredential(match="github.acme.internal/*/*", token_env="SKILLS_GHE_TOKEN"),
            SourceCredential(match="github.com/*", token_env="PUBLIC_TOKEN"),
        ),
    )
    monkeypatch.setenv("SKILLS_GHE_TOKEN", " ghs_secret ")
    monkeypatch.setenv("PUBLIC_TOKEN", "never-sent")
    assert policy.token_for("https://github.acme.internal/team/skills.git") == "ghs_secret"
    # A credential outside allowed_sources never fires, even with its variable set.
    assert policy.token_for("https://github.com/acme/skills.git") is None

    monkeypatch.delenv("SKILLS_GHE_TOKEN")
    with caplog.at_level(logging.WARNING):
        assert policy.token_for("https://github.acme.internal/team/skills.git") is None
    assert "SKILLS_GHE_TOKEN" in caplog.text


def test_repository_key_and_configured_sources() -> None:
    assert repository_key("https://GitHub.com/Acme/Skills.git") == "github.com/acme/skills"
    assert repository_key("https://github.com/acme") is None
    assert repository_key("http://github.com/acme/skills") is None
    policy = SkillsMarketplacePolicy(
        sources=(ConfiguredSource(url="https://github.com/acme/skills.git", ref="v1", name=None),)
    )
    assert policy.is_configured("https://github.com/acme/skills.git", "v1")
    assert not policy.is_configured("https://github.com/acme/skills.git", None)
