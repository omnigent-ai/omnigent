from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @field_validator("server_url")
    @classmethod
    def _normalize_server_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("OMNIGENT_SERVER_URL must start with http:// or https://")
        return value


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
