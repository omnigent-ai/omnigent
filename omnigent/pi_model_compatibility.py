"""Pi-specific model compatibility fallbacks absent from catalog metadata."""

from __future__ import annotations

from enum import Enum

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
