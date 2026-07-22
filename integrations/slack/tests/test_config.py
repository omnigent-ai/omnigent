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
        "OMNIGENT_SLACK_SERVER_AUTH",
        "OMNIGENT_SLACK_DATABRICKS_STATE_SECRET",
        "OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST",
        "OMNIGENT_SLACK_DATABRICKS_CLIENT_ID",
        "OMNIGENT_SLACK_DATABRICKS_CLIENT_SECRET",
        "OMNIGENT_SLACK_DATABRICKS_SCOPES",
        "OMNIGENT_SLACK_WEBAUTH_BASE_URL",
        "OMNIGENT_SLACK_WEBAUTH_PORT",
        "DATABRICKS_APP_URL",
        "DATABRICKS_APP_PORT",
        "DATABRICKS_HOST",
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
    "OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST": "https://ws.cloud.databricks.com",
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
    assert settings.databricks_workspace_host == "https://ws.cloud.databricks.com"
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


def test_databricks_workspace_host_defaults_to_databricks_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On the platform the workspace host is injected as DATABRICKS_HOST — a bare
    # host with no scheme — so the operator needn't set it explicitly, and it's
    # normalized to https.
    knobs = {k: v for k, v in _DATABRICKS_KNOBS.items() if "WORKSPACE_HOST" not in k}
    _set_env(monkeypatch, **knobs)
    monkeypatch.setenv("DATABRICKS_HOST", "ws.cloud.databricks.com")
    assert _load().databricks_workspace_host == "https://ws.cloud.databricks.com"


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
        OMNIGENT_SLACK_WEBAUTH_BASE_URL="https://bot.example.com",
    )
    assert _load().databricks_redirect_uri == "https://bot.example.com/auth/callback"


def test_databricks_rejects_plaintext_webauth_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The web-auth base URL is the OAuth redirect target (code + PII consent land
    # there), so it must be https for any non-loopback host — same rule as the
    # workspace host and server_url.
    _set_env(
        monkeypatch,
        **_DATABRICKS_KNOBS,
        OMNIGENT_SLACK_WEBAUTH_BASE_URL="http://bot.example.com",
    )
    with pytest.raises(ValidationError):
        _load()


def test_databricks_allows_loopback_webauth_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(
        monkeypatch,
        **_DATABRICKS_KNOBS,
        OMNIGENT_SLACK_WEBAUTH_BASE_URL="http://localhost:8000",
    )
    assert _load().databricks_redirect_uri == "http://localhost:8000/auth/callback"


def test_databricks_valid_without_webauth_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Legitimately absent on the first deploy (app URL doesn't exist yet); that's
    # not a plaintext leak, just "no enrollment link yet".
    monkeypatch.delenv("DATABRICKS_APP_URL", raising=False)
    _set_env(monkeypatch, **_DATABRICKS_KNOBS)
    settings = _load()
    assert settings.webauth_base_url is None
    assert settings.databricks_redirect_uri is None


def test_databricks_workspace_host_defaults_scheme_to_https(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A scheme-less host (DATABRICKS_HOST returns e.g. "ws.databricks.com") is
    # normalized to https rather than rejected.
    _set_env(
        monkeypatch,
        **{**_DATABRICKS_KNOBS, "OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST": "ws.databricks.com"},
    )
    assert _load().databricks_workspace_host == "https://ws.databricks.com"


def test_databricks_workspace_host_rejects_plaintext_http(monkeypatch: pytest.MonkeyPatch) -> None:
    # http:// defeats the TLS assumption behind skipping id_token verification.
    _set_env(
        monkeypatch,
        **{
            **_DATABRICKS_KNOBS,
            "OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST": "http://ws.databricks.com",
        },
    )
    with pytest.raises(ValidationError):
        _load()


def test_databricks_workspace_host_allows_http_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Loopback is exempt so local testing against a fake token endpoint works.
    _set_env(
        monkeypatch,
        **{
            **_DATABRICKS_KNOBS,
            "OMNIGENT_SLACK_DATABRICKS_WORKSPACE_HOST": "http://127.0.0.1:8080",
        },
    )
    assert _load().databricks_workspace_host == "http://127.0.0.1:8080"


def test_webauth_base_url_falls_back_to_app_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # The platform injects DATABRICKS_APP_URL; the enrollment-link base uses it
    # when not set explicitly (trailing slash trimmed).
    _set_env(monkeypatch)
    monkeypatch.setenv("DATABRICKS_APP_URL", "https://bot.example.com/")
    assert _load().webauth_base_url == "https://bot.example.com"


def test_webauth_port_defaults_to_databricks_app_port(monkeypatch: pytest.MonkeyPatch) -> None:
    # Databricks Apps route to DATABRICKS_APP_PORT; the web server binds it.
    _set_env(monkeypatch)
    monkeypatch.setenv("DATABRICKS_APP_PORT", "9001")
    assert _load().databricks_webauth_port == 9001
