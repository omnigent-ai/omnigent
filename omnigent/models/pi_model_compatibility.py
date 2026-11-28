"""Pi-specific model compatibility fallbacks absent from catalog metadata."""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from enum import Enum
from typing import TYPE_CHECKING, NotRequired, TypedDict

from omnigent.models.claude_model_vocabulary import canonical_claude_id, normalized_model_id
from omnigent.models.model_fallbacks import (
    PI_ANTHROPIC_ADAPTIVE_THINKING,
    PI_ANTHROPIC_BUDGET_THINKING,
    PI_ANTHROPIC_THINKING_REQUIRED,
)
from omnigent.models.model_metadata import (
    ModelCapability,
    ModelMetadata,
    ModelReasoningMetadata,
    ModelReasoningMode,
    merge_model_metadata,
)

if TYPE_CHECKING:
    from omnigent.models.model_catalog import ModelEntry

# These system models omit the finish reason Pi requires on Chat Completions.
# ``glm-`` avoids matching vendor-direct ids without a system.ai alias.
SYSTEM_AI_RESPONSES_KEYWORDS: tuple[str, ...] = ("kimi", "inkling", "qwen3", "glm-")


def unsupported_in_pi(model_id_lower: str) -> bool:
    """Return whether Pi cannot parse the model's response content.

    These endpoints return typed-array content that Pi renders as
    ``[object Object]``; their available alternate wires do not fix it.
    """
    return "gemini-2-5" in model_id_lower or "gpt-oss" in model_id_lower


class DatabricksPiSurface(Enum):
    """A Databricks Unity Gateway protocol surface Pi can be pointed at.

    The gateway serves each protocol under the same workspace origin, and each
    model accepts only some of them; sending a model to the wrong surface is
    rejected with "API type ... is not supported by ...".
    """

    ANTHROPIC = "anthropic"
    RESPONSES = "responses"
    COMPLETIONS = "completions"
    MLFLOW = "mlflow"


def databricks_pi_surface_for_model(model_id: str) -> DatabricksPiSurface:
    """Classify *model_id* onto its Databricks gateway surface by family.

    A last-resort fallback for when the live model-services catalog is
    unavailable: the catalog carries authoritative per-endpoint capabilities and
    is always preferred.

    The keyword surface split applies only to ``system.ai.*`` ids: the gateway
    serves Responses passthrough for ``system.ai.glm-5-2`` but rejects it for the
    ``databricks-glm-5-2`` alias of the same model, so an alias goes to chat.

    :param model_id: Gateway or Unity Catalog model id, any case.
    :returns: The surface whose protocol the model's family accepts.
    """
    lower = model_id.lower()
    if "claude" in lower:
        return DatabricksPiSurface.ANTHROPIC
    if lower.startswith("system.ai."):
        # system.ai.* ids are not routable at /serving-endpoints; the ones whose
        # chat surface omits Pi's required finish reason need Responses instead.
        if any(keyword in lower for keyword in SYSTEM_AI_RESPONSES_KEYWORDS):
            return DatabricksPiSurface.RESPONSES
        return DatabricksPiSurface.MLFLOW
    if "gpt" in lower:
        # Unknown GPT metadata fails toward Responses — the forward-compatible
        # tool-capable surface.
        return DatabricksPiSurface.RESPONSES
    return DatabricksPiSurface.COMPLETIONS


# DeepSeek streams its chain of thought on ``reasoning_content``; Pi only reads
# that channel when the model entry sets ``reasoning``. GLM/kimi/inkling route
# through the Responses API instead and do not need it.
PI_REASONING_MODEL_FRAGMENTS: tuple[str, ...] = ("deepseek",)

# Claude models support extended thinking; reasoning:true enables Pi's controls.
# Adaptive versus budget-based thinking is configured per model.
PI_CLAUDE_THINKING_MODEL_FRAGMENTS: tuple[str, ...] = ("claude",)


class PiModelEntry(TypedDict):
    """One model as Pi's ``models.json`` declares it."""

    id: str
    input: NotRequired[list[str]]
    reasoning: NotRequired[bool]
    thinkingLevelMap: NotRequired[dict[str, str | None]]
    compat: NotRequired[dict[str, bool]]
    # Omitted when the catalog reports no limit; Pi then applies its own
    # defaults (128000 / 16384).
    contextWindow: NotRequired[int]
    maxTokens: NotRequired[int]


