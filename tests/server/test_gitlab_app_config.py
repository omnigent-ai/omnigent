"""Focused tests for GitLab OAuth configuration and host validation."""

from __future__ import annotations

import pytest

from omnigent.server.gitlab_app import (
    GitLabAppConfig,
    build_authorize_url,
    normalize_gitlab_host,
    token_set_from_payload,
)


def test_config_defaults_to_gitlab_com(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_GITLAB_CLIENT_ID", "client")
    monkeypatch.setenv("OMNIGENT_GITLAB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OMNIGENT_DOMAIN", "omnigent.example")
    config = GitLabAppConfig.from_env()
    assert config is not None
    assert config.host == "https://gitlab.com"
    assert config.redirect_uri == "https://omnigent.example/v1/connections/gitlab/callback"
    assert "response_type=code" in build_authorize_url(config, state="signed")


def test_config_accepts_canonical_self_managed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_GITLAB_CLIENT_ID", "client")
    monkeypatch.setenv("OMNIGENT_GITLAB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OMNIGENT_GITLAB_REDIRECT_URI", "https://app.example/callback")
    monkeypatch.setenv("OMNIGENT_GITLAB_HOST", "https://GitLab.Dedicated.example:443/")
    config = GitLabAppConfig.from_env()
    assert config is not None
    assert config.host == "https://gitlab.dedicated.example"


@pytest.mark.parametrize(
    "value",
    [
        "http://gitlab.example",
        "https://gitlab.example/path",
        "https://user:pass@gitlab.example",
        "https://gitlab.example?x=1",
    ],
)
def test_rejects_unsafe_instance_origins(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_gitlab_host(value)


def test_token_payload_allows_non_expiring_token() -> None:
    token = token_set_from_payload({"access_token": "token", "scope": "api"})
    assert token.access_token == "token"
    assert token.refresh_token is None
    assert token.expires_at is None
