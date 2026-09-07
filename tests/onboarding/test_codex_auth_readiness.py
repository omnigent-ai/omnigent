"""Tests for the local mirror of Codex's provider-auth readiness logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.onboarding.codex_auth_readiness import (
    codex_config_effective_auth,
    effective_codex_model_provider,
    load_codex_config,
    provider_table_has_self_contained_auth,
)


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


_ENV_KEY_CONFIG = """\
model_provider = "gateway"

[model_providers.gateway]
env_key = "GATEWAY_TOKEN"
"""


def test_active_profile_provider_wins(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """\
profile = "work"
model_provider = "top"

[profiles.work]
model_provider = "profile-provider"
""",
    )
    parsed = load_codex_config(path)
    assert parsed is not None
    assert effective_codex_model_provider(parsed) == "profile-provider"


@pytest.mark.parametrize("value", ["token", " token "])
def test_env_key_present_is_provider_ready(tmp_path: Path, value: str) -> None:
    path = _config(tmp_path, _ENV_KEY_CONFIG)
    assert codex_config_effective_auth(path, env={"GATEWAY_TOKEN": value}) == "provider-ready"


@pytest.mark.parametrize("env", [{}, {"GATEWAY_TOKEN": ""}, {"GATEWAY_TOKEN": "   "}])
def test_env_key_missing_is_provider_auth_missing(tmp_path: Path, env: dict[str, str]) -> None:
    path = _config(tmp_path, _ENV_KEY_CONFIG)
    assert codex_config_effective_auth(path, env=env) == "provider-auth-missing"


def test_profile_selected_env_key_is_checked(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """\
profile = "work"

[profiles.work]
model_provider = "gateway"

[model_providers.gateway]
env_key = "GATEWAY_TOKEN"
""",
    )
    assert codex_config_effective_auth(path, env={}) == "provider-auth-missing"


@pytest.mark.parametrize(
    "provider_body",
    [
        "[model_providers.gateway]\n",
        '[model_providers.gateway.auth]\ncommand = "print-token"\n',
        '[model_providers.gateway]\nexperimental_bearer_token = "token"\n',
        '[model_providers.gateway.http_headers]\napi-key = "token"\n',
    ],
)
def test_non_openai_provider_without_env_key_is_ready(tmp_path: Path, provider_body: str) -> None:
    path = _config(tmp_path, f'model_provider = "gateway"\n{provider_body}')
    # Mirrors Codex: absent env_key means provider-specific auth is sufficient
    # or unnecessary. Credential validity remains a runtime concern.
    assert codex_config_effective_auth(path, env={}) == "provider-ready"


def test_requires_openai_auth_uses_codex_login(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        """\
model_provider = "gateway"

[model_providers.gateway]
requires_openai_auth = true
""",
    )
    assert codex_config_effective_auth(path, env={}) == "codex-login"


@pytest.mark.parametrize(
    "body",
    [
        "",
        'model_provider = "openai"\n',
        "model_provider = [broken\n",
    ],
)
def test_configs_that_cannot_supply_provider_auth_use_codex_login(
    tmp_path: Path, body: str
) -> None:
    assert codex_config_effective_auth(_config(tmp_path, body), env={}) == "codex-login"


@pytest.mark.parametrize(
    "provider", ["ollama", "lmstudio", "amazon-bedrock", "amazon-bedrock-runtime"]
)
def test_non_openai_builtin_provider_is_ready(tmp_path: Path, provider: str) -> None:
    path = _config(tmp_path, f'model_provider = "{provider}"\n')
    assert codex_config_effective_auth(path, env={}) == "provider-ready"


def test_missing_custom_provider_table_is_not_masked_by_login(tmp_path: Path) -> None:
    path = _config(tmp_path, 'model_provider = "missing-table"\n')
    assert codex_config_effective_auth(path, env={}) == "provider-auth-missing"


def test_redefined_ollama_table_is_ignored(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'model_provider = "ollama"\n[model_providers.ollama]\nenv_key = "OLLAMA_TOKEN"\n',
    )
    assert codex_config_effective_auth(path, env={}) == "provider-ready"


def test_redefined_openai_table_is_ignored(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        'model_provider = "openai"\n[model_providers.openai]\nrequires_openai_auth = false\n',
    )
    assert codex_config_effective_auth(path, env={}) == "codex-login"


def test_missing_config_uses_codex_login(tmp_path: Path) -> None:
    assert codex_config_effective_auth(tmp_path / "missing.toml", env={}) == "codex-login"


@pytest.mark.parametrize("header", ["Authorization", "api-key", "x-api-key"])
def test_auth_headers_are_self_contained_for_adoption(header: str) -> None:
    assert provider_table_has_self_contained_auth({"http_headers": {header: "credential"}})


def test_env_key_is_not_self_contained_for_adoption() -> None:
    assert not provider_table_has_self_contained_auth({"env_key": "GATEWAY_TOKEN"})
