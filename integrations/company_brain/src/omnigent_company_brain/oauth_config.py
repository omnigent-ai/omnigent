from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

ProviderName = Literal["google", "slack", "notion"]
CLIENT_CREDENTIAL_FORM_FIELD = "client_secret"


@dataclass(frozen=True, slots=True)
class OAuthProviderConfig:
    name: ProviderName
    auth_url: str
    token_url: str
    client_id_env: str
    client_credential_env: str
    scopes: tuple[str, ...]
    redirect_path: str
    scope_parameter: str = "scope"
    auth_parameters: dict[str, str] = field(default_factory=dict)
    pkce: bool = False
    refreshable: bool = False


PROVIDERS: dict[ProviderName, OAuthProviderConfig] = {
    "google": OAuthProviderConfig(
        name="google",
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        client_id_env="GOOGLE_OAUTH_CLIENT_ID",
        client_credential_env="GOOGLE_OAUTH_CLIENT_SECRET",
        scopes=(
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.acl.readonly",
            "openid",
            "email",
        ),
        redirect_path="google/callback",
        auth_parameters={"access_type": "offline", "prompt": "consent"},
        pkce=True,
        refreshable=True,
    ),
    "slack": OAuthProviderConfig(
        name="slack",
        auth_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        client_id_env="SLACK_OAUTH_CLIENT_ID",
        client_credential_env="SLACK_OAUTH_CLIENT_SECRET",
        scopes=(
            "channels:history",
            "channels:read",
            "reactions:read",
            "team:read",
            "users:read",
        ),
        redirect_path="slack/callback",
    ),
    "notion": OAuthProviderConfig(
        name="notion",
        auth_url="https://api.notion.com/v1/oauth/authorize",
        token_url="https://api.notion.com/v1/oauth/token",
        client_id_env="NOTION_OAUTH_CLIENT_ID",
        client_credential_env="NOTION_OAUTH_CLIENT_SECRET",
        scopes=(),
        redirect_path="notion/callback",
        auth_parameters={"owner": "user"},
    ),
}


@dataclass(frozen=True, slots=True)
class ResolvedOAuthClient:
    client_id: str
    client_credential: str = field(repr=False)
    hosted_domain: str | None = None


def resolve_oauth_client(provider: ProviderName) -> ResolvedOAuthClient:
    config = PROVIDERS[provider]
    client_id = os.environ.get(config.client_id_env)
    client_credential = os.environ.get(config.client_credential_env)
    if not client_id or not client_credential:
        raise RuntimeError(
            f"provider not configured; set {config.client_id_env} and "
            f"{config.client_credential_env}"
        )
    return ResolvedOAuthClient(
        client_id=client_id,
        client_credential=client_credential,
        hosted_domain=os.environ.get("GOOGLE_WORKSPACE_DOMAIN") if provider == "google" else None,
    )
