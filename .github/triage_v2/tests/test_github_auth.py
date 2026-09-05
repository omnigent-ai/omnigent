from __future__ import annotations

from datetime import UTC, datetime

import pytest

from issue_prioritization.github_auth import GitHubAppTokenProvider, resolve_github_token


def test_app_provider_resolves_installation_and_mints_token() -> None:
    calls = []
    signed = {}

    def signer(claims, private_key):
        signed.update(claims)
        signed["private_key"] = private_key
        return "app-jwt"

    def transport(method, path, payload, bearer):
        calls.append((method, path, payload, bearer))
        if path.endswith("/installation"):
            return {"id": 1234}
        return {"token": " installation-token\n"}

    provider = GitHubAppTokenProvider(
        " client-id ",
        " private-key\n",
        "omnigent-ai/omnigent",
        transport=transport,
        clock=lambda: datetime(2026, 8, 6, 9, 0, tzinfo=UTC),
        signer=signer,
    )

    assert provider.installation_token() == "installation-token"
    assert signed == {
        "iat": 1786006740,
        "exp": 1786007340,
        "iss": "client-id",
        "private_key": "private-key",
    }
    assert calls == [
        (
            "GET",
            "/repos/omnigent-ai/omnigent/installation",
            None,
            "app-jwt",
        ),
        (
            "POST",
            "/app/installations/1234/access_tokens",
            {},
            "app-jwt",
        ),
    ]


def test_static_token_auth_strips_secret_whitespace() -> None:
    token = resolve_github_token(
        "token",
        "omnigent-ai/omnigent",
        lambda key: " pat-token\n",
        "github-token",
        "github-app-client-id",
        "github-app-private-key",
    )

    assert token == "pat-token"


def test_app_auth_falls_back_to_static_token() -> None:
    secrets = {
        "github-app-client-id": "client-id",
        "github-app-private-key": "not-a-private-key",
        "github-token": " fallback-token\n",
    }
    warnings = []

    token = resolve_github_token(
        "app",
        "omnigent-ai/omnigent",
        secrets.__getitem__,
        "github-token",
        "github-app-client-id",
        "github-app-private-key",
        warn=warnings.append,
    )

    assert token == "fallback-token"
    assert warnings == ["GitHub App authentication failed; using the configured PAT fallback"]


def test_app_auth_requires_app_credentials_or_fallback() -> None:
    def missing_secret(key):
        raise KeyError(key)

    with pytest.raises(RuntimeError, match="PAT fallback is unavailable"):
        resolve_github_token(
            "app",
            "omnigent-ai/omnigent",
            missing_secret,
            "github-token",
            "github-app-client-id",
            "github-app-private-key",
        )
