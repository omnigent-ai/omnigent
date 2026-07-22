from __future__ import annotations

from pathlib import Path

import pytest
from omnigent_slack.config import Settings
from pydantic import ValidationError


def _load() -> Settings:
    # Ignore any developer .env on disk so tests exercise only the environment
    # we set via monkeypatch.
    return Settings(_env_file=None)  # type: ignore[call-arg]


_REQUIRED = {
    "OMNIGENT_SLACK_BOT_TOKEN": "xoxb-x",
    "OMNIGENT_SLACK_APP_TOKEN": "xapp-x",
    "OMNIGENT_SERVER_URL": "https://omnigent.example.com",
}


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    # Clear anything a developer's real .env / shell might inject, then set a
    # clean baseline plus the test's overrides.
    for key in (
        *_REQUIRED,
        "OMNIGENT_DEVICE_CLIENT_SECRET",
        "OMNIGENT_DATA_DIR",
        "OMNIGENT_SLACK_DATABASE_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    env = {**_REQUIRED, **overrides}
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_server_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, OMNIGENT_SERVER_URL="https://s.test/")
    assert _load().server_url == "https://s.test"


def test_server_url_rejects_bad_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, OMNIGENT_SERVER_URL="omnigent.test")
    with pytest.raises(ValidationError):
        _load()


def test_server_url_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("OMNIGENT_SERVER_URL", raising=False)
    with pytest.raises(ValidationError):
        _load()


def test_device_client_secret_optional_defaults_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    assert _load().device_client_secret is None


def test_device_client_secret_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, OMNIGENT_DEVICE_CLIENT_SECRET="sekret")
    assert _load().device_client_secret == "sekret"


def test_database_path_defaults_under_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With OMNIGENT_DATA_DIR set, the store defaults under it (not the cwd).
    _set_env(monkeypatch, OMNIGENT_DATA_DIR=str(tmp_path))
    assert _load().database_path == tmp_path / "omnigent_slack.sqlite3"


def test_database_path_defaults_under_home_when_no_data_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without OMNIGENT_DATA_DIR, it falls back to ~/.omnigent — never the cwd.
    _set_env(monkeypatch)
    assert _load().database_path == Path.home() / ".omnigent" / "omnigent_slack.sqlite3"


def test_database_path_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, OMNIGENT_SLACK_DATABASE_PATH="/custom/bot.sqlite3")
    assert _load().database_path == Path("/custom/bot.sqlite3")
