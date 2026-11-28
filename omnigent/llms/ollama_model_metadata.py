"""Normalize Ollama ``/api/show`` capability metadata."""

from __future__ import annotations

from typing import Any

from omnigent.models.model_metadata import (
    ModelCapability,
    ModelMetadata,
    ModelReasoningMetadata,
    ModelReasoningMode,
)
from omnigent.util.reasoning_effort import EFFORT_VALUES

# The canonical effort for Ollama's ``false`` ("no thinking") in ``thinking.values``.
_OLLAMA_THINKING_OFF = "none"


def looks_like_ollama_show(payload: object) -> bool:
    """Whether *payload* has the shape of an Ollama ``/api/show`` response.

    Other OpenAI-compatible servers can answer the same path with an error
    page or unrelated JSON; only a ``capabilities`` list identifies Ollama.
    """
    return isinstance(payload, dict) and isinstance(payload.get("capabilities"), list)


def parse_ollama_model_metadata(payload: dict[str, Any]) -> ModelMetadata:
    """Convert one Ollama ``/api/show`` response to normalized metadata.

    Reasoning support comes from the ``thinking`` capability. The accepted
    effort levels come from ``thinking.values``, e.g. ``[false, "low",
    "medium"]``; a model whose values are only booleans is an on/off toggle.

    :param payload: The decoded ``/api/show`` body.
    :returns: Metadata with reasoning support and efforts filled where reported;
        unknown fields stay unknown.
    """
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, list):
        return ModelMetadata()
    if "thinking" not in capabilities:
        return ModelMetadata(unsupported_capabilities=frozenset({ModelCapability.REASONING}))

    thinking = payload.get("thinking")
    values = thinking.get("values") if isinstance(thinking, dict) else None
    reasoning: ModelReasoningMetadata | None = None
    if isinstance(values, list):
        efforts = {value for value in values if isinstance(value, str) and value in EFFORT_VALUES}
        if any(value is False for value in values):
            efforts.add(_OLLAMA_THINKING_OFF)
        named_levels = efforts - {_OLLAMA_THINKING_OFF}
        if named_levels:
            reasoning = ModelReasoningMetadata(efforts=frozenset(efforts))
        elif any(value is True for value in values):
            reasoning = ModelReasoningMetadata(
                modes=frozenset({ModelReasoningMode.TOGGLE}),
                efforts=frozenset(efforts),
            )
    return ModelMetadata(
        supported_capabilities=frozenset({ModelCapability.REASONING}),
        reasoning=reasoning,
    )
