"""Tests for omnigent.models.pi_model_compatibility (Pi ``models.json`` entries)."""

from __future__ import annotations

import pytest

from omnigent.models.model_catalog import ModelEntry
from omnigent.models.model_metadata import (
    ModelCapability,
    ModelMetadata,
    ModelReasoningMetadata,
    ModelWireAPI,
)
from omnigent.models.pi_model_compatibility import (
    enrich_databricks_model_catalog,
    pi_model_json_entry,
)
from omnigent.onboarding.providers import ModelInfo

_REASONING = frozenset({ModelCapability.REASONING})


def _entry(model_id: str, *, reasoning: bool | None, wire: ModelWireAPI) -> ModelEntry:
    """A catalog entry whose reasoning capability is known-true, known-false, or unknown."""
    return ModelEntry(
        id=model_id,
        family="other",
        metadata=ModelMetadata(
            supported_capabilities=_REASONING if reasoning else frozenset(),
            unsupported_capabilities=_REASONING if reasoning is False else frozenset(),
            wire_apis=frozenset({wire}),
        ),
    )


@pytest.mark.parametrize(
    ("model_id", "wire"),
    [
        ("system.ai.gpt-6-luna", ModelWireAPI.OPENAI_RESPONSES),
        ("system.ai.gemini-3-8-flash", ModelWireAPI.OPENAI_CHAT),
        ("system.ai.glm-5-3", ModelWireAPI.OPENAI_RESPONSES),
        ("system.ai.kimi-k3", ModelWireAPI.OPENAI_RESPONSES),
        ("system.ai.qwen3-next", ModelWireAPI.OPENAI_RESPONSES),
        ("system.ai.grok-5", ModelWireAPI.OPENAI_CHAT),
    ],
)
def test_catalog_reasoning_capability_sets_pi_reasoning_flag(
    model_id: str, wire: ModelWireAPI
) -> None:
    """A reasoning-capable catalog entry carries ``reasoning: true`` whatever its vendor."""
    entry = pi_model_json_entry(_entry(model_id, reasoning=True, wire=wire))

    assert entry.get("reasoning") is True


def test_unknown_capability_keeps_vendor_fallbacks() -> None:
    """Without catalog capability data, only DeepSeek and Claude get the flag."""
    deepseek = pi_model_json_entry(
        _entry("system.ai.deepseek-v4", reasoning=None, wire=ModelWireAPI.OPENAI_CHAT)
    )
    claude = pi_model_json_entry(
        _entry("system.ai.claude-fable-5-1", reasoning=None, wire=ModelWireAPI.ANTHROPIC_MESSAGES)
    )
    llama = pi_model_json_entry(
        _entry("system.ai.llama-4-maverick", reasoning=None, wire=ModelWireAPI.OPENAI_CHAT)
    )

    assert deepseek.get("reasoning") is True
    assert claude.get("reasoning") is True
    assert "reasoning" not in llama


def test_catalog_unsupported_reasoning_leaves_flag_unset() -> None:
    """A model the catalog marks non-reasoning is written without the flag."""
    entry = pi_model_json_entry(
        _entry("system.ai.llama-4-maverick", reasoning=False, wire=ModelWireAPI.OPENAI_CHAT)
    )

    assert "reasoning" not in entry


@pytest.mark.parametrize(
    ("model_id", "wire"),
    [
        ("databricks-deepseek-v4", ModelWireAPI.OPENAI_CHAT),
        ("databricks-claude-fable-5-1", ModelWireAPI.ANTHROPIC_MESSAGES),
    ],
)
def test_vendor_floor_keeps_reasoning_flag_despite_catalog(
    model_id: str, wire: ModelWireAPI
) -> None:
    """DeepSeek and Claude keep ``reasoning: true`` even when the catalog denies it.

    DeepSeek reads its chain of thought off ``reasoning_content`` and Claude
    enables extended thinking; the id floor preserves both whatever the catalog
    reports.
    """
    entry = pi_model_json_entry(_entry(model_id, reasoning=False, wire=wire))

    assert entry.get("reasoning") is True


@pytest.mark.parametrize("live_efforts", [None, frozenset({"high"})])
def test_enriched_live_entry_carries_catalog_reasoning(live_efforts) -> None:
    """Live capabilities and reasoning details survive catalog enrichment."""
    live_reasoning = ModelReasoningMetadata(efforts=live_efforts) if live_efforts else None
    catalog_reasoning = ModelReasoningMetadata(efforts=frozenset({"low"}))
    live = ModelEntry(
        id="system.ai.gpt-6-luna",
        family="openai",
        metadata=ModelMetadata(
            supported_capabilities=frozenset({ModelCapability.TOOL_USE}),
            wire_apis=frozenset({ModelWireAPI.OPENAI_RESPONSES}),
            reasoning=live_reasoning,
        ),
    )
    catalog = ModelEntry(
        id="databricks-gpt-6-luna",
        family="openai",
        metadata=ModelMetadata(
            supported_capabilities=_REASONING,
            context_window=400_000,
            reasoning=catalog_reasoning,
        ),
    )

    (enriched,) = enrich_databricks_model_catalog((live,), (catalog,))

    assert enriched.metadata.supports(ModelCapability.REASONING) is True
    assert enriched.metadata.supports(ModelCapability.TOOL_USE) is True
    assert enriched.metadata.reasoning == (live_reasoning or catalog_reasoning)
    assert enriched.metadata.wire_apis == frozenset({ModelWireAPI.OPENAI_RESPONSES})
    assert pi_model_json_entry(enriched) == {
        "id": "system.ai.gpt-6-luna",
        "input": ["text", "image"],
        "contextWindow": 400_000,
        "reasoning": True,
    }


