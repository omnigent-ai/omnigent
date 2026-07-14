from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    slack_bot_token: str = Field(validation_alias="SLACK_BOT_TOKEN")
    slack_app_token: str = Field(validation_alias="SLACK_APP_TOKEN")
    omnigent_agent_name: str = Field(validation_alias="OMNIGENT_AGENT_NAME")

    omnigent_base_url: str = Field(
        default="http://127.0.0.1:6767",
        validation_alias="OMNIGENT_BASE_URL",
    )
    omnigent_auth_email: str | None = Field(default=None, validation_alias="OMNIGENT_AUTH_EMAIL")
    omnigent_auth_header_name: str = Field(
        default="X-Forwarded-Email",
        validation_alias="OMNIGENT_AUTH_HEADER_NAME",
    )
    omnigent_session_cookie: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_SESSION_COOKIE",
    )
    omnigent_runner_workspace: str = Field(
        default_factory=lambda: str(Path.cwd()),
        validation_alias="OMNIGENT_RUNNER_WORKSPACE",
    )
    omnigent_runner_host_id: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_RUNNER_HOST_ID",
    )
    omnigent_runner_launch_timeout_seconds: float = Field(
        default=60.0,
        ge=1.0,
        validation_alias="OMNIGENT_RUNNER_LAUNCH_TIMEOUT_SECONDS",
    )

    database_path: Path = Field(
        default=Path("data/omnigent_slack.sqlite3"),
        validation_alias="OMNIGENT_SLACK_DATABASE_PATH",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    slack_update_interval_seconds: float = Field(
        default=1.0,
        ge=0.0,
        validation_alias="SLACK_UPDATE_INTERVAL_SECONDS",
    )


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
