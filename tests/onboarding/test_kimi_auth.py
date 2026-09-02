"""Tests for :mod:`omnigent.onboarding.kimi_auth`.

``kimi login`` writes ``~/.kimi-code/credentials/kimi-code.json`` (verified
against kimi CLI v0.29.1). Detection is a subprocess-free file check: a present,
non-empty file is a completed login; a missing or empty file is not. Pay-per-use
users authenticate instead by configuring a Kimi provider with ``api_key`` or
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


def test_default_path_respects_kimi_code_home(monkeypatch, tmp_path: Path) -> None:
    """With no argument, detection resolves the credential via ``$KIMI_CODE_HOME``.

    Point ``KIMI_CODE_HOME`` at a tmp dir so the check is deterministic, never
    depends on the developer's real credential file, and confirms the login path
    honors the same home override as the config-key path.
    """
    fake_home = tmp_path / "custom-kimi-home"
    fake_creds = fake_home / "credentials" / "kimi-code.json"
    fake_creds.parent.mkdir(parents=True, exist_ok=True)
    fake_creds.write_text('{"access_token": "sk-xyz"}', encoding="utf-8")
    monkeypatch.setenv("KIMI_CODE_HOME", str(fake_home))
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


def test_moonshotai_catalog_provider_detected(tmp_path: Path) -> None:
    """A ``moonshotai`` catalog import (open-platform pay-per-use) counts.

    ``kimi provider catalog add moonshotai`` writes the catalog's own provider
    ``type`` (``moonshotai``), not ``kimi`` — detection must still recognize it.
    """
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.moonshotai]\ntype = "moonshotai"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is True


def test_provider_detected_by_moonshot_host(tmp_path: Path) -> None:
    """A provider with an opaque type but a Moonshot ``base_url`` counts."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.custom]\ntype = "custom"\n'
        'base_url = "https://api.moonshot.ai/v1"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is True


def test_provider_detected_by_moonshot_cn_host(tmp_path: Path) -> None:
    """The China Moonshot endpoint (``api.moonshot.cn``) counts as Kimi."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.custom]\ntype = "custom"\n'
        'base_url = "https://api.moonshot.cn/v1"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is True


def test_provider_detected_by_schemeless_host(tmp_path: Path) -> None:
    """A scheme-less ``base_url`` still resolves a Moonshot / Kimi host."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.custom]\ntype = "custom"\n'
        'base_url = "api.moonshot.ai/v1"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is True


def test_lookalike_host_not_detected(tmp_path: Path) -> None:
    """A look-alike parent domain (``moonshot.ai.evil.com``) is not a Kimi host."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.custom]\ntype = "custom"\n'
        'base_url = "https://moonshot.ai.evil.com/v1"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is False


def test_provider_env_moonshot_api_key_detected(tmp_path: Path) -> None:
    """A Kimi provider with ``env.MOONSHOT_API_KEY`` counts as configured."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.kimi-code]\ntype = "kimi"\n'
        '[providers.kimi-code.env]\nMOONSHOT_API_KEY = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is True


def test_non_kimi_provider_ignored(tmp_path: Path) -> None:
    """A provider that is neither a Kimi type nor a Kimi host is ignored.

    An unrelated provider the user added to kimi's config (e.g. OpenAI) must not
    make the Kimi harness read as configured when it is not the default.
    """
    config = tmp_path / "config.toml"
    _write_config(
        config,
        '[providers.openai]\ntype = "openai"\n'
        'base_url = "https://api.openai.com/v1"\napi_key = "sk-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is False


def test_default_custom_provider_detected(tmp_path: Path) -> None:
    """A custom OpenAI-compatible provider selected by ``default_model`` counts.

    Kimi Code works fully with e.g. an OpenRouter provider as the configured
    default, so readiness must treat that install as configured.
    """
    config = tmp_path / "config.toml"
    _write_config(
        config,
        'default_model = "openrouter/moonshotai/kimi-k3"\n'
        '[providers.openrouter]\ntype = "openai"\n'
        'base_url = "https://openrouter.ai/api/v1"\napi_key = "sk-or-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is True


def test_default_custom_provider_env_key_detected(tmp_path: Path) -> None:
    """A default custom provider keyed via its ``env`` table counts."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        'default_model = "openrouter/moonshotai/kimi-k3"\n'
        '[providers.openrouter]\ntype = "openai"\n'
        '[providers.openrouter.env]\nKIMI_API_KEY = "sk-or-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is True


def test_default_custom_provider_without_key_not_detected(tmp_path: Path) -> None:
    """The default custom provider still needs a non-empty API key."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        'default_model = "openrouter/moonshotai/kimi-k3"\n'
        '[providers.openrouter]\ntype = "openai"\n'
        'base_url = "https://openrouter.ai/api/v1"\n',
    )
    assert ka.kimi_api_key_configured(config) is False


def test_non_default_custom_provider_ignored(tmp_path: Path) -> None:
    """A keyed custom provider that ``default_model`` does not select is ignored.

    ``default_model`` routing to a different provider means kimi does not
    authenticate with this one by default, so it must not count as Kimi auth.
    """
    config = tmp_path / "config.toml"
    _write_config(
        config,
        'default_model = "other/some-model"\n'
        '[providers.openrouter]\ntype = "openai"\n'
        'base_url = "https://openrouter.ai/api/v1"\napi_key = "sk-or-abc123"\n',
    )
    assert ka.kimi_api_key_configured(config) is False


def test_default_model_without_provider_segment_ignored(tmp_path: Path) -> None:
    """A ``default_model`` with no ``provider/`` prefix selects no provider."""
    config = tmp_path / "config.toml"
    _write_config(
        config,
        'default_model = "kimi-k3"\n'
        '[providers.openrouter]\ntype = "openai"\n'
        'base_url = "https://openrouter.ai/api/v1"\napi_key = "sk-or-abc123"\n',
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
