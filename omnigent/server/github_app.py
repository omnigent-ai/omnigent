"""GitHub App configuration and credential handling.

Implements the config half of the *GitHub App* (not classic OAuth App)
integration that lets a user connect their GitHub account from the web
UI and have their managed sandboxes authenticate ``gh`` / git as them.
See ``docs/GITHUB_APP_SETUP.md``.

This module deliberately owns everything that *touches the App's
secrets* — reading them from env, minting the app JWT, and building the
OAuth token-request form fields — but makes **no network calls**. The
HTTP client that sends these to GitHub lives in
:mod:`omnigent.server.github_app_client`, so secret material and the
network sink never sit in the same module.

Two credential shapes are involved:

* **App JWT** — a short-lived RS256 token signed with the App's private
  key (``iss = app_id``). Authenticates *as the app*; only needed for
  app-level API calls (not required for the per-user connect flow).
* **User access token** (``ghu_…``) — obtained through the user
  authorization web flow using the App's client id / secret. Acts *as
  the connecting user*; this is what we inject into the sandbox.

The authorize / token endpoints are the same GitHub OAuth endpoints an
OAuth App uses, but the credentials belong to the App.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import jwt

_logger = logging.getLogger(__name__)

_AUTHORIZE_ENDPOINT = "https://github.com/login/oauth/authorize"

# App JWTs are accepted for up to 10 minutes by GitHub; use a small skew
# on the issued-at to tolerate minor clock drift between us and GitHub.
_APP_JWT_TTL_S = 540
_APP_JWT_CLOCK_SKEW_S = 30


@dataclass(frozen=True)
class GitHubTokenSet:
    """Result of a code exchange or refresh.

    :param access_token: The user access token (``ghu_…``).
    :param refresh_token: The refresh token (``ghr_…``), or ``None`` when
        the App issues non-expiring user tokens.
    :param expires_at: Unix epoch seconds the access token expires at, or
        ``None`` for non-expiring tokens.
    :param refresh_token_expires_at: Unix epoch seconds the refresh token
        expires at, or ``None``.
    :param scopes: Space-separated granted scopes reported by GitHub
        (usually empty for Apps — permissions are set on the App).
    """

    access_token: str
    refresh_token: str | None
    expires_at: int | None
    refresh_token_expires_at: int | None
    scopes: str


@dataclass(frozen=True)
class GitHubAppConfig:
    """Validated GitHub App configuration.

    Built once at startup via :meth:`from_env`. When required env is
    absent, :meth:`from_env` returns ``None`` and the whole feature stays
    dormant (the connect UI is hidden, sandboxes keep the shared
    ``GIT_TOKEN`` behaviour).

    :param app_id: Numeric App ID, or ``None`` (only needed for the app
        JWT / app-level calls).
    :param client_id: App client id used for the user authorization flow.
    :param client_secret: App client secret.
    :param private_key: RSA private key PEM for the app JWT, or ``None``.
    :param redirect_uri: OAuth callback URL registered on the App.
    :param slug: App slug used to build the ``install_url``, or ``None``.

    Token-at-rest encryption is not configured here: stored tokens are
    encrypted by the shared credential store's cipher
    (``OMNIGENT_CREDENTIAL_ENC_KEY``), not by any GitHub-specific key.
    """

    app_id: str | None
    client_id: str
    client_secret: str
    private_key: str | None
    redirect_uri: str
    slug: str | None

    @property
    def install_url(self) -> str | None:
        """The App's public installation URL, or ``None`` when no slug."""
        if not self.slug:
            return None
        return f"https://github.com/apps/{self.slug}/installations/new"

    def mint_app_jwt(self) -> str:
        """Mint a short-lived RS256 app JWT signed with the private key.

        Kept here (not on the network client) so the private key never
        leaves the secret-owning module.

        :returns: A signed JWT for app-level GitHub API calls.
        :raises RuntimeError: When no app id / private key is configured.
        """
        if not self.app_id or not self.private_key:
            raise RuntimeError("app JWT requires OMNIGENT_GITHUB_APP_ID and a private key")
        now = int(time.time())
        payload = {
            "iat": now - _APP_JWT_CLOCK_SKEW_S,
            "exp": now + _APP_JWT_TTL_S,
            "iss": self.app_id,
        }
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def code_exchange_fields(self, code: str) -> dict[str, str]:
        """Form fields for exchanging an authorization code for a token.

        The App credentials are assembled here so the network client can
        POST them without ever naming a secret.

        :param code: The ``code`` GitHub returned to the callback.
        :returns: The ``application/x-www-form-urlencoded`` field mapping.
        """
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,
        }

    def token_refresh_fields(self, refresh_token: str) -> dict[str, str]:
        """Form fields for refreshing a user access token.

        :param refresh_token: The stored ``ghr_…`` refresh token.
        :returns: The ``application/x-www-form-urlencoded`` field mapping.
        """
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

    @staticmethod
    def from_env() -> GitHubAppConfig | None:
        """Build config from ``OMNIGENT_GITHUB_APP_*`` env, or ``None``.

        The feature requires a client id, a client secret, and a
        resolvable redirect URI (explicit, or derived from
        ``OMNIGENT_DOMAIN``). Missing any of these disables it. Token-at-rest
        encryption is the credential store's concern
        (``OMNIGENT_CREDENTIAL_ENC_KEY``), not GitHub's — the caller only
        wires a connection store when that key is present.

        :returns: A validated config, or ``None`` when GitHub App
            integration is not configured.
        :raises RuntimeError: When a private key path is set but
            unreadable — a misconfiguration the operator should fix
            rather than silently run without app-level calls.
        """
        client_id = os.environ.get("OMNIGENT_GITHUB_APP_CLIENT_ID", "").strip()
        client_secret = os.environ.get("OMNIGENT_GITHUB_APP_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            return None

        redirect_uri = os.environ.get("OMNIGENT_GITHUB_APP_REDIRECT_URI", "").strip()
        if not redirect_uri:
            domain = os.environ.get("OMNIGENT_DOMAIN", "").strip()
            if not domain:
                _logger.warning(
                    "GitHub App client id/secret are set but neither "
                    "OMNIGENT_GITHUB_APP_REDIRECT_URI nor OMNIGENT_DOMAIN is — "
                    "GitHub App integration stays disabled."
                )
                return None
            redirect_uri = f"https://{domain}/v1/connections/github/callback"

        private_key = os.environ.get("OMNIGENT_GITHUB_APP_PRIVATE_KEY", "").strip() or None
        if private_key is None:
            key_path = os.environ.get("OMNIGENT_GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
            if key_path:
                try:
                    private_key = _read_text(key_path)
                except OSError as exc:
                    raise RuntimeError(
                        f"OMNIGENT_GITHUB_APP_PRIVATE_KEY_PATH={key_path!r} is unreadable: {exc}"
                    ) from exc

        return GitHubAppConfig(
            app_id=os.environ.get("OMNIGENT_GITHUB_APP_ID", "").strip() or None,
            client_id=client_id,
            client_secret=client_secret,
            private_key=private_key,
            redirect_uri=redirect_uri,
            slug=os.environ.get("OMNIGENT_GITHUB_APP_SLUG", "").strip() or None,
        )


def _read_text(path: str) -> str:
    """Read a text file (small helper isolated for test monkeypatching)."""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def build_authorize_url(config: GitHubAppConfig, *, state: str) -> str:
    """Build the GitHub user-authorization URL to redirect the user to.

    GitHub Apps take no ``scope`` parameter — permissions are configured
    on the App itself — so this only carries the client id, redirect, and
    the signed ``state``.

    :param config: The GitHub App config.
    :param state: An opaque, signed state string (see the routes module).
    :returns: The full ``https://github.com/login/oauth/authorize?…`` URL.
    """
    from urllib.parse import urlencode

    params = {
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "state": state,
    }
    return f"{_AUTHORIZE_ENDPOINT}?{urlencode(params)}"


def token_set_from_payload(payload: dict) -> GitHubTokenSet:
    """Parse a GitHub token-endpoint JSON payload into a :class:`GitHubTokenSet`.

    Shared by the network client so response parsing (which names the
    ``access_token`` / ``refresh_token`` OAuth fields) stays out of the
    HTTP module and next to the token type it produces.

    :param payload: The decoded JSON body from the token endpoint.
    :returns: The parsed token set.
    :raises GitHubAppError: When the payload carries an ``error`` or lacks
        an access token.
    """
    if "error" in payload:
        detail = payload.get("error_description", payload["error"])
        raise GitHubAppError(f"GitHub token exchange failed: {detail}")
    access = payload.get("access_token")
    if not access:
        raise GitHubAppError("GitHub token response missing access_token")
    now = int(time.time())
    expires_in = payload.get("expires_in")
    refresh_expires_in = payload.get("refresh_token_expires_in")
    return GitHubTokenSet(
        access_token=str(access),
        refresh_token=payload.get("refresh_token") or None,
        expires_at=now + int(expires_in) if expires_in else None,
        refresh_token_expires_at=(now + int(refresh_expires_in) if refresh_expires_in else None),
        scopes=str(payload.get("scope", "")),
    )


class GitHubAppError(Exception):
    """Raised when a GitHub App API interaction fails."""
