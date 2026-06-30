"""Tests for omnigent.onboarding.providers — catalog loading and queries."""

from __future__ import annotations

import pytest

from omnigent.onboarding import providers as _providers_mod
from omnigent.onboarding.providers import (
    ModelInfo,
    ProviderConfig,
    default_chat_model,
    get_all_providers,
    get_chat_models,
    get_models,
    get_provider_config,
)

# Minimal catalog fixture that mirrors the real MLflow JSON schema.
# Keyed by provider name; each value is what _fetch_provider_catalog returns
# (the full parsed dict with a "models" key).
_FAKE_CATALOG: dict[str, dict] = {
    "anthropic": {
        "models": {
            "claude-opus-4-8": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 200000, "max_output": 32000},
            },
            "claude-sonnet-4-6": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 200000, "max_output": 8192},
            },
            "claude-haiku-4-5": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 200000, "max_output": 8192},
            },
            "claude-3-embedding": {
                "mode": "embedding",
                "capabilities": {},
            },
        }
    },
    "openai": {
        "models": {
            "gpt-5.5-audio-preview-2026-06-01": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 128000, "max_output": 16384},
            },
            "gpt-5.5": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 128000, "max_output": 16384},
            },
            "gpt-4.1": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 128000, "max_output": 16384},
            },
            "gpt-3.5-turbo": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 16385, "max_output": 4096},
            },
        }
    },
    "gemini": {
        "models": {
            "gemini/gemini-2.5-flash": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 1000000, "max_output": 8192},
            },
        }
    },
}