def pi_model_with_thinking_defaults(entry: PiModelEntry) -> PiModelEntry:
    """Fill Anthropic defaults, respecting explicit level maps and compatibility flags.

    Normalize known ID variants for matching only; preserve the provider's actual ID.
    """
    model = normalized_model_id(canonical_claude_id(entry["id"]) or entry["id"])
    model = re.sub(r"(?:-\d{8})?(?:-v\d+:\d+)?$", "", model)
    compat = entry.get("compat", {})
    for fallback, adaptive in (
        (PI_ANTHROPIC_ADAPTIVE_THINKING, True),
        (PI_ANTHROPIC_BUDGET_THINKING, False),
    ):
        if model in fallback.model_ids and "forceAdaptiveThinking" not in compat:
            entry = {
                **entry,
                "reasoning": True,
                "compat": {**compat, "forceAdaptiveThinking": adaptive},
            }
    if model in PI_ANTHROPIC_THINKING_REQUIRED.model_ids and "thinkingLevelMap" not in entry:
        return {**entry, "reasoning": True, "thinkingLevelMap": {"off": None}}
    return entry


def pi_model_is_reasoning(model_id: str) -> bool:
    """Return whether *model_id* needs Pi's ``reasoning: true`` model flag.

    :param model_id: A model id, e.g. ``"system.ai.deepseek-v3"``.
    :returns: ``True`` when Pi must read the reasoning channel.
    """
    lower = model_id.lower()
    return any(fragment in lower for fragment in PI_REASONING_MODEL_FRAGMENTS)


def pi_model_entry_is_reasoning(model: ModelEntry) -> bool:
    """Whether a catalog entry needs Pi's ``reasoning`` flag: catalog capability or id floor."""
    return (
        model.metadata.supports(ModelCapability.REASONING) is True
        or pi_model_is_reasoning(model.id)
        or any(fragment in model.id.lower() for fragment in PI_CLAUDE_THINKING_MODEL_FRAGMENTS)
    )


def pi_model_json_entry(model: ModelEntry) -> PiModelEntry:
    """Translate normalized catalog metadata into Pi's ``models.json`` schema.

    Shared by every surface that renders a Pi ``models.json`` — the spawned
    harness in :mod:`omnigent.inner.pi_executor` and the interactive
    ``omnigent pi`` launch in :mod:`omnigent.harnesses.pi_native.credentials` — so both
    advertise the same limits and reasoning flag for the same model. Pi
    defaults an entry with no ``contextWindow``/``maxTokens`` to 128000/16384,
    which silently truncates the 1M-context gateway models, so limits are
    carried whenever the catalog reports them.

    :param model: A catalog entry, normally from
        :func:`~omnigent.models.model_catalog.fetch_databricks_model_service_entries`
        enriched by :func:`enrich_databricks_model_catalog`.
    :returns: A Pi model entry, e.g.
        ``{"id": ..., "input": ["text", "image"], "contextWindow": 1000000}``.
    """
    entry: PiModelEntry = {"id": model.id, "input": ["text", "image"]}
    if model.metadata.context_window is not None:
        entry["contextWindow"] = model.metadata.context_window
    if model.metadata.max_output_tokens is not None:
        entry["maxTokens"] = model.metadata.max_output_tokens
    if pi_model_entry_is_reasoning(model):
        entry["reasoning"] = True
    return entry


# When the endpoint lists no levels, Pi offers off, low, medium and high only:
# xhigh and max stay hidden because nothing says the model supports them, and
# minimal is left out because not every server accepts it.
_PI_UNREPORTED_LEVELS: dict[str, str | None] = {"off": "none", "minimal": None}


def pi_reported_thinking_level_map(
    reasoning: ModelReasoningMetadata | None,
) -> dict[str, str | None]:
    """Map Pi's thinking levels onto the reasoning controls an endpoint reported.

    Pi offers ``off`` through ``high`` unless a level maps to ``None``, and offers
    ``xhigh``/``max`` only when mapped. ``off`` is sent as ``"none"`` explicitly
    because Pi's Chat Completions client otherwise omits it and the model thinks.

    :param reasoning: The endpoint-reported controls, or ``None`` when they are
        unknown.
    :returns: A ``thinkingLevelMap``, e.g. ``{"off": "none", "high": None,
        "xhigh": "xhigh"}`` for a model without a ``high`` level.
    """
    efforts = reasoning.efforts if reasoning is not None else frozenset()
    if reasoning is not None and ModelReasoningMode.TOGGLE in reasoning.modes:
        # One "on" level; medium is Pi's default, so new sessions need no clamp.
        off = "none" if "none" in efforts else None
        return {"off": off, "minimal": None, "low": None, "high": None}
    if not efforts - {"none"}:
        return dict(_PI_UNREPORTED_LEVELS)
    level_map: dict[str, str | None] = {"off": "none" if "none" in efforts else None}
    for level in ("minimal", "low", "medium", "high"):
        if level not in efforts:
            level_map[level] = None
    for level in ("xhigh", "max"):
        if level in efforts:
            level_map[level] = level
    return level_map


