"""Unit tests for provider-neutral model-intent resolution."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from omnigent.model_catalog import ModelEntry
from omnigent.model_metadata import (
    ModelCapability,
    ModelCostTier,
    ModelIntent,
    ModelMetadata,
    ModelWireAPI,
)
from omnigent.model_resolver import (
    ModelCandidate,
    ModelPreferencePolicy,
    ModelResolutionError,
    ModelResolutionRequest,
    ModelResolutionSource,
    resolve_model,
)


def _candidate(
    model_id: str,
    *,
    family: str = "provider-family",
    supported: frozenset[ModelCapability] = frozenset(),
    unsupported: frozenset[ModelCapability] = frozenset(),
    context_window: int | None = None,
    cost_tier: ModelCostTier | None = None,
    wire_apis: frozenset[ModelWireAPI] = frozenset(),
) -> ModelEntry:
    return ModelEntry(
        id=model_id,
        family=family,
        metadata=ModelMetadata(
            supported_capabilities=supported,
            unsupported_capabilities=unsupported,
            context_window=context_window,
            cost_tier=cost_tier,
            wire_apis=wire_apis,
        ),
    )


def test_explicit_model_bypasses_constraints_without_catalog_metadata() -> None:
    resolution = resolve_model(
        ModelResolutionRequest(
            explicit_model="user-choice",
            required_capabilities=frozenset({ModelCapability.TOOL_USE}),
            minimum_context_window=200_000,
            required_wire_api=ModelWireAPI.BEDROCK_CONVERSE,
            allowed_families=frozenset({"required-family"}),
        ),
        [_candidate("catalog-choice")],
    )

    assert resolution.model_id == "user-choice"
    assert resolution.source == ModelResolutionSource.EXPLICIT
    assert resolution.metadata == ModelMetadata()
    assert resolution.family is None


def test_configured_default_wins_before_live_catalog() -> None:
    models = [_candidate("provider-default"), _candidate("catalog-choice")]

    resolution = resolve_model(
        ModelResolutionRequest(configured_default="provider-default"),
        models,
    )

    assert resolution.model_id == "provider-default"
    assert resolution.source == ModelResolutionSource.CONFIGURED_DEFAULT


def test_capability_requirement_skips_unverified_configured_default() -> None:
    coding_model = _candidate(
        "tool-capable",
        supported=frozenset({ModelCapability.TOOL_USE}),
    )

    resolution = resolve_model(
        ModelResolutionRequest(
            configured_default="unlisted-default",
            required_capabilities=frozenset({ModelCapability.TOOL_USE}),
        ),
        [coding_model],
    )

    assert resolution.model_id == "tool-capable"
    assert resolution.source == ModelResolutionSource.LIVE_CATALOG


def test_unknown_capability_does_not_satisfy_requirement() -> None:
    unknown = _candidate("unknown-tools")
    supported = _candidate(
        "known-tools",
        supported=frozenset({ModelCapability.TOOL_USE}),
    )

    resolution = resolve_model(
        ModelResolutionRequest(required_capabilities=frozenset({ModelCapability.TOOL_USE})),
        [unknown, supported],
    )

    assert resolution.model_id == "known-tools"


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (ModelIntent.FAST, "economy"),
        (ModelIntent.BALANCED, "standard"),
        (ModelIntent.POWERFUL, "premium"),
    ],
)
def test_cost_intents_rank_by_normalized_tier(intent: ModelIntent, expected: str) -> None:
    models = [
        _candidate("premium", cost_tier=ModelCostTier.PREMIUM),
        _candidate("economy", cost_tier=ModelCostTier.ECONOMY),
        _candidate("standard", cost_tier=ModelCostTier.STANDARD),
    ]

    resolution = resolve_model(ModelResolutionRequest(intent=intent), models)

    assert resolution.model_id == expected


def test_cost_intent_returns_best_available_tier() -> None:
    resolution = resolve_model(
        ModelResolutionRequest(intent=ModelIntent.FAST),
        [_candidate("premium", cost_tier=ModelCostTier.PREMIUM)],
    )

    assert resolution.model_id == "premium"
    assert resolution.metadata.cost_tier == ModelCostTier.PREMIUM


def test_wire_api_vocabulary_covers_model_endpoint_adapters() -> None:
    assert {wire_api.value for wire_api in ModelWireAPI} == {
        "anthropic-messages",
        "bedrock-converse",
        "gemini-generate-content",
        "openai-chat",
        "openai-responses",
    }


def test_constraints_filter_family_context_capability_and_wire_api() -> None:
    required_capabilities = frozenset(
        {ModelCapability.REASONING, ModelCapability.STRUCTURED_OUTPUT}
    )
    compatible = _candidate(
        "compatible",
        family="allowed",
        supported=required_capabilities,
        context_window=250_000,
        wire_apis=frozenset({ModelWireAPI.OPENAI_RESPONSES}),
    )
    wrong_wire = _candidate(
        "wrong-wire",
        family="allowed",
        supported=required_capabilities,
        context_window=250_000,
        wire_apis=frozenset({ModelWireAPI.OPENAI_CHAT}),
    )

    resolution = resolve_model(
        ModelResolutionRequest(
            required_capabilities=required_capabilities,
            minimum_context_window=200_000,
            required_wire_api=ModelWireAPI.OPENAI_RESPONSES,
            allowed_families=frozenset({"allowed"}),
        ),
        [wrong_wire, compatible],
    )

    assert resolution.model_id == "compatible"


def test_static_fallback_is_used_only_after_live_candidates_fail() -> None:
    fallback = _candidate(
        "fallback",
        supported=frozenset({ModelCapability.IMAGE_GENERATION}),
    )

    resolution = resolve_model(
        ModelResolutionRequest(
            required_capabilities=frozenset({ModelCapability.IMAGE_GENERATION})
        ),
        [_candidate("live-with-unknown-capabilities")],
        static_fallbacks=[fallback],
    )

    assert resolution.model_id == "fallback"
    assert resolution.source == ModelResolutionSource.STATIC_FALLBACK


def test_provider_preference_policy_can_override_default_order() -> None:
    class _ReversePolicy(ModelPreferencePolicy):
        def rank(
            self,
            intent: ModelIntent,
            candidates: Sequence[ModelCandidate],
        ) -> Sequence[ModelCandidate]:
            del intent
            return tuple(reversed(candidates))

    resolution = resolve_model(
        ModelResolutionRequest(),
        [_candidate("first"), _candidate("second")],
        preference_policy=_ReversePolicy(),
    )

    assert resolution.model_id == "second"


def test_no_compatible_model_raises_clear_error() -> None:
    with pytest.raises(ModelResolutionError, match=r"default.*tool-use"):
        resolve_model(
            ModelResolutionRequest(required_capabilities=frozenset({ModelCapability.TOOL_USE})),
            [_candidate("unknown-tools")],
        )


def test_metadata_rejects_conflicting_capability_facts() -> None:
    with pytest.raises(ValueError, match="both supported and unsupported"):
        ModelMetadata(
            supported_capabilities=frozenset({ModelCapability.VISION}),
            unsupported_capabilities=frozenset({ModelCapability.VISION}),
        )
