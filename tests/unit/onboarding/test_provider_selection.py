"""Tests for omnigent.onboarding.provider_selection — selection logic."""

from __future__ import annotations

import pytest
from click import ClickException

from omnigent.onboarding.provider_selection import (
    ProviderSelection,
    resolve_provider_from_model,
)

# ── resolve_provider_from_model ────────────────────────


def test_resolve_parses_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid litellm format should parse into provider + full model string."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    selection = resolve_provider_from_model("anthropic/claude-sonnet-4-20250514")
    assert selection.provider == "anthropic"
    assert selection.model == "anthropic/claude-sonnet-4-20250514"
    assert isinstance(selection, ProviderSelection)


def test_resolve_reads_api_key_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials should be read from the provider's env var."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    selection = resolve_provider_from_model("openai/gpt-5.4")
    assert selection.credentials["api_key"] == "sk-openai-test"


def test_resolve_missing_env_var_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing env var should raise a clear error."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ClickException, match="ANTHROPIC_API_KEY"):
        resolve_provider_from_model("anthropic/claude-sonnet-4-20250514")


def test_resolve_rejects_model_without_slash() -> None:
    """Model string without provider/ prefix should raise."""
    with pytest.raises(ClickException, match="provider/model_name"):
        resolve_provider_from_model("gpt-5.4")


def test_resolve_handles_nested_slashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model strings with multiple slashes should split on the first only."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deep")
    selection = resolve_provider_from_model("deepseek/deepseek-chat/v2")
    assert selection.provider == "deepseek"
    # Full model string preserved.
    assert selection.model == "deepseek/deepseek-chat/v2"


def test_resolve_openai_picks_up_base_url_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    For the openai provider, ``OPENAI_BASE_URL`` flows into
    ``credentials["base_url"]`` so onboarding can point at an
    OpenAI-compatible gateway (e.g. Databricks serving endpoints).
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.databricks.com/serving-endpoints")
    selection = resolve_provider_from_model("openai/gpt-5.4")
    assert selection.credentials["api_key"] == "sk-openai-test"
    assert selection.credentials["base_url"] == "https://example.databricks.com/serving-endpoints"


def test_resolve_openai_omits_base_url_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without ``OPENAI_BASE_URL`` set, credentials carry only
    ``api_key`` — no synthetic default that would point onboarding
    at an unintended endpoint.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    selection = resolve_provider_from_model("openai/gpt-5.4")
    assert selection.credentials == {"api_key": "sk-openai-test"}


def test_resolve_non_openai_provider_ignores_openai_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``OPENAI_BASE_URL`` is provider-specific to openai. Anthropic
    (and other simple providers) must not pick it up.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.databricks.com/serving-endpoints")
    selection = resolve_provider_from_model("anthropic/claude-sonnet-4-20250514")
    assert selection.credentials == {"api_key": "sk-ant-test"}
