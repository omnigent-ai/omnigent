"""Gate ``reasoning_effort`` using known and learned provider rejections.

Known model rejections are skipped at any endpoint. New rejections are learned
only after a successful stripped retry and are scoped to the effective endpoint.
Unlisted models are tried optimistically, so seeds save a round trip but are
not required for correctness.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

# Exact model IDs: a prefix would also suppress supported grok-4.x models.
_SEED_REJECTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("xai", "grok-4"),
        ("xai", "grok-code-fast-1"),
        ("xai", "grok-4-fast-reasoning"),
    }
)

# Learned rejections are scoped to the endpoint that returned the 400.
_learned_rejections: set[tuple[str, str, str]] = set()


def gating_identity(model: str, base_url: str = "") -> tuple[str, str]:
    """Split a prefixed model, or infer its provider from a known endpoint."""
    if "/" in model:
        provider, bare = model.split("/", 1)
        return provider.lower(), bare
    return _provider_for_base_url(base_url) or "openai", model


def _provider_for_base_url(base_url: str) -> str | None:
    """Match the URL host against known provider endpoints."""
    from omnigent.llms.routing import PROVIDER_CONFIGS

    try:
        host = urlparse(base_url).hostname
    except ValueError:
        return None
    if not host:
        return None
    for provider, url in PROVIDER_CONFIGS.items():
        if url and urlparse(url).hostname == host:
            return provider
    return None


def _endpoint_key(endpoint: str) -> str:
    """Use the lowercased network location as a rejection-cache key."""
    try:
        return (urlparse(endpoint).netloc or "").lower()
    except ValueError:
        return ""


def accepts_reasoning_effort(provider: str, model: str, endpoint: str = "") -> bool:
    """Skip seeded models everywhere and learned rejections at their endpoint."""
    pair = (provider, model.lower())
    if pair in _SEED_REJECTIONS:
        return False
    return (_endpoint_key(endpoint), *pair) not in _learned_rejections


# Value-validation errors can mention "support" without rejecting the parameter.
_CAPABILITY_REJECTION_PHRASES = ("not supported", "does not support", "unsupported")

# Do not strip on value errors such as "Unsupported value 'xhigh'".
_VALUE_REJECTION_PHRASES = ("value", "must be one of")


def is_reasoning_effort_rejection(exc: Exception) -> bool:
    """Match HTTP 400s that reject the parameter, not its value.

    Both httpx and OpenAI SDK errors expose the provider response.
    """
    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 400:
        return False
    try:
        body = response.text.lower()
    except Exception:  # an unreadable body is not a param rejection
        return False
    if "reasoning_effort" not in body.replace("reasoningeffort", "reasoning_effort"):
        return False
    if any(phrase in body for phrase in _VALUE_REJECTION_PHRASES):
        return False
    return any(phrase in body for phrase in _CAPABILITY_REJECTION_PHRASES)


def record_reasoning_effort_rejection(provider: str, model: str, endpoint: str = "") -> None:
    """Cache a confirmed rejection for this model and endpoint."""
    key = (_endpoint_key(endpoint), provider, model.lower())
    if key in _learned_rejections:
        return
    _learned_rejections.add(key)
    _logger.warning(
        "%s/%s (endpoint %s) rejected reasoning_effort (HTTP 400); retrying "
        "without it and omitting it for this model at this endpoint from now "
        "on. If this repeats across runs, seed the model in "
        "reasoning_effort_support to skip the wasted call.",
        provider,
        model,
        endpoint or "default",
    )


def strip_rejected_reasoning_effort(
    extra: dict[str, Any],
    exc: Exception,
) -> dict[str, Any] | None:
    """Return a stripped copy only when the provider rejects the parameter.

    The caller learns the rejection only after the stripped retry succeeds.
    """
    if "reasoning_effort" not in extra or not is_reasoning_effort_rejection(exc):
        return None
    return {k: v for k, v in extra.items() if k != "reasoning_effort"}


def clear_learned_rejections() -> None:
    """Reset the learned-rejection cache. Useful for tests."""
    _learned_rejections.clear()
