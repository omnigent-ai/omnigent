from __future__ import annotations

from pathlib import Path

import pytest
from omnigent_slack.config import ConfigError, Settings, load_settings
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
        "OMNIGENT_SLACK_SERVER_AUTH",
        "OMNIGENT_SLACK_DATABRICKS_STATE_SECRET",
        "OMNIGENT_SLACK_DATABRICKS_CLIENT_ID",
        "OMNIGENT_SLACK_DATABRICKS_CLIENT_SECRET",
        "OMNIGENT_SLACK_DATABRICKS_SCOPES",
        "OMNIGENT_SLACK_DATABRICKS_APP_URL",
        "DATABRICKS_HOST",
    ):
        monkeypatch.delenv(key, raising=False)
    # DATABRICKS_HOST is injected by the platform at runtime (a bare workspace
    # host); simulate that so databricks-mode Settings construct. Not a user
    # knob — the bot never lets it be overridden.
    env = {"DATABRICKS_HOST": "ws.cloud.databricks.com", **_REQUIRED, **overrides}
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_server_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, OMNIGENT_SERVER_URL="https://s.test/")
    assert _load().server_url == "https://s.test"


def test_server_url_rejects_bad_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, OMNIGENT_SERVER_URL="omnigent.test")
    with pytest.raises(ValidationError):
        _load()


def test_server_url_rejects_plaintext_http_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # The delegated bearer is sent to server_url on every request; plaintext would
    # leak it. Reject non-loopback http:// (mirrors the workspace-host rule).
    _set_env(monkeypatch, OMNIGENT_SERVER_URL="http://omnigent.example.com")
    with pytest.raises(ValidationError):
        _load()


def test_server_url_allows_http_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Loopback is exempt for local dev.
    _set_env(monkeypatch, OMNIGENT_SERVER_URL="http://127.0.0.1:8000")
    assert _load().server_url == "http://127.0.0.1:8000"


def test_server_url_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    monkeypatch.delenv("OMNIGENT_SERVER_URL", raising=False)
    with pytest.raises(ValidationError):
        _load()


def test_device_client_secret_optional_defaults_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    assert _load().device_client_secret is None


# ── load_settings(): operator-friendly config errors ─────────────────


def test_load_settings_missing_vars_raises_friendly_configerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing required vars → a ConfigError naming each env var + how to set
    them, NOT a raw pydantic ValidationError traceback."""
    _set_env(monkeypatch)  # baseline
    for key in _REQUIRED:  # then unset all three required vars
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    msg = str(excinfo.value)
    # Every required env var is named by its real (env) name…
    for name in _REQUIRED:
        assert name in msg
    # …and the message tells the operator how config is loaded.
    assert "does NOT load a .env" in msg
    assert "Missing required configuration" in msg
    # It must not be a bare pydantic dump.
    assert "validation error" not in msg.lower()


def test_load_settings_reports_only_the_missing_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """When only one required var is missing, only that one is listed."""
    _set_env(monkeypatch)
    monkeypatch.delenv("OMNIGENT_SERVER_URL", raising=False)

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    msg = str(excinfo.value)
    assert "OMNIGENT_SERVER_URL" in msg
    assert "OMNIGENT_SLACK_BOT_TOKEN" not in msg  # the ones that ARE set aren't flagged


def test_load_settings_invalid_value_raises_friendly_configerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that fails a validator (bad URL scheme) → a ConfigError under an
    'Invalid configuration' heading, not a missing-var message."""
    _set_env(monkeypatch, OMNIGENT_SERVER_URL="ftp://nope")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    msg = str(excinfo.value)
    assert "Invalid configuration" in msg
    assert "OMNIGENT_SERVER_URL" in msg
    assert "http://" in msg  # surfaces the validator's guidance


