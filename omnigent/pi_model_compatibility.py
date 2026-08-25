"""Pi-specific model compatibility fallbacks absent from catalog metadata."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from enum import Enum
from typing import TYPE_CHECKING, NotRequired, TypedDict

from omnigent.model_metadata import ModelMetadata

if TYPE_CHECKING:
    from omnigent.model_catalog import ModelEntry

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
    """A Databricks AI Gateway protocol surface Pi can be pointed at.

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


class PiModelEntry(TypedDict):
    """One model as Pi's ``models.json`` declares it."""

    id: str
    input: NotRequired[list[str]]
    reasoning: NotRequired[bool]
    # Omitted when the catalog reports no limit; Pi then applies its own
    # defaults (128000 / 16384).
    contextWindow: NotRequired[int]
    maxTokens: NotRequired[int]


def pi_model_is_reasoning(model_id: str) -> bool:
    """Return whether *model_id* needs Pi's ``reasoning: true`` model flag.

    :param model_id: A model id, e.g. ``"system.ai.deepseek-v3"``.
    :returns: ``True`` when Pi must read the reasoning channel.
    """
    lower = model_id.lower()
    return any(fragment in lower for fragment in PI_REASONING_MODEL_FRAGMENTS)


def pi_model_json_entry(model: ModelEntry) -> PiModelEntry:
    """Translate normalized catalog metadata into Pi's ``models.json`` schema.

    Shared by every surface that renders a Pi ``models.json`` — the spawned
    harness in :mod:`omnigent.inner.pi_executor` and the interactive
    ``omnigent pi`` launch in :mod:`omnigent.pi_native_credentials` — so both
    advertise the same limits for the same model. Pi defaults an entry with no
    ``contextWindow``/``maxTokens`` to 128000/16384, which silently truncates
    the 1M-context gateway models, so limits are carried whenever the catalog
    reports them.

    :param model: A catalog entry, normally from
        :func:`~omnigent.model_catalog.fetch_databricks_model_service_entries`
        enriched by :func:`enrich_databricks_model_catalog`.
    :returns: A Pi model entry, e.g.
        ``{"id": ..., "input": ["text", "image"], "contextWindow": 1000000}``.
    """
    entry: PiModelEntry = {"id": model.id, "input": ["text", "image"]}
    if model.metadata.context_window is not None:
        entry["contextWindow"] = model.metadata.context_window
    if model.metadata.max_output_tokens is not None:
        entry["maxTokens"] = model.metadata.max_output_tokens
    if pi_model_is_reasoning(model.id):
        entry["reasoning"] = True
    return entry


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
    gaps.

    :param discovered: Live workspace entries.
    :param metadata_models: MLflow catalog entries, e.g. from
        :func:`~omnigent.model_catalog.catalog_model_entries`.
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
            enriched.append(model)
            continue
        live = model.metadata
        enriched.append(
            replace(
                model,
                metadata=ModelMetadata(
                    supported_capabilities=(
                        live.supported_capabilities or metadata.supported_capabilities
                    ),
                    unsupported_capabilities=(
                        live.unsupported_capabilities or metadata.unsupported_capabilities
                    ),
                    context_window=(
                        live.context_window
                        if live.context_window is not None
                        else metadata.context_window
                    ),
                    max_output_tokens=(
                        live.max_output_tokens
                        if live.max_output_tokens is not None
                        else metadata.max_output_tokens
                    ),
                    cost_tier=(
                        live.cost_tier if live.cost_tier is not None else metadata.cost_tier
                    ),
                    wire_apis=live.wire_apis or metadata.wire_apis,
                ),
            )
        )
    return tuple(enriched)