@pytest.mark.parametrize("live_reasoning", [None, False, True])
@pytest.mark.parametrize("catalog_reasoning", [False, True])
def test_gateway_catalog_falls_back_to_vendor_metadata(
    monkeypatch, live_reasoning, catalog_reasoning
):
    monkeypatch.setattr(
        "omnigent.onboarding.providers.get_models",
        lambda provider: (
            [
                ModelInfo(
                    name="gpt-6-luna",
                    provider="openai",
                    supports_reasoning=catalog_reasoning,
                    max_input_tokens=400_000,
                    max_output_tokens=128_000,
                )
            ]
            if provider == "openai"
            else []
        ),
    )
    live = _entry(
        "system.ai.gpt-6-luna", reasoning=live_reasoning, wire=ModelWireAPI.OPENAI_RESPONSES
    )
    expected = catalog_reasoning if live_reasoning is None else live_reasoning
    (enriched,) = enrich_databricks_model_catalog((live,), ())
    assert enriched.metadata.supports(ModelCapability.REASONING) is expected
    assert enriched.metadata.context_window == 400_000
    assert enriched.metadata.wire_apis == live.metadata.wire_apis
    assert (pi_model_json_entry(enriched).get("reasoning") is True) is expected


@pytest.mark.parametrize("reasoning", [True, False, None])
@pytest.mark.parametrize("configured_limits", [False, True])
def test_generic_model_metadata_preserves_capability_and_limit_precedence(
    monkeypatch: pytest.MonkeyPatch, reasoning: bool | None, configured_limits: bool
) -> None:
    from omnigent.models.pi_model_compatibility import pi_generic_model_json_entry

    catalog = ModelInfo(
        name="custom-reasoning-model",
        provider="openai",
        supports_reasoning=reasoning,
        max_input_tokens=200_000,
        max_output_tokens=16_000,
    )
    monkeypatch.setattr("omnigent.onboarding.providers.find_catalog_models", lambda _: [catalog])
    entry = pi_generic_model_json_entry(
        catalog.name,
        configured_context_window=123_000 if configured_limits else None,
        configured_max_output_tokens=8_000 if configured_limits else None,
    )
    assert (entry.get("reasoning") is True) is (reasoning is True)
    assert entry["contextWindow"] == (123_000 if configured_limits else 200_000)
    assert entry["maxTokens"] == (8_000 if configured_limits else 16_000)


