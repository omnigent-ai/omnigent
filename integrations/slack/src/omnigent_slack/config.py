from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Auth posture the bot assumes for its Omnigent server. ``auto`` probes the
# server (the historical behaviour — device grant / OIDC ticket). ``databricks``
# is for a server fronted by the Databricks Apps proxy (header mode), which the
# probe can't drive: identity is asserted by the proxy. The bot runs its own
# Databricks U2M OAuth client (authorization code + PKCE, ``offline_access``) so
# each user signs in once and gets a durable, refreshable token the bot forwards
# to the server. See ``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``.
ServerAuthMode = Literal["auto", "databricks"]

# Minimum length for the enrollment-state HMAC secret. 32 chars is a floor
# against offline brute-forcing a weak operator value (which would let an
# attacker forge a signed `state`); `openssl rand -hex 32` yields 64.
_MIN_STATE_SECRET_LEN = 32


def _normalize_host(value: str | None) -> str | None:
    """Normalize a workspace host: strip trailing slash, add ``https://`` scheme.

    ``DATABRICKS_HOST`` (and often an operator-supplied value) is a bare host
    like ``my-workspace.cloud.databricks.com`` with no scheme; the OAuth
    endpoints need a full URL, so default a missing scheme to ``https://``.
    Returns ``None`` for an empty/absent value.
    """
    if value is None:
        return None
    value = value.strip().rstrip("/")
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"
    return value


def _is_loopback_url(url: str) -> bool:
    """Whether ``url``'s host is loopback (localhost / 127.0.0.1 / ::1).

    Used to allow a plaintext ``http://`` workspace host for local testing only,
    while requiring https for any real host.
    """
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def _normalize_oauth_scopes(raw: str) -> str:
    """Normalize an OAuth scope string, forcing the scopes the flow needs.

    ``offline_access`` is required for a refresh token (the whole point of the
    U2M flow — without it the token expires in ~1h and the user re-enrolls), and
    ``openid`` is required for the ``id_token`` the bot reads the user's email
    from. Both are added if the operator's scope string omits them, so a narrow
    custom scope still yields a refreshable, identity-bearing token.
    """
    scopes = [s for s in raw.split() if s]
    for required in ("openid", "offline_access"):
        if required not in scopes:
            scopes.append(required)
    return " ".join(scopes)