@pytest.fixture(autouse=True)
def mock_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _fetch_provider_catalog to return fixture data without network calls."""
    monkeypatch.setattr(
        _providers_mod,
        "_fetch_provider_catalog",
        lambda provider: _FAKE_CATALOG.get(provider, {}),
    )


# ── get_all_providers ──────────────────────────────────────


def test_get_all_providers_returns_nonempty_list() -> None:
    """Catalog must contain at least the major providers."""
    providers = get_all_providers()
    # 68 JSON files in the catalog; after normalization and exclusion
    # there should still be many providers.
    assert len(providers) > 40, (
        f"Expected at least 40 providers from the catalog, "
        f"got {len(providers)}. Catalog files may be missing."
    )


def test_get_all_providers_contains_major_providers() -> None:
    """Major providers must appear in the catalog."""
    providers = get_all_providers()
    for expected in ["anthropic", "openai", "gemini", "groq", "deepseek"]:
        assert expected in providers, (
            f"Expected {expected!r} in providers list. "
            f"Catalog file {expected}.json may be missing."
        )


def test_get_all_providers_popular_first() -> None:
    """Popular providers must appear before the rest."""
    from omnigent.onboarding.providers import COMMON_PROVIDERS

    providers = get_all_providers()
    # The first entries should be the popular providers (in order).
    popular_in_list = [p for p in providers if p in COMMON_PROVIDERS]
    expected_popular = [p for p in COMMON_PROVIDERS if p in set(providers)]
    assert popular_in_list[: len(expected_popular)] == expected_popular, (
        f"Popular providers should appear first in COMMON_PROVIDERS order. "
        f"Got: {popular_in_list[: len(expected_popular)]}, "
        f"expected: {expected_popular}"
    )
    # The rest (after popular) should be alphabetically sorted.
    rest = providers[len(expected_popular) :]
    assert rest == sorted(rest), (
        f"Non-popular providers should be alphabetically sorted, but got: {rest[:10]}..."
    )


def test_get_all_providers_excludes_bedrock_converse() -> None:
    """bedrock_converse is a variant that should be excluded."""
    providers = get_all_providers()
    assert "bedrock_converse" not in providers, (
        "bedrock_converse should be excluded (it's a variant of bedrock)."
    )


def test_get_all_providers_consolidates_vertex_ai() -> None:
    """vertex_ai-* variants should be consolidated into vertex_ai."""
    providers = get_all_providers()
    assert "vertex_ai" in providers, (
        "vertex_ai should appear after consolidating vertex_ai-* variants."
    )
    vertex_variants = [p for p in providers if p.startswith("vertex_ai-")]
    assert vertex_variants == [], (
        f"vertex_ai-* variants should be consolidated, but found: {vertex_variants}"
    )


# ── get_models / get_chat_models ─────────────────────────


def test_get_models_anthropic_returns_models() -> None:
    """Anthropic catalog must contain known models."""
    models = get_models("anthropic")
    assert len(models) > 0, "Expected at least one model from the anthropic catalog."
    # Every model should have the provider field set.
    for m in models:
        assert m.provider == "anthropic", (
            f"Model {m.name!r} has provider={m.provider!r}, expected 'anthropic'."
        )


def test_get_chat_models_filters_to_chat_mode() -> None:
    """get_chat_models must only return mode='chat' models."""
    chat_models = get_chat_models("anthropic")
    assert len(chat_models) > 0, "Expected at least one chat model from anthropic catalog."
    for m in chat_models:
        assert m.mode == "chat", (
            f"get_chat_models returned model {m.name!r} with mode={m.mode!r}, expected 'chat'."
        )


def test_get_chat_models_sorted_newest_first() -> None:
    """Chat models must be sorted with newer versions before older ones."""
    chat_models = get_chat_models("openai")
    # gpt-5.x models should appear before gpt-4.x models, which
    # should appear before gpt-3.5 models. Check that the first
    # model has a higher version than the last.
    from omnigent.onboarding.providers import _extract_model_version

    first_version = _extract_model_version(chat_models[0].name)
    last_version = _extract_model_version(chat_models[-1].name)
    assert first_version >= last_version, (
        f"Models not sorted newest-first: first model "
        f"{chat_models[0].name!r} (v{first_version}) should have "
        f"version >= last model {chat_models[-1].name!r} (v{last_version})."
    )


# ── default_chat_model ─────────────────────────────────────


def test_default_chat_model_anthropic_is_pinned_opus() -> None:
    """The anthropic default is the explicit ``claude-opus-4-8`` pin.

    The out-of-box Claude default is an explicit pin (it may be newer than
    the bundled catalog), so a fresh user gets the intended current model.
    A failure means the pin regressed to the dynamic catalog pick.
    """
    assert default_chat_model("anthropic") == "claude-opus-4-8"


def test_default_chat_model_openai_is_pinned_gpt() -> None:
    """The openai default is the explicit ``gpt-5.5`` pin (general-purpose).

    The default must be a usable general-purpose ``gpt-*`` text model, never
    a specialty (audio/realtime/…) variant. A failure means the pin
    regressed.
    """
    default = default_chat_model("openai")
    assert default == "gpt-5.5"
    assert default.startswith("gpt-")
    for token in ("audio", "realtime", "search", "transcribe", "tts", "image"):
        assert token not in default.lower()


def test_default_chat_model_openrouter_is_pinned_oss() -> None:
    """OpenRouter defaults to the pinned OSS model (not an OpenAI/Anthropic id).

    OpenRouter routes OSS models, so its out-of-box default is an OSS model
    (``moonshotai/kimi-k2.6``) — also the gateway add flow's OpenAI-surface
    pre-fill. A failure means the OSS pin regressed.
    """
    assert default_chat_model("openrouter") == "moonshotai/kimi-k2.6"


def test_default_chat_model_dynamic_skips_specialty_variants() -> None:
    """The dynamic rule (non-pinned providers) drops specialty modalities.

    Pinned providers short-circuit, so this exercises the catalog rule via
    the internal helper on the openai catalog (whose newest-first top entry
    is a specialty model): the dynamic pick must skip audio/realtime/etc.
    and choose a general-purpose ``gpt-*``. Guards the fallback used for any
    non-pinned provider.
    """
    from omnigent.onboarding.providers import _SPECIALTY_MODEL_TOKENS

    general = [
        m.name
        for m in get_chat_models("openai")
        if not any(tok in m.name.lower() for tok in _SPECIALTY_MODEL_TOKENS)
    ]
    assert general, "openai catalog should have a general-purpose model"
    # The catalog's raw newest-first top entry IS a specialty model, so the
    # dynamic rule's exclusion genuinely changes the result.
    assert general[0] != get_chat_models("openai")[0].name
    assert general[0].startswith("gpt-")


def test_default_chat_model_unknown_provider_is_none() -> None:
    """An unknown provider yields None (the runtime then fails loud)."""
    assert default_chat_model("nonexistent_provider_xyz") is None


def test_get_models_returns_model_info_dataclass() -> None:
    """Models must be ModelInfo instances with required fields."""
    models = get_models("openai")
    model = models[0]
    assert isinstance(model, ModelInfo), (
        f"Expected ModelInfo instance, got {type(model).__name__}."
    )
    assert model.name, "Model name must not be empty."
    assert model.provider == "openai"


def test_get_models_unknown_provider_returns_empty() -> None:
    """Unknown provider should return an empty list, not raise."""
    models = get_models("nonexistent_provider_xyz")
    assert models == [], f"Expected empty list for unknown provider, got {len(models)} models."


def test_get_models_strips_provider_prefix() -> None:
    """Models with provider/ prefix in their name should be stripped."""
    models = get_models("gemini")
    for m in models:
        assert not m.name.startswith("gemini/"), (
            f"Model name {m.name!r} still has provider prefix — should be stripped."
        )


def test_get_models_excludes_finetuned() -> None:
    """Fine-tuned model variants (ft:*) should be excluded."""
    # Check openai which is most likely to have ft: entries.
    models = get_models("openai")
    ft_models = [m for m in models if m.name.startswith("ft:")]
    assert ft_models == [], (
        f"Fine-tuned models should be excluded, but found: {[m.name for m in ft_models]}"
    )


# ── get_provider_config ──────────────────────────────────


def test_simple_provider_returns_api_key_mode() -> None:
    """Simple providers (openai, anthropic) have a single api_key auth mode."""
    config = get_provider_config("anthropic")
    assert isinstance(config, ProviderConfig)
    assert config.default_mode == "api_key"
    assert len(config.auth_modes) == 1
    mode = config.auth_modes[0]
    assert mode.mode_id == "api_key"
    assert mode.is_default is True
    # Must have at least an api_key field.
    field_names = [f.name for f in mode.fields]
    assert "api_key" in field_names, f"Expected 'api_key' field in auth mode, got {field_names}."


def test_bedrock_has_multiple_auth_modes() -> None:
    """Bedrock should have api_key and access_keys auth modes."""
    config = get_provider_config("bedrock")
    mode_ids = [m.mode_id for m in config.auth_modes]
    assert "api_key" in mode_ids, f"Expected 'api_key' mode for bedrock, got {mode_ids}."
    assert "access_keys" in mode_ids, f"Expected 'access_keys' mode for bedrock, got {mode_ids}."
    assert config.default_mode == "api_key"


def test_azure_requires_api_base_and_version() -> None:
    """Azure auth mode must require api_base and api_version fields."""
    config = get_provider_config("azure")
    mode = config.auth_modes[0]
    field_names = [f.name for f in mode.fields]
    assert "api_key" in field_names
    assert "api_base" in field_names, (
        f"Azure auth must require api_base, got fields: {field_names}."
    )
    assert "api_version" in field_names, (
        f"Azure auth must require api_version, got fields: {field_names}."
    )


def test_auth_fields_have_descriptions() -> None:
    """Every auth field must have a non-empty description."""
    for provider_name in ["anthropic", "bedrock", "azure", "databricks"]:
        config = get_provider_config(provider_name)
        for mode in config.auth_modes:
            for field in mode.fields:
                assert field.description, (
                    f"Auth field {field.name!r} in {provider_name}/{mode.mode_id} "
                    f"has empty description."
                )


def test_unknown_provider_gets_default_api_key_mode() -> None:
    """Providers not in _PROVIDER_AUTH_MODES get a default api_key mode."""
    config = get_provider_config("some_unknown_provider")
    assert config.default_mode == "api_key"
    assert len(config.auth_modes) == 1
    assert config.auth_modes[0].fields[0].name == "api_key"
