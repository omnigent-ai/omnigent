from __future__ import annotations

from pathlib import Path

import pytest
from omnigent_discord.config import ConfigError, Settings, load_settings

_MINIMAL = {
    "OMNIGENT_DISCORD_BOT_TOKEN": "tok",
    "OMNIGENT_SERVER_URL": "https://omnigent.example.com",
}


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    """Set exactly the given environment, clearing every other bot variable."""
    for name in (
        "OMNIGENT_DISCORD_BOT_TOKEN",
        "OMNIGENT_SERVER_URL",
        "OMNIGENT_DISCORD_GUILD_IDS",
        "OMNIGENT_DISCORD_COMMAND_GUILD_IDS",
        "OMNIGENT_DISCORD_DATABASE_PATH",
        "OMNIGENT_DISCORD_TOKEN_ENCRYPTION_KEY",
        "OMNIGENT_DISCORD_STREAM_EDIT_INTERVAL",
        "OMNIGENT_DEVICE_CLIENT_SECRET",
        "OMNIGENT_DATA_DIR",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in {**_MINIMAL, **overrides}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def test_minimal_config_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    settings = load_settings()
    assert settings.bot_token == "tok"
    assert settings.server_url == "https://omnigent.example.com"


def test_missing_required_vars_are_named_in_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OMNIGENT_DISCORD_BOT_TOKEN=None, OMNIGENT_SERVER_URL=None)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "OMNIGENT_DISCORD_BOT_TOKEN" in message
    assert "OMNIGENT_SERVER_URL" in message
    # The error must be printable guidance, not a pydantic dump.
    assert "validation error" not in message.lower()


def test_server_url_trailing_slash_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OMNIGENT_SERVER_URL="https://omnigent.example.com/")
    assert load_settings().server_url == "https://omnigent.example.com"


def test_plaintext_server_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # The per-user delegated bearer rides on every request to this host.
    _env(monkeypatch, OMNIGENT_SERVER_URL="http://omnigent.example.com")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "https" in str(excinfo.value)


def test_plaintext_loopback_is_allowed_for_local_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OMNIGENT_SERVER_URL="http://localhost:8000")
    assert load_settings().server_url == "http://localhost:8000"


def test_scheme_less_server_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OMNIGENT_SERVER_URL="omnigent.example.com")
    with pytest.raises(ConfigError):
        load_settings()


def test_guild_ids_parse_from_commas_or_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, OMNIGENT_DISCORD_GUILD_IDS="1, 2 3,,4")
    assert load_settings().allowed_guild_ids == frozenset({"1", "2", "3", "4"})


def test_empty_allow_list_permits_every_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    settings = load_settings()
    assert settings.guild_allowed("anything") is True
    assert settings.guild_allowed(None) is True


def test_allow_list_blocks_other_guilds_but_never_dms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, OMNIGENT_DISCORD_GUILD_IDS="900")
    settings = load_settings()
    assert settings.guild_allowed("900") is True
    assert settings.guild_allowed("901") is False
    # A DM carries no guild and is reachable only by someone who shares a guild
    # with the bot, so it is never filtered here.
    assert settings.guild_allowed(None) is True


def test_allow_list_does_not_scope_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # Registering the command per-guild makes it DM-blind, and the allow-list is
    # a security control with an unrelated purpose — so setting one must not
    # silently disable /omnigent in DMs.
    _env(monkeypatch, OMNIGENT_DISCORD_GUILD_IDS="900 901")
    assert load_settings().command_guild_ids == frozenset()


def test_command_guilds_are_an_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(
        monkeypatch,
        OMNIGENT_DISCORD_GUILD_IDS="900",
        OMNIGENT_DISCORD_COMMAND_GUILD_IDS="777",
    )
    assert load_settings().command_guild_ids == frozenset({"777"})


def test_database_path_honors_the_shared_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    assert load_settings().database_path == tmp_path / "omnigent_discord.sqlite3"


def test_out_of_range_edit_interval_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    # Too fast a cadence burns the per-channel edit budget and stalls the turn.
    _env(monkeypatch, OMNIGENT_DISCORD_STREAM_EDIT_INTERVAL="0.01")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "OMNIGENT_DISCORD_STREAM_EDIT_INTERVAL" in str(excinfo.value)


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.setenv("OMNIGENT_DISCORD_SOMETHING_ELSE", "x")
    assert isinstance(load_settings(), Settings)