def _local_data_dir() -> Path:
    """Return the local runtime data dir for the bot's SQLite store.

    Honors ``OMNIGENT_DATA_DIR`` (the shared data-isolation knob, so a
    checkout/worktree keeps its own state), else ``~/.omnigent``. Kept as a
    local copy rather than an import so the standalone ``omnigent-slack``
    package stays decoupled from omnigent core.

    :returns: The data directory path (callers create it lazily).
    """
    value = os.environ.get("OMNIGENT_DATA_DIR")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".omnigent"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    slack_bot_token: str = Field(validation_alias="OMNIGENT_SLACK_BOT_TOKEN")
    slack_app_token: str = Field(validation_alias="OMNIGENT_SLACK_APP_TOKEN")

    # The one Omnigent server this bot talks to. Set by the operator, never
    # by a Slack user — so the bot only ever issues requests to this fixed
    # host (closes the SSRF vector a user-supplied URL would open). Every
    # user still authenticates as their own identity against it.
    server_url: str = Field(validation_alias="OMNIGENT_SERVER_URL")

    # Optional shared secret proving this socket server is an authorized
    # device-grant client. When the Omnigent server has
    # OMNIGENT_DEVICE_CLIENT_SECRET set, this must match; the bot sends it
    # in the X-Omnigent-Client-Secret header on device authorize/token/
    # revoke. Leave unset when the server doesn't require it.
    device_client_secret: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_DEVICE_CLIENT_SECRET",
    )

    # Bot SQLite store (thread→session map, user configs, encrypted tokens).
    # Defaults under the runtime data dir (``OMNIGENT_DATA_DIR`` or
    # ``~/.omnigent``) so the daemon doesn't depend on its launch cwd — set
    # OMNIGENT_SLACK_DATABASE_PATH to override.
    database_path: Path = Field(
        default_factory=lambda: _local_data_dir() / "omnigent_slack.sqlite3",
        validation_alias="OMNIGENT_SLACK_DATABASE_PATH",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # Fernet key (urlsafe-base64, 32 bytes) that encrypts the delegated
    # Omnigent access/refresh tokens at rest in the local SQLite store.
    # Generate with ``python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"``. Set this so a stolen
    # database file cannot be used to impersonate users — see
    # designs/DEVICE_AUTH.md. If unset, tokens are kept in memory
    # only (never written to disk) and lost on restart, so users
    # re-authenticate; the integration still works either way.
    token_encryption_key: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SLACK_TOKEN_ENCRYPTION_KEY",
    )

    # ── Databricks Apps web-auth (header/proxy-mode servers) ──────────────
    #
    # When the Omnigent server is deployed as a Databricks App, its proxy
    # asserts identity via a header the bot can't produce from a Socket-Mode
    # event. Set OMNIGENT_SLACK_SERVER_AUTH=databricks and register a custom U2M
    # OAuth app (authorization code + PKCE) in the workspace: the bot runs that
    # OAuth flow through a web page it serves as its own Databricks App, so each
    # user gets a durable, refreshable token. See
    # docs/DATABRICKS_APP_WEBAUTH_DESIGN.md.
    server_auth_mode: ServerAuthMode = Field(
        default="auto",
        validation_alias="OMNIGENT_SLACK_SERVER_AUTH",
    )

    # Databricks workspace host the custom U2M OAuth app is registered in, e.g.
    # ``https://my-workspace.cloud.databricks.com``. The bot hits its
    # ``/oidc/v1/authorize`` and ``/oidc/v1/token`` endpoints. Distinct from
    # server_url (the *.databricksapps.com app). Defaults to the platform-
    # injected DATABRICKS_HOST when unset (the OAuth app lives in the same
    # workspace the bot runs in); required — directly or via DATABRICKS_HOST —
    # in databricks mode.
    databricks_workspace_host: str | None = Field(
        default_factory=lambda: _normalize_host(os.environ.get("DATABRICKS_HOST")),
        validation_alias="OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST",
    )

    # Custom U2M OAuth app credentials (client id + secret) registered in the
    # workspace above. The client id is public; the secret authenticates the
    # token/refresh calls. Both required in databricks mode.
    databricks_oauth_client_id: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SLACK_DATABRICKS_CLIENT_ID",
    )
    databricks_oauth_client_secret: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SLACK_DATABRICKS_CLIENT_SECRET",
    )

    # HMAC key (any long random string) that signs the enrollment ``state``.
    # Kept separate from the OAuth client secret on purpose: rotating the OAuth
    # credential in Databricks then doesn't invalidate in-flight enrollment
    # links, and the state-signing key stays out of the OAuth-credential blast
    # radius. Required in databricks mode.
    databricks_state_secret: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SLACK_DATABRICKS_STATE_SECRET",
    )

    # Space-separated OAuth scopes to request. ``openid`` and ``offline_access``
    # are forced on (see _normalize_oauth_scopes) so the flow always yields an
    # id_token + refresh token. The token's scopes must be a SUPERSET of the
    # scopes the target server app declares, or its Databricks proxy rejects the
    # token (401 on /api, 302→login elsewhere). ``all-apis`` satisfies any app's
    # requirement (the same default a ``databricks-cli`` token carries); narrow it
    # only to the exact scope the server app declares once that's known.
    databricks_oauth_scopes: str = Field(
        default="all-apis",
        validation_alias="OMNIGENT_SLACK_DATABRICKS_SCOPES",
    )

    # Public base URL of this bot's own Databricks App (where the enrollment
    # page is reachable), used to build the link posted into Slack. Defaults to
    # DATABRICKS_APP_URL when the platform injects it.
    databricks_webauth_base_url: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SLACK_WEBAUTH_BASE_URL",
    )

    # Port the enrollment web server binds. Databricks Apps route to
    # DATABRICKS_APP_PORT (8000 by convention); honour it by default.
    databricks_webauth_port: int = Field(
        default_factory=lambda: int(os.environ.get("DATABRICKS_APP_PORT", "8000")),
        validation_alias="OMNIGENT_SLACK_WEBAUTH_PORT",
    )

    @field_validator("server_url")
    @classmethod
    def _normalize_server_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("OMNIGENT_SERVER_URL must start with http:// or https://")
        # The per-user delegated bearer is sent to this host on every request
        # (see omnigent.py). Plaintext http:// would transmit that credential in
        # the clear, so reject it for any non-loopback host — same rule the
        # Databricks workspace host uses. Loopback stays allowed for local dev.
        if value.startswith("http://") and not _is_loopback_url(value):
            raise ValueError(
                "OMNIGENT_SERVER_URL must use https:// (plaintext would leak the "
                "delegated bearer token); http:// is allowed only for loopback"
            )
        return value

    @property
    def webauth_base_url(self) -> str | None:
        """Public base URL of this bot's enrollment page (for the Slack link)."""
        base = self.databricks_webauth_base_url or os.environ.get("DATABRICKS_APP_URL")
        return base.strip().rstrip("/") if base else None

    @property
    def databricks_redirect_uri(self) -> str | None:
        """OAuth redirect URI the authorize call sends the ``?code=`` back to.

        The custom OAuth app must register this exact value. It reuses the
        enrollment page's ``/auth/callback`` route on the bot's own Databricks
        App URL. ``None`` when the base URL isn't configured yet.
        """
        base = self.webauth_base_url
        return f"{base}/auth/callback" if base else None

    @property
    def databricks_oauth_scopes_normalized(self) -> str:
        """Requested scopes with ``openid`` + ``offline_access`` forced on."""
        return _normalize_oauth_scopes(self.databricks_oauth_scopes)

    @field_validator("databricks_workspace_host")
    @classmethod
    def _normalize_workspace_host(cls, value: str | None) -> str | None:
        # A scheme-less host (e.g. DATABRICKS_HOST, or an operator typing just the
        # hostname) is defaulted to https. The model validator then enforces https
        # for any non-loopback host.
        return _normalize_host(value)

    @model_validator(mode="after")
    def _check_databricks_config(self) -> Settings:
        """Fail fast when databricks mode is missing required config.

        Catches misconfiguration at startup rather than at first enrollment,
        where a Slack user would just see a generic failure.
        """
        if self.server_auth_mode != "databricks":
            return self
        missing = [
            name
            for name, value in (
                ("OMNIGENT_SLACK_DATABRICKS_CLIENT_ID", self.databricks_oauth_client_id),
                ("OMNIGENT_SLACK_DATABRICKS_CLIENT_SECRET", self.databricks_oauth_client_secret),
                ("OMNIGENT_SLACK_DATABRICKS_STATE_SECRET", self.databricks_state_secret),
                # workspace_host defaults to DATABRICKS_HOST (injected on the
                # platform); still required for a laptop run where it's unset.
                ("OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST", self.databricks_workspace_host),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "OMNIGENT_SLACK_SERVER_AUTH=databricks requires " + ", ".join(missing)
            )
        # The OAuth flow's security rests on TLS: the client secret rides HTTP
        # Basic on the token call, and the id_token (the confused-deputy anchor)
        # is trusted WITHOUT signature verification because it arrives directly
        # over TLS from the token endpoint (see databricks_oauth._email_from_id_token
        # — this check is what lets it skip JWKS verification). A plaintext
        # workspace host defeats both, so require https (loopback excepted for
        # local testing).
        host = self.databricks_workspace_host or ""
        if host.startswith("http://") and not _is_loopback_url(host):
            raise ValueError(
                "OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST must use https:// "
                "(plaintext exposes the client secret and lets an on-path "
                "attacker forge the id_token identity)"
            )
        # The web-auth base URL is the OAuth redirect target — where the ``?code=``
        # and the PII consent page land — so it must be TLS too, same rule as the
        # workspace host. Only checked when set: it's legitimately absent on the
        # first deploy (the app URL doesn't exist yet), which just means no
        # enrollment link is issued — not a plaintext leak.
        base = self.webauth_base_url or ""
        if base.startswith("http://") and not _is_loopback_url(base):
            raise ValueError(
                "OMNIGENT_SLACK_WEBAUTH_BASE_URL must use https:// "
                "(it is the OAuth redirect target — plaintext would expose the "
                "authorization code and the consent page's identity data)"
            )
        # The state secret is the HMAC key protecting the enrollment `state`; a
        # weak value is offline-brute-forceable from one legitimately-signed state,
        # after which an attacker can forge a state binding a victim's Slack id to
        # the attacker's email (identity corruption). Require real entropy.
        if len(self.databricks_state_secret or "") < _MIN_STATE_SECRET_LEN:
            raise ValueError(
                f"OMNIGENT_SLACK_DATABRICKS_STATE_SECRET must be at least "
                f"{_MIN_STATE_SECRET_LEN} characters (use e.g. `openssl rand -hex 32`)"
            )
        return self


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
