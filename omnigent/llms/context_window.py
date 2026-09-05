"""
Context window resolution for LLM models.

Provides :func:`get_model_context_window` which resolves a model's context
window from the shared MLflow catalog, then optional local metadata, with a
conservative 128K fallback.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from omnigent.onboarding.providers import ModelInfo, find_catalog_models

_DEFAULT_CONTEXT_WINDOW: int = 128_000

_ANTHROPIC_1M_BETA_SUFFIX = "[1m]"
_ANTHROPIC_1M_BETA_WINDOW = 1_000_000


class _LiteLLM(Protocol):
    def get_model_info(self, model: str) -> Mapping[str, object]: ...


def _encoded_context_window(model: str) -> int | None:
    """Read a context size explicitly encoded in a provider model id."""
    bare = model.rsplit("/", 1)[-1].split(":", 1)[0].strip().lower()
    if "claude" in bare and bare.endswith(_ANTHROPIC_1M_BETA_SUFFIX):
        return _ANTHROPIC_1M_BETA_WINDOW
    return None


def _catalog_context_window(model: str) -> int | None:
    """Return catalog context metadata when every matching entry agrees."""
    windows = {
        candidate.max_input_tokens
        for candidate in find_catalog_models(model)
        if candidate.max_input_tokens is not None
    }
    if len(windows) == 1:
        return next(iter(windows))
    return None


# Fallback cache pricing as a multiple of the plain input rate, used when the
# catalog publishes no explicit cache rate for a model (e.g. ``databricks-*``
# entries today omit them). Both providers we serve publish the same ratios:
# a cache *read* (cache hit) bills at ~10% of input — OpenAI gpt-5 0.125/1.25,
# gpt-5-mini 0.075/0.75, Anthropic sonnet 0.30/3.00 are all exactly 0.10 — and
# an Anthropic cache *write* (5-minute cache creation) bills at 1.25× input
# (sonnet 3.75/3.00). OpenAI has no separate write charge and reports no
# cache-creation tokens, so the write multiplier applies to a ~0 bucket there.
# Far closer than the old "bill cache at full input rate" fallback, which
# over-charged cache reads ~10×.
_FALLBACK_CACHE_READ_INPUT_RATIO: float = 0.10
_FALLBACK_CACHE_WRITE_INPUT_RATIO: float = 1.25


def get_model_context_window(model: str) -> int:
    """
    Look up the model's context window size in tokens.

    Resolution order:

    1. ``AP_CONTEXT_WINDOW_OVERRIDE`` env var — overrides everything.
       Supports custom/self-hosted models and e2e compaction tests.
    2. :func:`_encoded_context_window` — explicit provider metadata carried
       in the model id, currently Anthropic's ``[1m]`` beta marker.
    3. Shared MLflow catalog metadata. This is the authoritative dynamic
       source and reuses the onboarding/model-resolver cache.
    4. ``litellm.get_model_info()`` — optional local metadata. Also
       tried with the ``databricks/`` prefix for Databricks models.
    5. ``_DEFAULT_CONTEXT_WINDOW`` (128 K) — conservative fallback.

    :param model: The model identifier, e.g. ``"openai/gpt-4o"`` or
        ``"databricks-gpt-5-5"``.
    :returns: Context window size in tokens.
    """
    override = os.environ.get("AP_CONTEXT_WINDOW_OVERRIDE")
    if override is not None:
        return int(override)
    encoded = _encoded_context_window(model)
    if encoded is not None:
        return encoded
    catalog_window = _catalog_context_window(model)
    if catalog_window is not None:
        return catalog_window
    try:
        litellm = cast(_LiteLLM, importlib.import_module("litellm"))
    except ImportError:
        return _DEFAULT_CONTEXT_WINDOW
    try:
        info = litellm.get_model_info(model)
        if info:
            limit = info.get("max_input_tokens")
            if isinstance(limit, (int, float, str)) and limit:
                return int(limit)
    except Exception:
        pass
    if model.startswith("databricks-"):
        try:
            info = litellm.get_model_info(f"databricks/{model}")
            if info:
                limit = info.get("max_input_tokens")
                if isinstance(limit, (int, float, str)) and limit:
                    return int(limit)
        except Exception:
            pass
    return _DEFAULT_CONTEXT_WINDOW


def resolve_effective_context_window(
    spec_context_window: int | None,
    model: str | None,
    *,
    model_override: str | None = None,
) -> int | None:
    """
    Resolve the context window to use for compaction budgeting.

    Prefers an explicit, spec-declared window (``executor.context_window``)
    over the model-catalog lookup. An agent author who declares a window is
    stating the size the model actually serves for this agent (e.g. a 1M
    Claude window); the catalog lookup falls back to a conservative 128K
    default for models it can't resolve, which would otherwise compact far
    too early.

    Mirrors the server's display ring (``server/routes/sessions.py``):
    ``executor.context_window`` describes only the *spec* model, so an active
    ``model_override`` bypasses the declared window and sizes against the
    override model's real catalog window instead. Without this, overriding a
    1M-window agent down to a small-window model would budget compaction
    against 1M and under-compact past the real model's limit.

    :param spec_context_window: ``executor.context_window`` from the spec,
        or ``None`` when the author declared no explicit window.
    :param model: The spec-declared / default model identifier, or ``None``.
    :param model_override: The active per-session model override, or ``None``.
        When set, the declared window is ignored and the override model's
        catalog window is used (matching the server ring).
    :returns: The declared window when set and no override is active;
        otherwise the effective model's catalog window via
        :func:`get_model_context_window`; ``None`` when neither a usable
        window nor a model is available.
    """
    effective_model = model_override if model_override is not None else model
    if spec_context_window is not None and model_override is None:
        return spec_context_window
    if effective_model:
        return get_model_context_window(effective_model)
    return None


@dataclass(frozen=True)
class ModelPricing:
    """
    Per-token prices for a model, in USD per token (not per million).

    Anthropic-style providers report ``input_tokens`` as the *non-cached*
    portion of the prompt and bill cache reads / cache writes at separate
    rates, so cost is the sum of the four priced parts. When the catalog
    publishes no cache rates (e.g. OpenAI and ``databricks-*`` entries in
    the MLflow catalog), ``cache_read_per_token`` / ``cache_write_per_token``
    are ``None`` and :func:`compute_llm_cost` derives them from
    ``input_per_token`` via the standard ratios (see
    ``_FALLBACK_CACHE_READ_INPUT_RATIO`` / ``_FALLBACK_CACHE_WRITE_INPUT_RATIO``).

    :param input_per_token: Price per non-cached input token, e.g.
        ``2.5e-6``.
    :param output_per_token: Price per output token, e.g. ``1e-5``.
    :param cache_read_per_token: Price per cache-read (cache-hit) input
        token (typically ~0.1x input), or ``None`` when unpublished.
    :param cache_write_per_token: Price per cache-write (cache-creation)
        input token (typically ~1.25x input), or ``None`` when
        unpublished.
    """

    input_per_token: float
    output_per_token: float
    cache_read_per_token: float | None = None
    cache_write_per_token: float | None = None


def fetch_model_pricing(model: str) -> ModelPricing | None:
    """
    Look up per-token pricing for *model* from the MLflow catalog.

    Returns prices per token (not per million), including cache-read /
    cache-write rates when the catalog publishes them. Uses the same
    shared catalog lookup as :func:`get_model_context_window`, including the
    same unambiguous family-prefix fallback for newly released variants.

    :param model: Model identifier, e.g. ``"anthropic/claude-sonnet-4-6"``
        or ``"databricks-gpt-5-5"``.
    :returns: A :class:`ModelPricing`, or ``None`` when pricing is
        unavailable (network error, model not in catalog, or catalog
        entry lacks input/output pricing data).
    """

    def _extract(info: ModelInfo) -> ModelPricing | None:
        """Convert shared per-million catalog prices to per-token values."""
        if info.input_price is None or info.output_price is None:
            return None
        return ModelPricing(
            input_per_token=float(info.input_price) / 1_000_000,
            output_per_token=float(info.output_price) / 1_000_000,
            cache_read_per_token=(
                float(info.cache_read_price) / 1_000_000
                if info.cache_read_price is not None
                else None
            ),
            cache_write_per_token=(
                float(info.cache_write_price) / 1_000_000
                if info.cache_write_price is not None
                else None
            ),
        )

    prices = {
        price for info in find_catalog_models(model) if (price := _extract(info)) is not None
    }
    if len(prices) == 1:
        return next(iter(prices))
    return None


def compute_llm_cost(usage: dict[str, Any], pricing: ModelPricing) -> float:
    """
    Compute USD cost for one usage record under *pricing*, cache-aware.

    **Important:** ``input_tokens`` must be the *non-cached* portion of
    the input. ``cache_read_input_tokens`` and
    ``cache_creation_input_tokens`` are *additive* — the function
    prices each bucket at its own rate and sums them. This matches
    Anthropic's native semantics. OpenAI's ``prompt_tokens`` is the
    *total* input count (including cached tokens), so callers using
    OpenAI usage data must subtract ``cached_tokens`` from
    ``prompt_tokens`` before passing the result as ``input_tokens``
    here; failing to do so double-bills cached tokens at the full
    input rate.

    Prices cache-read and cache-write (cache-creation) input tokens at
    their own rates when the catalog publishes them; when it doesn't (e.g.
    ``databricks-*`` entries), it derives them from the input rate via the
    standard ratios — cache read ≈ 0.10× input, cache write ≈ 1.25× input
    (see ``_FALLBACK_CACHE_READ_INPUT_RATIO`` /
    ``_FALLBACK_CACHE_WRITE_INPUT_RATIO``). Providers that don't break out
    cache tokens omit those keys (counted as ``0``), so the result reduces
    to the plain ``input * price + output * price`` formula.

    :param usage: Usage dict; reads ``input_tokens``, ``output_tokens``,
        ``cache_read_input_tokens``, ``cache_creation_input_tokens``
        (missing keys count as 0). ``input_tokens`` must be the
        non-cached portion — see note above. Example:
        ``{"input_tokens": 1200, "output_tokens": 300,
        "cache_read_input_tokens": 5000}``.
    :param pricing: Per-token prices for the model.
    :returns: Cost in USD for the tokens in *usage*.
    """
    input_tokens = usage.get("input_tokens") or 0
    output_tokens = usage.get("output_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    # No published cache rate → derive one from the input rate using the
    # industry-standard ratios (see _FALLBACK_CACHE_*_INPUT_RATIO). This keeps
    # cache reads at ~10% of input on models whose catalog entry omits cache
    # pricing (``databricks-*`` today) instead of billing them at full input
    # rate, which over-charged cache-heavy sessions ~10×. Never drop the
    # tokens — cache reads/writes still cost something.
    cache_read_rate = (
        pricing.cache_read_per_token
        if pricing.cache_read_per_token is not None
        else pricing.input_per_token * _FALLBACK_CACHE_READ_INPUT_RATIO
    )
    cache_write_rate = (
        pricing.cache_write_per_token
        if pricing.cache_write_per_token is not None
        else pricing.input_per_token * _FALLBACK_CACHE_WRITE_INPUT_RATIO
    )
    return (
        input_tokens * pricing.input_per_token
        + output_tokens * pricing.output_per_token
        + cache_read * cache_read_rate
        + cache_write * cache_write_rate
    )


def fetch_model_pricing_with_provider(
    model: str,
    provider_config: dict[str, Any] | None = None,
    harness: str | None = None,
) -> ModelPricing | None:
    """
    Fetch model pricing, checking provider config first then catalog.

    Resolution order:
    1. Provider config custom pricing (for self-hosted/gateway models with configured rates)
    2. MLflow catalog via :func:`fetch_model_pricing` (for known vendor models)
    3. ``None`` (unpriced)

    This enables cost tracking for self-hosted models (Ollama, llama-server,
    vLLM, custom gateways) and gateway endpoints that expose
    catalog-known model IDs but
    charge their own configured rates. Custom pricing takes precedence when
    configured, so a self-hosted provider serving ``claude-opus-4`` can
    override the vendor catalog rate with its own.

    Example provider config::

        providers:
          ollama-local:
            kind: local
            openai:
              base_url: http://localhost:11434/v1
              api_key: ollama
              pricing:
                input_per_million: 0.0
                output_per_million: 0.0

    :param model: Model identifier, e.g. ``"llama3.2:latest"`` or
        ``"anthropic/claude-sonnet-4-6"``.
    :param provider_config: Parsed provider config from
        :func:`~omnigent.onboarding.provider_config.load_config`. When
        ``None``, skips provider lookup and falls through to catalog.
    :param harness: Harness name to determine which provider family to check,
        e.g. ``"claude-sdk"`` or ``"codex"``. When ``None``, skips provider
        lookup and falls through to catalog. SDK executors may pass
        executor-style spellings (``claude_sdk``, ``agents_sdk``) which are
        normalized internally.
    :returns: A :class:`ModelPricing` (per-token rates), or ``None`` when
        pricing is unavailable.

    .. note::
        LIMITATION: This function uses ``default_provider_for_harness`` to
        resolve the provider, which returns the DEFAULT provider for the given
        harness, not necessarily the provider actually used by the session.
        Sessions using named providers (via ``ProviderAuth``) that differ from
        the default may be priced incorrectly. A future improvement should
        thread the actual ``ProviderEntry`` from launch/session state instead
        of re-resolving the default.
    """
    # Step 1: Check provider config custom pricing first (configured rates take precedence)
    # Canonicalize harness to handle SDK executor spellings (claude_sdk -> claude-sdk)
    if provider_config is not None and harness is not None:
        from omnigent.harness_aliases import canonicalize_harness

        canonical_harness = canonicalize_harness(harness) or harness
        # Lazy import to avoid circular dependency. provider_config imports
        # this module at top level, so we import it only when needed here.
        from omnigent.onboarding.provider_config import (
            default_provider_for_harness,
        )

        try:
            provider = default_provider_for_harness(provider_config, canonical_harness)
            if provider is not None:
                # Determine which family (anthropic/openai) this harness uses
                from omnigent.onboarding.provider_config import provider_family_for_harness

                family_name = provider_family_for_harness(canonical_harness)
                if family_name is not None:
                    # Get the family config with custom pricing (if any)
                    family = provider.family(family_name)
                    if family is not None and family.pricing is not None:
                        # Convert per-million prices to per-token
                        return ModelPricing(
                            input_per_token=family.pricing.input_per_million / 1_000_000,
                            output_per_token=family.pricing.output_per_million / 1_000_000,
                            cache_read_per_token=(
                                family.pricing.cache_read_per_million / 1_000_000
                                if family.pricing.cache_read_per_million is not None
                                else None
                            ),
                            cache_write_per_token=(
                                family.pricing.cache_write_per_million / 1_000_000
                                if family.pricing.cache_write_per_million is not None
                                else None
                            ),
                        )
        except Exception:
            # If provider lookup fails (e.g., malformed config, import error),
            # fall through to catalog rather than breaking cost tracking entirely.
            pass

    # Step 2: Fall back to catalog pricing when provider pricing is not configured
    catalog_pricing = fetch_model_pricing(model)
    if catalog_pricing is not None:
        return catalog_pricing

    # Step 3: No pricing available
    return None
