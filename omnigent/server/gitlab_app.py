"""GitLab OAuth configuration and token parsing.

GitLab uses standard OAuth 2.0.  The configured instance URL is deliberately
validated once here so self-managed and Dedicated deployments never construct
OAuth or API requests from an untrusted request parameter.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

_logger = logging.getLogger(__name__)
_DEFAULT_HOST = "https://gitlab.com"


class GitLabAppError(Exception):
    """Raised when GitLab OAuth returns an invalid or failed response."""


@dataclass(frozen=True)
class GitLabTokenSet:
    """A GitLab OAuth token response, with optional expiry/refresh metadata."""

    access_token: str
    refresh_token: str | None
    expires_at: int | None
    scopes: str


@dataclass(frozen=True)
class GitLabAppConfig:
    """Validated deployment-wide GitLab OAuth configuration."""

    client_id: str
    client_secret: str
    host: str
    redirect_uri: str
    scopes: str = "read_user api"

    @staticmethod
    def from_env() -> GitLabAppConfig | None:
        """Build config from GitLab env vars, or disable the provider when absent."""
        client_id = os.environ.get("OMNIGENT_GITLAB_CLIENT_ID", "").strip()
        client_secret = os.environ.get("OMNIGENT_GITLAB_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            return None
        host = normalize_gitlab_host(os.environ.get("OMNIGENT_GITLAB_HOST", _DEFAULT_HOST))
        redirect_uri = os.environ.get("OMNIGENT_GITLAB_REDIRECT_URI", "").strip()
        if not redirect_uri:
            domain = os.environ.get("OMNIGENT_DOMAIN", "").strip()
            if not domain:
                _logger.warning(
                    "GitLab client credentials are set but neither OMNIGENT_GITLAB_REDIRECT_URI "
                    "nor OMNIGENT_DOMAIN is set; GitLab integration stays disabled."
                )
                return None
            redirect_uri = f"https://{domain}/v1/connections/gitlab/callback"
        return GitLabAppConfig(
            client_id=client_id,
            client_secret=client_secret,
            host=host,
            redirect_uri=redirect_uri,
            scopes=os.environ.get("OMNIGENT_GITLAB_SCOPES", "read_user api").strip()
            or "read_user api",
        )

    def code_exchange_fields(self, code: str) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }

    def token_refresh_fields(self, refresh_token: str) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }


def normalize_gitlab_host(value: str) -> str:
    """Return a canonical HTTPS GitLab origin, rejecting unsafe URLs."""
    raw = value.strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "OMNIGENT_GITLAB_HOST must be an HTTPS origin without a path or credentials"
        )
    host = parsed.hostname.lower()
    netloc = host if parsed.port in (None, 443) else f"{host}:{parsed.port}"
    return urlunsplit(("https", netloc, "", "", ""))


def build_authorize_url(config: GitLabAppConfig, *, state: str) -> str:
    """Build the instance-specific GitLab OAuth authorization URL."""
    query = urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "scope": config.scopes,
            "state": state,
        }
    )
    return f"{config.host}/oauth/authorize?{query}"


def token_set_from_payload(payload: dict[object, object]) -> GitLabTokenSet:
    """Parse and validate a GitLab OAuth token response."""
    if payload.get("error"):
        raise GitLabAppError(
            f"GitLab token exchange failed: {payload.get('error_description', payload['error'])}"
        )
    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise GitLabAppError("GitLab token response missing access_token")
    expires_in = payload.get("expires_in")
    if expires_in is None:
        expires_at = None
    elif isinstance(expires_in, (str, int, float)):
        try:
            expires_at = int(time.time()) + int(expires_in)
        except (TypeError, ValueError) as exc:
            raise GitLabAppError("GitLab token response has invalid expires_in") from exc
    else:
        raise GitLabAppError("GitLab token response has invalid expires_in")
    scopes = payload.get("scope", "")
    return GitLabTokenSet(
        access_token=access_token,
        refresh_token=payload.get("refresh_token")
        if isinstance(payload.get("refresh_token"), str)
        else None,
        expires_at=expires_at,
        scopes=str(scopes),
    )