def pi_generic_model_json_entry(
    model_id: str,
    *,
    provider_metadata: ModelMetadata | None = None,
    configured_context_window: int | None = None,
    configured_max_output_tokens: int | None = None,
) -> PiModelEntry:
    """Render a configured model with catalog capabilities and token limits.

    Native and SDK providers share the same metadata even when the user supplies
    both limits. Configured limits override catalog values; an unavailable
    catalog retains the model and Pi's defaults. Metadata the serving endpoint
    reports (*provider_metadata*) wins over the catalog, and its reasoning
    controls become the entry's ``thinkingLevelMap``.
    """
    from omnigent.models.model_catalog import ModelEntry

    metadata = _pi_catalog_metadata(model_id)
    if provider_metadata is not None:
        metadata = merge_model_metadata(provider_metadata, metadata)
    entry = pi_model_json_entry(ModelEntry(id=model_id, family="other", metadata=metadata))
    if (
        provider_metadata is not None
        and provider_metadata.supports(ModelCapability.REASONING) is True
        and "thinkingLevelMap" not in entry
    ):
        entry["thinkingLevelMap"] = pi_reported_thinking_level_map(provider_metadata.reasoning)
    if configured_context_window is not None:
        entry["contextWindow"] = configured_context_window
    if configured_max_output_tokens is not None:
        entry["maxTokens"] = configured_max_output_tokens
    return entry


def _pi_catalog_metadata(model_id: str) -> ModelMetadata:
    """Resolve Pi metadata through shared alias lookup, accepting unambiguous fields."""
    from omnigent.onboarding.providers import find_catalog_models

    matches = []
    with suppress(OSError):
        matches = find_catalog_models(model_id)
    reasoning = {model.supports_reasoning for model in matches}
    contexts = {model.max_input_tokens for model in matches if model.max_input_tokens is not None}
    outputs = {model.max_output_tokens for model in matches if model.max_output_tokens is not None}
    return ModelMetadata(
        supported_capabilities=(
            frozenset({ModelCapability.REASONING}) if reasoning == {True} else frozenset()
        ),
        unsupported_capabilities=(
            frozenset({ModelCapability.REASONING}) if reasoning == {False} else frozenset()
        ),
        context_window=next(iter(contexts)) if len(contexts) == 1 else None,
        max_output_tokens=next(iter(outputs)) if len(outputs) == 1 else None,
    )


def databricks_model_aliases(model_id: str) -> frozenset[str]:
    """Return equivalent Unity Catalog and serving-endpoint model ids.

    :param model_id: Either spelling, e.g. ``"system.ai.claude-opus"`` or
        ``"databricks-claude-opus"``.
    :returns: The lowercased id plus its counterpart spelling.
    """
    normalized = model_id.lower()
    aliases = {normalized}
    if normalized.startswith("system.ai."):
        aliases.add(f"databricks-{normalized.removeprefix('system.ai.')}")
    elif normalized.startswith("databricks-"):
        aliases.add(f"system.ai.{normalized.removeprefix('databricks-')}")
    return frozenset(aliases)


def enrich_databricks_model_catalog(
    discovered: Sequence[ModelEntry],
    metadata_models: Sequence[ModelEntry],
) -> tuple[ModelEntry, ...]:
    """Add MLflow limits and capabilities to live workspace models.

    The workspace's model-service listing is authoritative for *availability*
    but reports no token limits; the MLflow catalog reports limits but not what
    this workspace serves. Live values win per field; catalog values fill the
    gaps. Models absent from the Databricks catalog use the shared vendor/alias
    lookup already used by configured Pi providers.

    :param discovered: Live workspace entries.
    :param metadata_models: MLflow catalog entries, e.g. from
        :func:`~omnigent.models.model_catalog.catalog_model_entries`.
    :returns: The discovered entries with metadata filled in.
    """
    metadata_by_alias = {
        alias: model.metadata
        for model in metadata_models
        for alias in databricks_model_aliases(model.id)
    }
    enriched: list[ModelEntry] = []
    for model in discovered:
        metadata = next(
            (
                metadata_by_alias[alias]
                for alias in databricks_model_aliases(model.id)
                if alias in metadata_by_alias
            ),
            None,
        )
        if metadata is None:
            metadata = _pi_catalog_metadata(model.id)
        enriched.append(replace(model, metadata=merge_model_metadata(model.metadata, metadata)))
    return tuple(enriched)
