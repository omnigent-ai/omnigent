"""Provider-neutral metadata used for model selection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


def concrete_reported_model(value: object) -> str | None:
    """Keep model reports verbatim, excluding Claude Code's local-error marker."""
    if not isinstance(value, str):
        return None
    model = value.strip()
    return model if model and model != "<synthetic>" else None


class ModelIntent(str, Enum):
    """Stable purpose requested by a model-selection caller."""

    DEFAULT = "default"
    FAST = "fast"
    BALANCED = "balanced"
    POWERFUL = "powerful"


class ModelCapability(str, Enum):
    """A model feature that can be required without inspecting its id."""

    TOOL_USE = "tool-use"
    REASONING = "reasoning"
    VISION = "vision"
    IMAGE_GENERATION = "image-generation"
    STRUCTURED_OUTPUT = "structured-output"


class ModelCostTier(str, Enum):
    """Provider-relative cost tier used to rank compatible models."""

    ECONOMY = "economy"
    STANDARD = "standard"
    PREMIUM = "premium"


class ModelWireAPI(str, Enum):
    """Wire protocols a model endpoint is known to accept."""

    ANTHROPIC_MESSAGES = "anthropic-messages"
    OPENAI_CHAT = "openai-chat"
    OPENAI_RESPONSES = "openai-responses"
    GEMINI_GENERATE_CONTENT = "gemini-generate-content"
    BEDROCK_CONVERSE = "bedrock-converse"


class ModelReasoningMode(str, Enum):
    """Provider-neutral reasoning controls accepted by a model."""

    ADAPTIVE = "adaptive"
    FIXED_BUDGET = "fixed-budget"
    # Thinking switches on or off; the model has no effort ladder.
    TOGGLE = "toggle"


@dataclass(frozen=True)
class ModelReasoningMetadata:
    """Known reasoning modes and effort levels for one model."""

    modes: frozenset[ModelReasoningMode] = frozenset()
    efforts: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ModelMetadata:
    """Normalized, provider-neutral facts about one model.

    Capability support is tri-state. A capability in
    :attr:`supported_capabilities` is supported, one in
    :attr:`unsupported_capabilities` is known not to be supported, and one in
    neither set is unknown. Unknown is intentionally distinct from unsupported
    because many provider listing APIs return ids without capability metadata.
    """

    supported_capabilities: frozenset[ModelCapability] = frozenset()
    unsupported_capabilities: frozenset[ModelCapability] = frozenset()
    context_window: int | None = None
    max_output_tokens: int | None = None
    cost_tier: ModelCostTier | None = None
    wire_apis: frozenset[ModelWireAPI] = frozenset()
    reasoning: ModelReasoningMetadata | None = None

    def __post_init__(self) -> None:
        overlap = self.supported_capabilities & self.unsupported_capabilities
        if overlap:
            names = ", ".join(sorted(capability.value for capability in overlap))
            raise ValueError(f"capabilities cannot be both supported and unsupported: {names}")
        if self.context_window is not None and self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")

    def supports(self, capability: ModelCapability) -> bool | None:
        """Return known support for *capability*, or ``None`` when unknown."""
        if capability in self.supported_capabilities:
            return True
        if capability in self.unsupported_capabilities:
            return False
        return None


def merge_model_metadata(live: ModelMetadata, fallback: ModelMetadata) -> ModelMetadata:
    """Combine two descriptions of one model, preferring *live* per field.

    *live* is what the serving endpoint reports; *fallback* (e.g. a catalog)
    fills only the fields and capabilities *live* leaves unknown.

    :param live: Endpoint-reported metadata.
    :param fallback: Metadata used where *live* is silent.
    :returns: The merged metadata.
    """
    return ModelMetadata(
        supported_capabilities=(
            live.supported_capabilities
            | (fallback.supported_capabilities - live.unsupported_capabilities)
        ),
        unsupported_capabilities=(
            live.unsupported_capabilities
            | (fallback.unsupported_capabilities - live.supported_capabilities)
        ),
        context_window=(
            live.context_window if live.context_window is not None else fallback.context_window
        ),
        max_output_tokens=(
            live.max_output_tokens
            if live.max_output_tokens is not None
            else fallback.max_output_tokens
        ),
        cost_tier=live.cost_tier if live.cost_tier is not None else fallback.cost_tier,
        wire_apis=live.wire_apis or fallback.wire_apis,
        reasoning=live.reasoning or fallback.reasoning,
    )
