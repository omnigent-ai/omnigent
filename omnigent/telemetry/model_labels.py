"""Model id → coarse family/tier labels for routing telemetry.

A routing decision names a *servable model id*, which on Databricks is a
workspace serving-endpoint name (e.g. ``"acme-internal-review-llm"``).
Those names are user-defined, so no model string is ever shipped
verbatim. Both label functions are strict allowlists: they return a token
from a fixed tuple defined here, or ``"other"``. This mirrors the
``agent_name`` convention in :mod:`omnigent.telemetry.events`, where only
known built-in agent names are recorded.

A token must be a whole id segment (``-``/``_``/``.`` separated), bar a
trailing generation number, so ``"qwen3-coder"`` reads as ``qwen`` while
``"gemini-3-flash"`` does not read as the ``mini`` tier and
``"acme-project-zephyr"`` does not read as the ``pro`` tier.
"""

from __future__ import annotations

import re

#: Vendor families recognised in model ids.
_FAMILIES: tuple[str, ...] = (
    "claude",
    "gpt",
    "codex",
    "glm",
    "gemini",
    "llama",
    "qwen",
    "kimi",
    "deepseek",
    "mistral",
    "grok",
)

#: Capability tiers recognised in model ids. ``"mini"``/``"nano"`` are
#: OpenAI spellings; the rest are Anthropic/Google.
_TIERS: tuple[str, ...] = (
    "opus",
    "sonnet",
    "haiku",
    "fable",
    "mini",
    "nano",
    "flash",
    "pro",
    "max",
)

_SEGMENT_SPLIT = re.compile(r"[^a-z0-9]+")


def _match(model: str | None, tokens: tuple[str, ...]) -> str | None:
    """Return the first token in *tokens* naming a segment of *model*.

    :param model: Servable model id. ``None`` or empty yields ``None``.
    :param tokens: Allowlisted tokens to look for.
    :returns: The matching token, ``"other"`` when none matches, or
        ``None`` when there is no model.
    """
    if not model:
        return None
    segments = {seg.rstrip("0123456789") for seg in _SEGMENT_SPLIT.split(model.lower())}
    return next((token for token in tokens if token in segments), "other")


def model_family(model: str | None) -> str | None:
    """Return the vendor family token for *model*.

    :param model: Servable model id, e.g. ``"databricks-claude-opus-4-8"``.
    :returns: A token from :data:`_FAMILIES`, ``"other"`` when none
        matches, or ``None`` when there is no model.
    """
    return _match(model, _FAMILIES)


def model_tier(model: str | None) -> str | None:
    """Return the capability tier token for *model*.

    :param model: Servable model id, e.g. ``"databricks-claude-opus-4-8"``.
    :returns: A token from :data:`_TIERS`, ``"other"`` when none matches,
        or ``None`` when there is no model.
    """
    return _match(model, _TIERS)
