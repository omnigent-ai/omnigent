"""Tests for omnigent.pi_native_credentials (native Pi provider wiring)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from omnigent import pi_native_credentials as creds


def _databricks_config() -> dict[str, object]:
    """A config whose default provider is a Databricks profile (serves pi)."""
    return {
        "providers": {
            "databricks": {"kind": "databricks", "default": True, "profile": "demo-staging"},
        }
    }


def test_resolves_databricks_default_to_anthropic_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Databricks default → Pi anthropic-messages gateway provider.

    The Databricks profile is marked default for the anthropic/openai surfaces
    (not ``pi`` directly), so the resolver must fall back to the Anthropic
    surface — which Pi speaks natively — and build a gateway provider with a
    bearer-token refresh command.
    """
    from omnigent.inner import databricks_executor

    def _host(profile: str | None) -> str:
        return "https://wkspc.example.com/"

    monkeypatch.setattr(databricks_executor, "_read_databrickscfg_host", _host)

    provider = creds.resolve_pi_native_provider(config_loader=_databricks_config)

    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.base_url == "https://wkspc.example.com/ai-gateway/anthropic"
    assert provider.model == "databricks-claude-sonnet-4-6"
    assert provider.auth_header is True
    # apiKey is a "!command" so Pi refreshes the gateway token per request.
    assert provider.api_key.startswith("!")
    assert "demo-staging" in provider.api_key


def test_databricks_unresolvable_host_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No host for the profile → fall back to Pi's own login (None)."""
    from omnigent.inner import databricks_executor

    def _no_host(profile: str | None) -> None:
        return None

    monkeypatch.setattr(databricks_executor, "_read_databrickscfg_host", _no_host)
    assert creds.resolve_pi_native_provider(config_loader=_databricks_config) is None


def test_key_provider_resolves_to_inline_family() -> None:
    """A key-kind provider with an anthropic family → inline Pi provider."""
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test-literal",
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(
        model="claude-sonnet-4-6", config_loader=lambda: config
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.api_key == "sk-test-literal"
    assert provider.auth_header is False
    assert provider.model == "claude-sonnet-4-6"


def test_subscription_default_returns_none() -> None:
    """A subscription (CLI-login) default isn't reusable by Pi → None."""
    config = {"providers": {"claude": {"kind": "subscription", "default": True, "cli": "claude"}}}
    assert creds.resolve_pi_native_provider(config_loader=lambda: config) is None


def test_no_providers_returns_none() -> None:
    """No configured providers → None (Pi uses its own login)."""
    assert creds.resolve_pi_native_provider(config_loader=dict) is None


def test_malformed_config_returns_none() -> None:
    """A loader that raises must not break launch — resolve to None."""

    def _boom() -> dict[str, object]:
        raise RuntimeError("bad config")

    assert creds.resolve_pi_native_provider(config_loader=_boom) is None


def test_unresolvable_secret_falls_back_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider whose secret can't resolve → None, not a hard launch failure.

    A key-kind default whose ``api_key`` references an env var absent from the
    runner env makes ``entry.family()`` raise during resolution (not during the
    config load). The contract is "any resolution failure → fall back to Pi's
    own login", so the resolver must swallow it and return ``None`` rather than
    let the exception fail the Pi terminal launch.
    """
    monkeypatch.delenv("PI_NATIVE_AUDIT_UNSET_KEY", raising=False)
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "$PI_NATIVE_AUDIT_UNSET_KEY",
                },
            }
        }
    }
    assert creds.resolve_pi_native_provider(config_loader=lambda: config) is None


def test_to_models_config_shape() -> None:
    """The rendered models.json carries baseUrl/api/apiKey/models (+authHeader)."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://x/ai-gateway/anthropic",
        api="anthropic-messages",
        model="databricks-claude-sonnet-4-6",
        api_key="!get-token",
        auth_header=True,
    )
    cfg = provider.to_models_config()
    entry = cfg["providers"]["omnigent"]
    assert entry["baseUrl"] == "https://x/ai-gateway/anthropic"
    assert entry["api"] == "anthropic-messages"
    assert entry["apiKey"] == "!get-token"
    assert entry["authHeader"] is True
    assert entry["models"] == [{"id": "databricks-claude-sonnet-4-6"}]