def test_load_settings_succeeds_with_full_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path returns a Settings instance (no error)."""
    _set_env(monkeypatch)
    settings = load_settings()
    assert settings.server_url == _REQUIRED["OMNIGENT_SERVER_URL"]


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


def test_server_auth_mode_defaults_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    assert _load().server_auth_mode == "auto"


_DATABRICKS_KNOBS = {
    "OMNIGENT_SLACK_SERVER_AUTH": "databricks",
    "OMNIGENT_SLACK_DATABRICKS_CLIENT_ID": "client-id",
    "OMNIGENT_SLACK_DATABRICKS_CLIENT_SECRET": "client-secret",
    "OMNIGENT_SLACK_DATABRICKS_STATE_SECRET": "state-secret-0123456789abcdef0123456789",
}


def test_databricks_mode_requires_oauth_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, OMNIGENT_SLACK_SERVER_AUTH="databricks")
    with pytest.raises(ValidationError):
        _load()


@pytest.mark.parametrize("omit", sorted(set(_DATABRICKS_KNOBS) - {"OMNIGENT_SLACK_SERVER_AUTH"}))
def test_databricks_mode_requires_each_oauth_knob(
    monkeypatch: pytest.MonkeyPatch, omit: str
) -> None:
    # Each of the OAuth knobs is mandatory in databricks mode — dropping any one
    # must fail fast at startup rather than at first enrollment.
    knobs = {k: v for k, v in _DATABRICKS_KNOBS.items() if k != omit}
    _set_env(monkeypatch, **knobs)
    with pytest.raises(ValidationError):
        _load()


def test_databricks_mode_valid_with_required_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, **_DATABRICKS_KNOBS)
    settings = _load()
    assert settings.server_auth_mode == "databricks"
    assert settings.databricks_oauth_client_id == "client-id"
    # The state HMAC key is its own required knob, distinct from the client secret.
    assert settings.databricks_state_secret == "state-secret-0123456789abcdef0123456789"
    assert settings.databricks_oauth_client_secret == "client-secret"


def test_databricks_mode_rejects_short_state_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # A weak state secret is brute-forceable → forgeable `state`. Require entropy.
    knobs = {**_DATABRICKS_KNOBS, "OMNIGENT_SLACK_DATABRICKS_STATE_SECRET": "too-short"}
    _set_env(monkeypatch, **knobs)
    with pytest.raises(ValidationError):
        _load()


def test_databricks_workspace_host_rejects_plaintext_http(monkeypatch: pytest.MonkeyPatch) -> None:
    # http:// defeats the TLS assumption behind skipping id_token verification.
    _set_env(
        monkeypatch,
        **{
            **_DATABRICKS_KNOBS,
            "DATABRICKS_HOST": "http://ws.databricks.com",
        },
    )
    with pytest.raises(ValidationError):
        _load()


def test_databricks_scopes_force_openid_and_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch, **_DATABRICKS_KNOBS, OMNIGENT_SLACK_DATABRICKS_SCOPES="supervisor-agents"
    )
    scopes = _load().databricks_oauth_scopes_normalized.split()
    assert "supervisor-agents" in scopes
    assert "openid" in scopes
    assert "offline_access" in scopes


def test_databricks_redirect_uri_reuses_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        **_DATABRICKS_KNOBS,
        OMNIGENT_SLACK_DATABRICKS_APP_URL="https://bot.example.com",
    )
    assert _load().databricks_redirect_uri == "https://bot.example.com/auth/callback"


def test_databricks_rejects_plaintext_app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The app URL is the OAuth redirect target (code + PII consent land there),
    # so it must be https for any non-loopback host — same rule as the workspace
    # host and server_url.
    _set_env(
        monkeypatch,
        **_DATABRICKS_KNOBS,
        OMNIGENT_SLACK_DATABRICKS_APP_URL="http://bot.example.com",
    )
    with pytest.raises(ValidationError):
        _load()


def test_databricks_allows_loopback_app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        **_DATABRICKS_KNOBS,
        OMNIGENT_SLACK_DATABRICKS_APP_URL="http://localhost:8000",
    )
    assert _load().databricks_redirect_uri == "http://localhost:8000/auth/callback"


def test_databricks_valid_without_app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Legitimately absent on the first deploy (app URL doesn't exist yet); that's
    # not a plaintext leak, just "no enrollment link yet".
    _set_env(monkeypatch, **_DATABRICKS_KNOBS)
    settings = _load()
    assert settings.webauth_base_url is None
    assert settings.databricks_redirect_uri is None


def test_app_url_trailing_slash_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The operator supplies the app URL; the enrollment-link base trims a
    # trailing slash so the redirect URI joins cleanly.
    _set_env(
        monkeypatch,
        **_DATABRICKS_KNOBS,
        OMNIGENT_SLACK_DATABRICKS_APP_URL="https://bot.example.com/",
    )
    assert _load().webauth_base_url == "https://bot.example.com"


def test_webauth_port_defaults_to_8000(monkeypatch: pytest.MonkeyPatch) -> None:
    # Laptop run without the platform var: fall back to the 8000 convention.
    _set_env(monkeypatch)
    assert _load().databricks_webauth_port == 8000
