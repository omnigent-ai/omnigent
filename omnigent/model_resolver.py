"""Deterministic model-intent resolution over provider catalog entries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from omnigent.model_metadata import (
    ModelCapability,
    ModelCostTier,
    ModelIntent,
    ModelMetadata,
    ModelWireAPI,
)


class ModelCandidate(Protocol):
    """Catalog entry shape consumed by :func:`resolve_model`."""

    id: str
    family: str
    metadata: ModelMetadata


class ModelResolutionSource(str, Enum):
    """Precedence branch that supplied a resolved model."""

    EXPLICIT = "explicit"
    CONFIGURED_DEFAULT = "configured-default"
    LIVE_CATALOG = "live-catalog"
    STATIC_FALLBACK = "static-fallback"


@dataclass(frozen=True)
class ModelResolutionRequest:
    """One provider-neutral model selection request."""

    intent: ModelIntent = ModelIntent.DEFAULT
    explicit_model: str | None = None
    configured_default: str | None = None
    required_capabilities: frozenset[ModelCapability] = frozenset()
    minimum_context_window: int | None = None
    required_wire_api: ModelWireAPI | None = None
    allowed_families: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for field_name, value in (
            ("explicit_model", self.explicit_model),
            ("configured_default", self.configured_default),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.minimum_context_window is not None and self.minimum_context_window <= 0:
            raise ValueError("minimum_context_window must be positive")


@dataclass(frozen=True)
class ModelResolution:
    """Resolved model plus known metadata and the precedence branch used.

    ``family`` is unavailable when an explicit or configured model is absent
    from the supplied catalogs.
    """

    model_id: str
    source: ModelResolutionSource
    metadata: ModelMetadata
    family: str | None = None


class ModelResolutionError(ValueError):
    """Raised when no candidate can satisfy a model request."""


class ModelPreferencePolicy(Protocol):
    """Provider-specific ordering policy for compatible candidates."""

    def rank(
        self,
        intent: ModelIntent,
        candidates: Sequence[ModelCandidate],
    ) -> Sequence[ModelCandidate]:
        """Return *candidates* in preferred order for *intent*."""
        ...


_COST_TIER_ORDER: dict[ModelIntent, tuple[ModelCostTier, ...]] = {
    ModelIntent.FAST: (
        ModelCostTier.ECONOMY,
        ModelCostTier.STANDARD,
        ModelCostTier.PREMIUM,
    ),
    ModelIntent.BALANCED: (
        ModelCostTier.STANDARD,
        ModelCostTier.ECONOMY,
        ModelCostTier.PREMIUM,
    ),
    ModelIntent.POWERFUL: (
        ModelCostTier.PREMIUM,
        ModelCostTier.STANDARD,
        ModelCostTier.ECONOMY,
    ),
}


class MetadataPreferencePolicy:
    """Default ranking policy using normalized cost and context metadata.

    Provider catalog order remains the stable tie-breaker. Providers can pass
    their own :class:`ModelPreferencePolicy` when their preference rules are
    richer than the normalized metadata. Intent is a best-effort ordering hint,
    not a hard cost-tier requirement; the selected tier remains available on
    the resolution metadata.
    """

    def rank(
        self,
        intent: ModelIntent,
        candidates: Sequence[ModelCandidate],
    ) -> Sequence[ModelCandidate]:
        indexed = tuple(enumerate(candidates))
        tier_order = _COST_TIER_ORDER.get(intent)
        if tier_order is None:
            return tuple(candidates)
        tier_rank = {tier: index for index, tier in enumerate(tier_order)}
        unknown_rank = len(tier_order)
        return tuple(
            candidate
            for _, candidate in sorted(
                indexed,
                key=lambda pair: (
                    tier_rank[pair[1].metadata.cost_tier]
                    if pair[1].metadata.cost_tier is not None
                    else unknown_rank,
                    pair[0],
                ),
            )
        )


def resolve_model(
    request: ModelResolutionRequest,
    live_models: Sequence[ModelCandidate],
    *,
    static_fallbacks: Sequence[ModelCandidate] = (),
    preference_policy: ModelPreferencePolicy | None = None,
) -> ModelResolution:
    """Resolve a model using explicit, configured, live, then static precedence.

    An explicit user/session model always wins and intentionally bypasses
    capability, context, family, and wire constraints, even when it is absent
    from the catalog. A configured default wins when its known metadata
    satisfies the request; an unlisted configured default can satisfy requests
    with no metadata constraints. Catalog and fallback candidates must have
    positive metadata for every requested constraint.
    """
    all_models = (*live_models, *static_fallbacks)
    if request.explicit_model is not None:
        return _named_resolution(
            request.explicit_model,
            ModelResolutionSource.EXPLICIT,
            all_models,
        )

    capabilities = request.required_capabilities
    if request.configured_default is not None:
        configured = _find_candidate(request.configured_default, all_models)
        if configured is not None and _is_compatible(configured, request, capabilities):
            return _candidate_resolution(configured, ModelResolutionSource.CONFIGURED_DEFAULT)
        if configured is None and _request_has_no_metadata_constraints(request, capabilities):
            return _named_resolution(
                request.configured_default,
                ModelResolutionSource.CONFIGURED_DEFAULT,
                (),
            )

    policy = preference_policy or MetadataPreferencePolicy()
    live_compatible = tuple(
        model for model in live_models if _is_compatible(model, request, capabilities)
    )
    if live_compatible:
        selected = policy.rank(request.intent, live_compatible)[0]
        return _candidate_resolution(selected, ModelResolutionSource.LIVE_CATALOG)

    fallback_compatible = tuple(
        model for model in static_fallbacks if _is_compatible(model, request, capabilities)
    )
    if fallback_compatible:
        selected = policy.rank(request.intent, fallback_compatible)[0]
        return _candidate_resolution(selected, ModelResolutionSource.STATIC_FALLBACK)

    constraints = sorted(capability.value for capability in capabilities)
    if request.minimum_context_window is not None:
        constraints.append(f"context>={request.minimum_context_window}")
    if request.required_wire_api is not None:
        constraints.append(f"wire={request.required_wire_api.value}")
    if request.allowed_families:
        constraints.append(f"family in {sorted(request.allowed_families)!r}")
    detail = ", ".join(constraints) if constraints else "provider compatibility"
    raise ModelResolutionError(f"no model satisfies intent {request.intent.value!r} ({detail})")


def _request_has_no_metadata_constraints(
    request: ModelResolutionRequest,
    capabilities: frozenset[ModelCapability],
) -> bool:
    return not (
        capabilities
        or request.minimum_context_window is not None
        or request.required_wire_api is not None
        or request.allowed_families
    )


def _find_candidate(
    model_id: str,
    candidates: Sequence[ModelCandidate],
) -> ModelCandidate | None:
    return next((candidate for candidate in candidates if candidate.id == model_id), None)


def _is_compatible(
    candidate: ModelCandidate,
    request: ModelResolutionRequest,
    capabilities: frozenset[ModelCapability],
) -> bool:
    if request.allowed_families and candidate.family not in request.allowed_families:
        return False
    if any(candidate.metadata.supports(capability) is not True for capability in capabilities):
        return False
    if request.minimum_context_window is not None:
        context_window = candidate.metadata.context_window
        if context_window is None or context_window < request.minimum_context_window:
            return False
    if (
        request.required_wire_api is not None
        and request.required_wire_api not in candidate.metadata.wire_apis
    ):
        return False
    return True


def _candidate_resolution(
    candidate: ModelCandidate,
    source: ModelResolutionSource,
) -> ModelResolution:
    return ModelResolution(
        model_id=candidate.id,
        source=source,
        metadata=candidate.metadata,
        family=candidate.family,
    )


def _named_resolution(
    model_id: str,
    source: ModelResolutionSource,
    candidates: Sequence[ModelCandidate],
) -> ModelResolution:
    candidate = _find_candidate(model_id, candidates)
    if candidate is not None:
        return _candidate_resolution(candidate, source)
    return ModelResolution(model_id=model_id, source=source, metadata=ModelMetadata())
