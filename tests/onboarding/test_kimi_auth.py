"""Tests for :mod:`omnigent.onboarding.kimi_auth`.

``kimi login`` writes ``~/.kimi-code/credentials/kimi-code.json`` (verified
against kimi CLI v0.29.1). Detection is a subprocess-free file check: a present,
non-empty file is a completed login; a missing or empty file is not. API key
users can also authenticate by configuring a Kimi provider with ``api_key`` or
``env.KIMI_API_KEY`` in ``~/.kimi-code/config.toml``.
"""

from __future__ import annotations

from pathlib import Path

from omnigent.onboarding import kimi_auth as ka


def test_credential_present_and_nonempty_detected(tmp_path: Path) -> None:
    """A present, non-empty credential file reads as a completed login."""
    creds = tmp_path / "kimi-code.json"
    creds.write_text('{"access_token": "sk-abc"}', encoding="utf-8")
    assert ka.kimi_login_detected(creds) is True


def test_credential_absent_not_detected(tmp_path: Path) -> None:
    """A missing credential file reads as not-logged-in."""
    assert ka.kimi_login_detected(tmp_path / "kimi-code.json") is False


def test_credential_empty_not_detected(tmp_path: Path) -> None:
    """An empty (zero-byte) credential file is not a completed login."""
    creds = tmp_path / "kimi-code.json"
    creds.write_text("", encoding="utf-8")
    assert ka.kimi_login_detected(creds) is False


def test_credential_directory_not_detected(tmp_path: Path) -> None:
    """A path that is a directory (not a regular file) is not a login."""
    creds = tmp_path / "kimi-code.json"
    creds.mkdir()
    assert ka.kimi_login_detected(creds) is False


def test_default_path_uses_home_credentials(monkeypatch, tmp_path: Path) -> None:
    """With no argument, detection checks the real ``~/.kimi-code`` location.

    Point ``HOME`` at a tmp dir and recompute the module default so the check is
    deterministic and never depends on the developer's real credential file.
    """
    fake_creds = tmp_path / ".kimi-code" / "credentials" / "kimi-code.json"
    fake_creds.parent.mkdir(parents=True, exist_ok=True)
    fake_creds.write_text('{"access_token": "sk-xyz"}', encoding="utf-8")
    monkeypatch.setattr(ka, "KIMI_CREDENTIALS_PATH", fake_creds)
    assert ka.kimi_login_detected() is True

    fake_creds.unlink()
    assert ka.kimi_login_detected() is False


def _write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_api_key_in_provider_detected(tmp_path: Path) -> None:
    """A Kimi provider with a non-empty ``api_key`` counts as configured."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.moonshot-ai]\ntype = "kimi"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is True


def test_api_key_in_provider_env_detected(tmp_path: Path) -> None:
    """A Kimi provider with ``env.KIMI_API_KEY`` counts as configured."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        """\
[providers.moonshot-ai]
type = "kimi"

[providers.moonshot-ai.env]
KIMI_API_KEY = "sk-abc123"
""",
    )
    assert ka.kimi_api_key_configured(config) is True


def test_non_kimi_provider_ignored(tmp_path: Path) -> None:
    """Providers whose ``type`` is not ``kimi`` do not count as configured."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.openai]\ntype = "openai"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is False


def test_empty_api_key_not_detected(tmp_path: Path) -> None:
    """An empty or whitespace-only ``api_key`` does not count as configured."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.moonshot-ai]\ntype = "kimi"\napi_key = "   "\n',
    )
    assert ka.kimi_api_key_configured(config) is False


def test_missing_config_not_detected(tmp_path: Path) -> None:
    """A missing config file means no API key is configured."""
    assert ka.kimi_api_key_configured(tmp_path / "config.toml") is False


def test_malformed_config_not_detected(tmp_path: Path) -> None:
    """A malformed config file is treated as "no API key" rather than raising."""
    config = tmp_path / "config.toml"
    _write_config(config, "this is not valid TOML\n[")
    assert ka.kimi_api_key_configured(config) is False


def test_auth_configured_prefers_credential_file(tmp_path: Path) -> None:
    """``kimi_auth_configured`` is True when only the credential file exists."""
    creds = tmp_path / "kimi-code.json"
    config = tmp_path / "config.toml"
    creds.write_text('{"access_token": "sk-abc"}', encoding="utf-8")
    assert ka.kimi_auth_configured(creds_path=creds, config_path=config) is True


def test_auth_configured_falls_back_to_api_key(tmp_path: Path) -> None:
    """``kimi_auth_configured`` is True when only an API key is configured."""
    creds = tmp_path / "kimi-code.json"
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.moonshot-ai]\ntype = "kimi"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_auth_configured(creds_path=creds, config_path=config) is True


def test_auth_configured_false_when_nothing_present(tmp_path: Path) -> None:
    """``kimi_auth_configured`` is False when neither credential nor key exists."""
    creds = tmp_path / "kimi-code.json"
    config = tmp_path / "config.toml"
    assert ka.kimi_auth_configured(creds_path=creds, config_path=config) is False


def test_api_key_config_respects_kimi_code_home(monkeypatch, tmp_path: Path) -> None:
    """``kimi_api_key_configured`` reads ``$KIMI_CODE_HOME/config.toml``."""
    fake_home = tmp_path / "custom-kimi-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    config = fake_home / "config.toml"
    _write_config(
        config,
        '[providers.moonshot-ai]\ntype = "kimi"\napi_key = "sk-abc123"\n',
    )
    monkeypatch.setenv("KIMI_CODE_HOME", str(fake_home))
    assert ka.kimi_api_key_configured() is True
