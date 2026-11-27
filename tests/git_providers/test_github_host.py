"""Tests for the configured GitHub host (github.com or GitHub Enterprise Server)."""

from __future__ import annotations

import pytest

from omnigent.git_providers.github_host import (
    DEFAULT_API_URL,
    DEFAULT_HOST,
    GitHubHost,
    github_host_from_env,
    normalize_host,
    parse_github_host,
)


def test_unset_means_github_com() -> None:
    assert parse_github_host(None, None) == GitHubHost()
    assert parse_github_host("", " ") == GitHubHost()
    github = github_host_from_env({})
    assert (github.host, github.api_url, github.web_url) == (
        DEFAULT_HOST,
        DEFAULT_API_URL,
        "https://github.com",
    )
    assert github.is_default


def test_enterprise_host_derives_api_v3() -> None:
    github = github_host_from_env({"OMNIGENT_GITHUB_HOST": "GHE.Example.com"})
    assert github.host == "ghe.example.com"
    assert github.api_url == "https://ghe.example.com/api/v3"
    assert github.web_url == "https://ghe.example.com"
    assert not github.is_default


def test_explicit_api_url_wins_and_is_normalized() -> None:
    github = parse_github_host("ghe.example.com", "https://API.ghe.example.com:8443/v3/")
    assert github.api_url == "https://api.ghe.example.com:8443/v3"


def test_host_with_port_is_accepted() -> None:
    assert parse_github_host("ghe.example.com:8443", None).api_url == (
        "https://ghe.example.com:8443/api/v3"
    )


@pytest.mark.parametrize(
    "host",
    [
        "https://ghe.example.com",
        "ghe.example.com/path",
        "user@ghe.example.com",
        "ghe example.com",
        "-ghe.example.com",
    ],
)
def test_invalid_host_is_rejected(host: str) -> None:
    with pytest.raises(ValueError, match="OMNIGENT_GITHUB_HOST"):
        parse_github_host(host, None)


@pytest.mark.parametrize(
    "api_url",
    [
        "http://ghe.example.com/api/v3",
        "ghe.example.com/api/v3",
        "https://user:pass@ghe.example.com/api/v3",
        "https://ghe.example.com/api/v3?x=1",
        "https://ghe.example.com/api/v3#frag",
    ],
)
def test_invalid_api_url_is_rejected(api_url: str) -> None:
    with pytest.raises(ValueError, match="OMNIGENT_GITHUB_API_URL"):
        parse_github_host("ghe.example.com", api_url)


def test_normalize_host_rejects_non_hosts() -> None:
    assert normalize_host("GitHub.com") == "github.com"
    assert normalize_host("ghe.example.com:443") == "ghe.example.com:443"
    for value in (None, "", "https://x.example.com", "x.example.com/p", "a b"):
        assert normalize_host(value) is None
