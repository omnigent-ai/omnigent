"""Normalize Anthropic Models API capability metadata."""

from __future__ import annotations

from typing import Any

from omnigent.model_metadata import (
    ModelCapability,
    ModelMetadata,
    ModelReasoningMetadata,
    ModelReasoningMode,
    ModelWireAPI,
)


def parse_anthropic_model_metadata(payload: dict[str, Any]) -> ModelMetadata:
    """Convert one Anthropic Models API object to normalized metadata."""
    capabilities = payload.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}

    supported: set[ModelCapability] = set()
    unsupported: set[ModelCapability] = set()
    for key, capability in (
        ("thinking", ModelCapability.REASONING),
        ("image_input", ModelCapability.VISION),
        ("structured_outputs", ModelCapability.STRUCTURED_OUTPUT),
    ):
        value = capabilities.get(key)
        if not isinstance(value, dict) or not isinstance(value.get("supported"), bool):
            continue
        (supported if value["supported"] else unsupported).add(capability)

    thinking = capabilities.get("thinking")
    effort = capabilities.get("effort")
    reasoning: ModelReasoningMetadata | None = None
    if isinstance(thinking, dict) or isinstance(effort, dict):
        types = thinking.get("types") if isinstance(thinking, dict) else None
        types = types if isinstance(types, dict) else {}
        modes = {
            mode
            for api_name, mode in (
                ("adaptive", ModelReasoningMode.ADAPTIVE),
                ("enabled", ModelReasoningMode.FIXED_BUDGET),
            )
            if isinstance(types.get(api_name), dict) and types[api_name].get("supported") is True
        }
        efforts = {
            name
            for name, value in (effort.items() if isinstance(effort, dict) else ())
            if name != "supported" and isinstance(value, dict) and value.get("supported") is True
        }
        reasoning = ModelReasoningMetadata(
            modes=frozenset(modes),
            efforts=frozenset(efforts),
        )

    max_input_tokens = payload.get("max_input_tokens")
    return ModelMetadata(
        supported_capabilities=frozenset(supported),
        unsupported_capabilities=frozenset(unsupported),
        context_window=max_input_tokens if isinstance(max_input_tokens, int) else None,
        wire_apis=frozenset({ModelWireAPI.ANTHROPIC_MESSAGES}),
        reasoning=reasoning,
    )