@pytest.mark.parametrize("model", ["unknown-model", "deepseek-test", "claude-test"])
def test_generic_model_catalog_outage_keeps_existing_fallbacks(
    monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    from omnigent.models.pi_model_compatibility import pi_generic_model_json_entry

    def unavailable(_):
        raise OSError("catalog unavailable")

    monkeypatch.setattr("omnigent.onboarding.providers.find_catalog_models", unavailable)
    entry = pi_generic_model_json_entry(model)
    assert entry["id"] == model
    assert (entry.get("reasoning") is True) is (model != "unknown-model")
    assert "contextWindow" not in entry and "maxTokens" not in entry


def test_generic_catalog_programming_errors_surface(monkeypatch):
    from omnigent.models.pi_model_compatibility import pi_generic_model_json_entry

    def invalid(_):
        raise TypeError("invalid catalog implementation")

    monkeypatch.setattr("omnigent.onboarding.providers.find_catalog_models", invalid)
    with pytest.raises(TypeError, match="invalid catalog implementation"):
        pi_generic_model_json_entry("custom-model")


@pytest.mark.parametrize("other_reasoning", [False, None])
def test_generic_model_ambiguous_catalog_does_not_guess_capability(monkeypatch, other_reasoning):
    from omnigent.models.pi_model_compatibility import pi_generic_model_json_entry

    monkeypatch.setattr(
        "omnigent.onboarding.providers.find_catalog_models",
        lambda _: [
            ModelInfo(
                name="custom-a",
                provider="openai",
                supports_reasoning=True,
                max_input_tokens=100_000,
            ),
            ModelInfo(
                name="custom-b",
                provider="openai",
                supports_reasoning=other_reasoning,
                max_input_tokens=200_000,
            ),
        ],
    )
    entry = pi_generic_model_json_entry("custom-model")
    assert "reasoning" not in entry and "contextWindow" not in entry


@pytest.mark.parametrize(
    ("model", "requires_thinking", "adaptive"),
    [
        ("system.ai.claude-opus-5-5", True, True),
        ("databricks-claude-sonnet-5-5", True, True),
        ("claude-fable-5-1", True, True),
        ("anthropic/claude-opus-5-5", True, True),
        ("system.ai.claude-opus-5-5[1m]", True, True),
        ("anthropic/Claude-Sonnet-5-5[1M]", True, True),
        ("claude-opus-5-5-20261001", True, True),
        ("us.anthropic.claude-opus-5-5", True, True),
        ("us.anthropic.claude-opus-5-5-20261001-v1:0", True, True),
        ("claude-sonnet-4-6", False, True),
        ("system.ai.claude-opus-5", False, True),
        ("claude-haiku-4-5", False, False),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", False, False),
        ("databricks-claude-opus-4-5", False, False),
        ("claude-sonnet-4-5[1m]", False, False),
        ("system.ai.claude-haiku-5-5", False, None),
        ("system.ai.claude-opus-5-50", False, None),
        ("claude-opus-5-5-custom", False, None),
    ],
)
@pytest.mark.parametrize("catalog_available", [False, True])
def test_native_and_sdk_agree_on_anthropic_thinking(
    model, requires_thinking, adaptive, catalog_available
):
    from omnigent.harnesses.pi_native.credentials import PiProviderConfig
    from omnigent.inner.pi_executor import _build_models_json

    catalog = ModelEntry(id=model, family="claude", metadata=ModelMetadata(context_window=1000000))
    native = PiProviderConfig(
        provider_id="omnigent",
        api="anthropic-messages",
        base_url="https://gateway.example/anthropic",
        api_key="synthetic",
        auth_header=True,
        model=model,
        extra_models=[pi_model_json_entry(catalog)] if catalog_available else [],
    ).to_models_config()["providers"]["omnigent"]
    sdk = _build_models_json(
        "https://gateway.example",
        "synthetic",
        {"claude": "https://gateway.example/anthropic"},
        model=model,
        catalog_models=[catalog] if catalog_available else [],
    )["providers"]["databricks-anthropic"]
    assert native["compat"]["forceAdaptiveThinking"] is True
    assert "forceAdaptiveThinking" not in sdk.get("compat", {})
    for provider in (native, sdk):
        entry = next(row for row in provider["models"] if row["id"] == model)
        assert entry.get("compat", {}).get("forceAdaptiveThinking") is adaptive
        assert entry.get("thinkingLevelMap") == ({"off": None} if requires_thinking else None)
        if requires_thinking:
            assert entry["reasoning"] is True
        if catalog_available:
            assert entry["contextWindow"] == 1000000


@pytest.mark.parametrize(
    ("wire_api", "provider_name"),
    [("chat", "databricks-completions"), ("responses", "databricks-openai")],
)
def test_openai_compatible_claude_keeps_off_available(wire_api, provider_name):
    from omnigent.harnesses.pi_native.credentials import PiProviderConfig
    from omnigent.inner.pi_executor import _build_models_json
    from omnigent.models.pi_model_compatibility import pi_generic_model_json_entry

    model = "claude-opus-5-5"
    native = PiProviderConfig(
        provider_id="omnigent",
        api="openai-completions",
        base_url="https://gateway.example/v1",
        api_key="synthetic",
        auth_header=True,
        model=model,
        extra_models=[pi_generic_model_json_entry(model)],
    ).to_models_config()["providers"]["omnigent"]
    sdk = _build_models_json(
        "https://gateway.example",
        "synthetic",
        {"openai": "https://gateway.example/v1"},
        model=model,
        openai_wire_api=wire_api,
    )["providers"][provider_name]
    for provider in (native, sdk):
        assert "thinkingLevelMap" not in provider["models"][0]
        assert "compat" not in provider["models"][0]
    assert sdk["models"] == native["models"] == [pi_generic_model_json_entry(model)]


@pytest.mark.parametrize(
    ("model", "compat"),
    [
        ("claude-opus-5-5", {}),
        ("claude-opus-5-5[1m]", {"forceAdaptiveThinking": False}),
        ("claude-haiku-4-5", {"forceAdaptiveThinking": True}),
    ],
)
def test_explicit_thinking_settings_win_over_fallback(model, compat) -> None:
    from omnigent.models.pi_model_compatibility import pi_model_with_thinking_defaults

    entry = {
        "id": model,
        "thinkingLevelMap": {"off": "low"},
        "compat": {"supportsEagerToolInputStreaming": False, **compat},
    }
    result = pi_model_with_thinking_defaults(entry)
    assert result["thinkingLevelMap"] is entry["thinkingLevelMap"]
    assert result["compat"] == {"forceAdaptiveThinking": True, **entry["compat"]}
    assert entry["compat"] == {"supportsEagerToolInputStreaming": False, **compat}


def test_unknown_thinking_defaults_leave_entry_unchanged() -> None:
    from omnigent.models.pi_model_compatibility import pi_model_with_thinking_defaults

    entry = {"id": "anthropic-compatible-model"}
    assert pi_model_with_thinking_defaults(entry) is entry