def test_write_models_config_is_owner_only(tmp_path: Path) -> None:
    """models.json is written 0600 in a 0700 dir (it may hold a literal key)."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
    )
    agent_dir = tmp_path / "pi-agent"
    path = creds.write_pi_models_config(agent_dir, provider)

    assert path == agent_dir / "models.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(agent_dir.stat().st_mode) == 0o700
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["providers"]["omnigent"]["apiKey"] == "sk-secret"


def test_provider_launch_returns_env_and_args(tmp_path: Path) -> None:
    """pi_native_provider_launch writes config and returns the env + CLI args."""
    provider = creds.PiProviderConfig(
        provider_id="omnigent",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        model="claude-sonnet-4-6",
        api_key="sk-secret",
        auth_header=False,
    )
    agent_dir = tmp_path / "pi-agent"
    env, args = creds.pi_native_provider_launch(agent_dir, provider)

    assert env == {creds.PI_CODING_AGENT_DIR_ENV_VAR: str(agent_dir)}
    assert args == ["--provider", "omnigent", "--model", "claude-sonnet-4-6"]
    assert (agent_dir / "models.json").exists()


def test_openai_chat_wire_api_resolves_to_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenAI family with wire_api: chat → openai-completions API.

    This tests the fix for the DeepInfra bug where pi-native was ignoring
    the wire_api setting and always using openai-responses. Providers like
    DeepInfra implement Chat Completions (/v1/openai/chat/completions) but
    not the Responses API (/v1/openai/responses returns 404).
    """
    # Set a fake API key in the environment for testing
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-deepinfra-key")

    config = {
        "providers": {
            "deepinfra": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.deepinfra.com/v1/openai",
                    "api_key": "$OPENAI_API_KEY",
                    "wire_api": "chat",
                    "models": {"default": "zai-org/GLM-4.7"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # wire_api: chat should resolve to openai-completions, not openai-responses
    assert provider.api == "openai-completions", (
        f"Expected openai-completions but got {provider.api} "
        f"(wire_api:chat should use chat completions API, not responses)"
    )
    assert provider.base_url == "https://api.deepinfra.com/v1/openai"
    assert provider.model == "zai-org/GLM-4.7"
    assert provider.api_key == "sk-test-deepinfra-key"  # Resolved from environment
    assert provider.auth_header is False


def test_openai_responses_wire_api_default() -> None:
    """An OpenAI family without wire_api (or wire_api: responses) → openai-responses API.

    When wire_api is not set or set to "responses", the default behavior
    should be to use the OpenAI Responses API.
    """
    config = {
        "providers": {
            "openai-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "models": {"default": "gpt-4o"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Default (no wire_api) should use openai-responses
    assert provider.api == "openai-responses"
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.model == "gpt-4o"


def test_openai_responses_wire_api_explicit() -> None:
    """An OpenAI family with wire_api: responses → openai-responses API.

    When wire_api is explicitly set to "responses", it should use the
    OpenAI Responses API.
    """
    config = {
        "providers": {
            "openai-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-test",
                    "wire_api": "responses",
                    "models": {"default": "gpt-4o"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Explicit wire_api: responses should use openai-responses
    assert provider.api == "openai-responses"
    assert provider.base_url == "https://api.openai.com/v1"
    assert provider.model == "gpt-4o"


def test_anthropic_family_ignores_wire_api() -> None:
    """The Anthropic family always uses anthropic-messages, ignoring wire_api.

    The wire_api setting is only meaningful for the OpenAI family.
    Errors: (BLE001 — any resolution failure must not break launch
    # The contract is "any resolution failure → fall back to Pi's
    # own login", so the resolver must swallow it and return ``None`` rather than
    # let the exception fail the Pi terminal launch.
    """
    config = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key": "sk-test",
                    "wire_api": "chat",  # Should be ignored for Anthropic
                    "models": {"default": "claude-4"},
                },
            }
        }
    }
    provider = creds.resolve_pi_native_provider(config_loader=lambda: config)
    assert provider is not None
    # Anthropic should always use anthropic-messages, not affected by wire_api
    assert provider.api == "anthropic-messages"
    assert provider.base_url == "https://api.anthropic.com"
    assert provider.model == "claude-4"
    assert provider.api_key == "sk-test"
