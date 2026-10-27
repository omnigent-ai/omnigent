"""Normalize LiteLLM proxy model metadata.

A LiteLLM proxy answers the OpenAI-compatible ``/v1/models`` listing with a
few extra per-model fields (``mode``, ``max_input_tokens``) and serves the
full capability record — function calling, reasoning, and the per-level
``supports_*_reasoning_effort`` flags — at ``/v1/model/info``. Plain
``/v1/models`` pages carry ids only, so without this the launch picker can
neither drop non-chat routes (embeddings, transcription) nor offer an effort
ladder for gateway-listed models.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from omnigent.models.model_metadata import (
    ModelCapability,
    ModelMetadata,
    ModelReasoningMetadata,
    ModelReasoningMode,
    ModelWireAPI,
)
from omnigent.util.reasoning_effort import EFFORT_VALUES

# Listing fields only a LiteLLM proxy adds to an OpenAI ``/v1/models`` row.
_LITELLM_LISTING_KEYS = ("mode", "max_input_tokens")

# Modes a coding harness can converse with; anything else (``embedding``,
# ``audio_transcription``, ``image_generation``, ``rerank``, …) has no tool use.
_CONVERSATIONAL_MODES = frozenset({"chat", "completion", "responses"})

# A reasoning model accepts these unless its record says otherwise.
_BASE_EFFORTS = ("low", "medium", "high")

# ``supports_<effort>_reasoning_effort`` flags: True adds the level, False
# removes it (``low`` is in the base ladder, the rest are opt-in). The
# ``none``/``minimal`` flags are not read: they describe the provider API,
# but harnesses validate efforts against their own catalogs (codex lists no
# ``none`` for the same GPT models), and "no reasoning override" is already
# the picker's unselected state. A shorter ladder is safe; an extra level can
# fail the launch.
_EFFORT_FLAGS = ("low", "xhigh", "max")


def looks_like_litellm_listing(items: Iterable[object]) -> bool:
    """Whether a ``/v1/models`` page carries LiteLLM's extra per-model fields.

    :param items: The page's ``data`` rows.
    :returns: True when any row has a LiteLLM-only key, e.g. ``mode``.
    """
    return any(
        isinstance(item, dict) and any(key in item for key in _LITELLM_LISTING_KEYS)
        for item in items
    )


def index_litellm_model_info(payload: object) -> dict[str, dict[str, Any]]:
    """Map each ``model_name`` in a ``/v1/model/info`` payload to its ``model_info``.

    A name served by several deployments appears once per deployment; the
    first record wins, matching the order LiteLLM lists its router entries.

    :param payload: The decoded ``/v1/model/info`` response body.
    :returns: ``{"gpt-6.1-sol": {"mode": "responses", ...}, ...}``.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    indexed: dict[str, dict[str, Any]] = {}
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):
            continue
        name = row.get("model_name")
        info = row.get("model_info")
        if isinstance(name, str) and name and isinstance(info, dict):
            indexed.setdefault(name, info)
    return indexed


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _reasoning(info: Mapping[str, Any]) -> ModelReasoningMetadata | None:
    levels = info.get("reasoning_effort_levels")
    if isinstance(levels, list) and levels:
        efforts = {level for level in levels if isinstance(level, str)}
    elif info.get("supports_reasoning") is True:
        efforts = set(_BASE_EFFORTS)
        for effort in _EFFORT_FLAGS:
            flag = info.get(f"supports_{effort}_reasoning_effort")
            if flag is True:
                efforts.add(effort)
            elif flag is False:
                efforts.discard(effort)
    else:
        return None
    modes = (
        frozenset({ModelReasoningMode.ADAPTIVE})
        if info.get("supports_adaptive_thinking") is True
        else frozenset()
    )
    return ModelReasoningMetadata(modes=modes, efforts=frozenset(efforts & EFFORT_VALUES))


def _wire_apis(mode: object, provider: object) -> frozenset[ModelWireAPI]:
    # LiteLLM bridges every conversational route onto both OpenAI wires, but
    # only an Anthropic-backed route keeps Messages semantics (thinking,
    # cache_control) end to end, so only those advertise that wire.
    if mode not in _CONVERSATIONAL_MODES:
        return frozenset()
    wires = {ModelWireAPI.OPENAI_CHAT, ModelWireAPI.OPENAI_RESPONSES}
    if provider == "anthropic":
        wires.add(ModelWireAPI.ANTHROPIC_MESSAGES)
    return frozenset(wires)


def parse_litellm_model_metadata(
    listing_row: Mapping[str, Any], info: Mapping[str, Any] | None
) -> ModelMetadata:
    """Convert one LiteLLM model's listing row and info record to metadata.

    :param listing_row: The model's ``/v1/models`` row, e.g.
        ``{"id": "gpt-6.1-sol", "mode": "responses", "max_input_tokens": 922000}``.
    :param info: Its ``/v1/model/info`` ``model_info`` record, or ``None``
        when that endpoint was unavailable or does not name the model.
    :returns: Normalized metadata; unknown fields stay unknown.
    """
    record: Mapping[str, Any] = info or {}
    # LiteLLM routes a deployment with no ``mode`` as chat.
    mode = record.get("mode") or listing_row.get("mode") or ("chat" if info else None)

    supported: set[ModelCapability] = set()
    unsupported: set[ModelCapability] = set()
    if isinstance(mode, str) and mode not in _CONVERSATIONAL_MODES:
        unsupported.add(ModelCapability.TOOL_USE)
    for key, capability in (
        ("supports_function_calling", ModelCapability.TOOL_USE),
        ("supports_reasoning", ModelCapability.REASONING),
        ("supports_vision", ModelCapability.VISION),
    ):
        value = record.get(key)
        if value is True and capability not in unsupported:
            supported.add(capability)
        elif value is False:
            unsupported.add(capability)

    return ModelMetadata(
        supported_capabilities=frozenset(supported),
        unsupported_capabilities=frozenset(unsupported),
        context_window=_positive_int(record.get("max_input_tokens"))
        or _positive_int(listing_row.get("max_input_tokens")),
        max_output_tokens=_positive_int(record.get("max_output_tokens"))
        or _positive_int(listing_row.get("max_output_tokens")),
        wire_apis=_wire_apis(mode, record.get("litellm_provider")) if info else frozenset(),
        reasoning=_reasoning(record),
    )
