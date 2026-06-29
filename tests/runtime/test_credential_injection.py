"""Tests for the per-user secret → env-var mapping (#5)."""

from __future__ import annotations

from omnigent.runtime.credentials.injection import (
    build_credential_env,
    injectable_credential_names,
)


def test_empty_input_injects_nothing() -> None:
    assert build_credential_env({}) == {}


def test_github_feeds_both_token_vars() -> None:
    # `gh` reads GH_TOKEN; actions / git HTTPS helpers read GITHUB_TOKEN — one
    # logical secret must populate both.
    env = build_credential_env({"github": "ghp_alice"})
    assert env == {"GITHUB_TOKEN": "ghp_alice", "GH_TOKEN": "ghp_alice"}


def test_github_aliases_are_equivalent() -> None:
    for name in ("github", "github_token", "gh_token", "gh"):
        assert build_credential_env({name: "t"}) == {"GITHUB_TOKEN": "t", "GH_TOKEN": "t"}


def test_aws_trio_maps_to_standard_vars() -> None:
    env = build_credential_env(
        {
            "aws_access_key_id": "AKIA",
            "aws_secret_access_key": "secret",
            "aws_session_token": "sess",
        }
    )
    assert env == {
        "AWS_ACCESS_KEY_ID": "AKIA",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_SESSION_TOKEN": "sess",
    }


def test_name_is_case_insensitive_and_trimmed() -> None:
    assert build_credential_env({"  GitHub  ": "t"}) == {
        "GITHUB_TOKEN": "t",
        "GH_TOKEN": "t",
    }


def test_env_style_name_passes_through_verbatim() -> None:
    # An UPPER_SNAKE name with no alias is injected as itself, so a user can
    # wire an arbitrary credential without a table entry.
    assert build_credential_env({"MY_SERVICE_TOKEN": "v"}) == {"MY_SERVICE_TOKEN": "v"}


def test_unknown_freeform_name_is_dropped_not_guessed() -> None:
    # A lowercase non-alias, non-env-style name must never silently invent a
    # variable (could clobber something unrelated).
    assert build_credential_env({"some random label": "v"}) == {}


def test_empty_values_are_skipped() -> None:
    assert build_credential_env({"github": "", "MY_TOKEN": "keep"}) == {"MY_TOKEN": "keep"}


def test_injectable_names_cover_known_aliases() -> None:
    names = injectable_credential_names()
    assert "github" in names
    assert "aws_secret_access_key" in names
    # Env-style passthrough names are not enumerable, so absent here.
    assert "MY_SERVICE_TOKEN" not in names
