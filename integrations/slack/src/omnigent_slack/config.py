from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Auth posture the bot assumes for its Omnigent server. ``auto`` probes the
# server (the historical behaviour — device grant / OIDC ticket). ``databricks``
# is for a server fronted by the Databricks Apps proxy (header mode), which the
# probe can't drive: identity is asserted by the proxy, so the bot enrolls each
# user through a web page it hosts as its own Databricks App and exchanges the
# user's forwarded token for one scoped to the target app. See
# ``docs/DATABRICKS_APP_WEBAUTH_DESIGN.md``.
ServerAuthMode = Literal["auto", "databricks"]

# Default OAuth token-exchange knobs for the Databricks web-auth flow. The
# subject is the user's forwarded access token; the exchange returns a token
# whose audience is the target app, so it can't call other Databricks APIs.
_DEFAULT_EXCHANGE_SCOPE = "all-apis"
_DEFAULT_SUBJECT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


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
    # event. Set OMNIGENT_SLACK_SERVER_AUTH=databricks to enroll each user
    # through a web page this bot serves as its own Databricks App. See
    # docs/DATABRICKS_APP_WEBAUTH_DESIGN.md.
    server_auth_mode: ServerAuthMode = Field(
        default="auto",
        validation_alias="OMNIGENT_SLACK_SERVER_AUTH",
    )

    # oauth2_app_client_id of the target Omnigent app (audience of the token
    # exchange). Fetch with ``w.apps.get("<app>").oauth2_app_client_id``. The
    # exchanged token is scoped to this app and can't call other Databricks
    # APIs — the property that makes storing it least-privilege. Required when
    # server_auth_mode == "databricks".
    databricks_target_audience: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SLACK_DATABRICKS_AUDIENCE",
    )

    # Workspace base URL whose /oidc/v1/token endpoint performs the exchange.
    # Defaults to DATABRICKS_HOST (injected into every Databricks App), so it
    # normally needs no explicit value.
    databricks_workspace_host: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST",
    )

    # HMAC key (any non-empty string) that signs the ``state`` binding a browser
    # enrollment session to the Slack (team, user) that requested it. Prevents a
    # user from enrolling someone else's Slack identity. Required when
    # server_auth_mode == "databricks".
    databricks_state_secret: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SLACK_DATABRICKS_STATE_SECRET",
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
        return value

    @property
    def workspace_host(self) -> str | None:
        """Workspace base URL for the token exchange, honouring DATABRICKS_HOST.

        Explicit config wins; else the Databricks-App-injected ``DATABRICKS_HOST``
        (normalised to include a scheme). ``None`` when neither is available.
        """
        host = self.databricks_workspace_host or os.environ.get("DATABRICKS_HOST")
        if not host:
            return None
        host = host.strip().rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        return host

    @property
    def webauth_base_url(self) -> str | None:
        """Public base URL of this bot's enrollment page (for the Slack link)."""
        base = self.databricks_webauth_base_url or os.environ.get("DATABRICKS_APP_URL")
        return base.strip().rstrip("/") if base else None

    @property
    def exchange_scope(self) -> str:
        return _DEFAULT_EXCHANGE_SCOPE

    @property
    def subject_token_type(self) -> str:
        return _DEFAULT_SUBJECT_TOKEN_TYPE

    @model_validator(mode="after")
    def _check_databricks_config(self) -> Settings:
        """Fail fast when databricks mode is missing its required knobs.

        Catches misconfiguration at startup rather than at first enrollment,
        where a Slack user would just see a generic failure.
        """
        if self.server_auth_mode == "databricks":
            missing = [
                name
                for name, value in (
                    ("OMNIGENT_SLACK_DATABRICKS_AUDIENCE", self.databricks_target_audience),
                    ("OMNIGENT_SLACK_DATABRICKS_STATE_SECRET", self.databricks_state_secret),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "OMNIGENT_SLACK_SERVER_AUTH=databricks requires: " + ", ".join(missing)
                )
        return self


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
