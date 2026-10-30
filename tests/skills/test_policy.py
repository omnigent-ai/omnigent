"""The operator's ``skills_marketplace:`` policy: parsing, source globs and scoped tokens."""

from __future__ import annotations

import logging

import pytest

from omnigent.skills.policy import (
    CLOSED_POLICY,
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


def test_no_block_or_an_explicit_empty_allowlist_allows_everything() -> None:
    for raw in (None, {}, {"allowed_sources": []}):
        policy = SkillsMarketplacePolicy.from_config(raw)
        assert policy.allowed_sources is None
        assert policy.allows("https://github.com/anyone/anything.git")
        assert policy.allow_url_install and not policy.sources and not policy.credentials


@pytest.mark.parametrize(
    "raw",
    [
        "nope",
        ["github.com/acme/*"],
        {"allowed_sources": ["a/b/c/d", "", 7]},
        {"allowed_sources": "a/b/c/d"},
        {"allowed_sources": {"host": "github.com"}},
    ],
)
def test_a_mistaken_restriction_fails_closed_not_open(
    raw: object, caplog: pytest.LogCaptureFixture
) -> None:
    # A typo in the allowlist must never widen it to every repository.
    with caplog.at_level(logging.WARNING):
        policy = SkillsMarketplacePolicy.from_config(raw)
    assert policy.allowed_sources == ()
    assert not policy.allows("https://github.com/acme/skills.git")
    assert "refusing every source" in caplog.text
    if not isinstance(raw, dict):
        assert policy == CLOSED_POLICY and policy.allow_url_install is False


def test_a_non_boolean_url_install_switch_is_off() -> None:
    policy = SkillsMarketplacePolicy.from_config({"allow_url_install": "yes"})
    assert policy.allow_url_install is False


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


def test_sources_keep_their_directory_and_accept_the_allowed_sources_form() -> None:
    # host/owner/repo used to parse as owner "github.com", repo "acme", so the
    # seeded source pointed at a repository that does not exist.
    policy = SkillsMarketplacePolicy.from_config(
        {
            "sources": [
                "github.com/acme/mono",
                "https://github.com/acme/mono/tree/main/team-a",
                {"url": "acme/mono", "path": "team-b/skills"},
                {"url": "acme/mono", "path": "../etc"},
            ]
        }
    )
    assert [(s.url, s.ref, s.path) for s in policy.sources] == [
        ("https://github.com/acme/mono.git", None, ""),
        ("https://github.com/acme/mono.git", "main", "team-a"),
        ("https://github.com/acme/mono.git", None, "team-b/skills"),
    ]
    assert policy.is_configured("https://github.com/acme/mono.git", "main", "team-a")
    assert not policy.is_configured("https://github.com/acme/mono.git", "main")


def test_clone_limits_are_operator_configurable(caplog: pytest.LogCaptureFixture) -> None:
    from omnigent.skills.limits import DEFAULT_CLONE_LIMITS, MIB

    assert SkillsMarketplacePolicy.from_config({}).clone_limits == DEFAULT_CLONE_LIMITS
    with caplog.at_level(logging.WARNING):
        policy = SkillsMarketplacePolicy.from_config(
            {
                "limits": {
                    "checkout_mb": 2048,
                    "checkout_entries": 200_000,
                    "clone_timeout_s": 900,
                    "clone_timeout": 1,
                }
            }
        )
    assert (
        policy.clone_limits.checkout_bytes,
        policy.clone_limits.checkout_entries,
        policy.clone_limits.timeout_s,
    ) == (2048 * MIB, 200_000, 900)
    assert any("clone_timeout is not a known limit" in r.getMessage() for r in caplog.records)
    # A value that is not a positive number keeps that default rather than lifting the bound.
    for bad in (0, -1, True, "1000", None):
        kept = SkillsMarketplacePolicy.from_config({"limits": {"checkout_entries": bad}})
        assert kept.clone_limits.checkout_entries == DEFAULT_CLONE_LIMITS.checkout_entries
