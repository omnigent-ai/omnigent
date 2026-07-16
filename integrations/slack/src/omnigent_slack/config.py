from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    database_path: Path = Field(
        default=Path("data/omnigent_slack.sqlite3"),
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
