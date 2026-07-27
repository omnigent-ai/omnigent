"""
Context window resolution for LLM models.

Provides :func:`get_model_context_window` which resolves a model's
context window size via multiple backends (env var override, litellm
registry, MLflow GitHub Release catalog) with a conservative 128K
fallback.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import cachetools

_MLFLOW_CATALOG_URL = (
    "https://github.com/mlflow/mlflow/releases/download/model-catalog%2Flatest/{provider}.json"
)

# Process-level cache of the per-provider MLflow catalog. The catalog is
# a remote GitHub release asset that changes at most a few times a day,
# but the response builder for ``GET /v1/sessions/{id}`` calls
# ``get_model_context_window`` on every snapshot — without this cache,
# every conversation load for a provider-prefixed model (claude-*, gpt-*,
# databricks-*, …) paid a ~490ms uncached ``urlopen`` to GitHub. A 1-hour
# TTL keeps it fresh enough while collapsing that to one fetch per
# provider per hour. ``maxsize`` comfortably exceeds the provider count.
# Guarded by a lock because the fetch runs under ``asyncio.to_thread``,
# so concurrent requests can race the same key.
_CATALOG_TTL_SECONDS = 3600
_catalog_cache: cachetools.TTLCache[str, dict[str, object] | None] = cachetools.TTLCache(
    maxsize=32, ttl=_CATALOG_TTL_SECONDS
)
_catalog_cache_lock = threading.Lock()
# Sentinel distinguishing "absent from cache" from a cached ``None``
# (a cached fetch failure). ``object()`` is unique so it can never
# collide with a real catalog value.
_CATALOG_MISS = object()

_MODEL_PREFIX_TO_PROVIDER: dict[str, str] = {
    "databricks-": "databricks",
    "gpt-": "openai",
    "o1-": "openai",
    "o3-": "openai",
    "o4-": "openai",
    "claude-": "anthropic",
    "gemini-": "google",
    "llama-": "meta",
    "mistral-": "mistral",
}

# Providers Omnigent routes directly for explicit ``vendor/model`` ids.
# :func:`fetch_model_pricing` gates its OpenRouter fallbacks on this set so a
# transient catalog-fetch failure for one of these providers (``models is
# None``) can't be mistaken for "unrecognized provider" and mis-price e.g.
# ``openai/gpt-4o`` at OpenRouter's public rate.
_KNOWN_DIRECT_PROVIDERS: frozenset[str] = frozenset(_MODEL_PREFIX_TO_PROVIDER.values())

_DEFAULT_CONTEXT_WINDOW: int = 128_000

# Omnigent's authoritative context-window registry, consulted BEFORE litellm
# and the MLflow catalog (see :func:`get_model_context_window`). The upstream
# backends mis-size or omit several ids we actually serve, and offline both
# collapse to the conservative 128K default — so for those models this registry
# is our single source of truth, not a last-resort fallback. Keyed by the
# *normalized* base id (provider prefix and OpenRouter ``:tag`` suffix stripped,
# lowercased; the ``[1m]`` beta marker is PRESERVED so the 1M variant stays
# distinct from its base — see :func:`_registry_context_window`). A spec's
# ``executor.context_window`` still overrides everything.
#
# Covered today:
#  - Qwen models (absent from litellm + the MLflow catalog) — published Alibaba
#    Cloud Model Studio / DashScope maxima.
#  - Anthropic 1M-context beta ids (the ``[1m]`` suffix) — handled by a rule in
#    :func:`_registry_context_window` rather than enumerated per base model.
_CONTEXT_WINDOW_REGISTRY: dict[str, int] = {
    "qwen3-coder-plus": 1_048_576,  # DashScope coding-plan default: 1M tokens
    "qwen3-coder-flash": 1_048_576,  # served flash variant: 1M tokens
    "qwen3-coder": 262_144,  # 480B open weights: 256K native (1M w/ YaRN)
    "qwen-plus": 131_072,
    "qwen-max": 131_072,
    "qwen-turbo": 1_008_192,
    "qwen-flash": 1_000_000,
}

# The Anthropic 1M-context beta encodes its window in the model id via a
# trailing ``[1m]`` marker (e.g. ``claude-opus-4-8[1m]``). Neither litellm nor
# the MLflow catalog keys on the bracketed form, so without this it resolves to
# the 128K default — under-sizing both the context meter and the compaction /
# overflow threshold by ~8x. The suffix *is* the window, so we read it directly
# rather than stripping it (the base id may legitimately have a smaller window).
_ANTHROPIC_1M_BETA_SUFFIX = "[1m]"
_ANTHROPIC_1M_BETA_WINDOW = 1_000_000

_logger = logging.getLogger(__name__)


def _registry_context_window(model: str) -> int | None:
    """Resolve a model's context window from Omnigent's authoritative registry.

    Consulted BEFORE litellm and the MLflow catalog. Normalizes the id the way
    model strings reach us — a provider prefix (``qwen/qwen3-coder``,
    ``openrouter/qwen/qwen3-coder``) and an OpenRouter-style ``:tag`` suffix
    (``qwen3-coder:free``) — down to the bare id, lowercased, with the ``[1m]``
    beta marker preserved.

    Resolution: an exact :data:`_CONTEXT_WINDOW_REGISTRY` entry, then the
    Anthropic 1M-context beta rule (a trailing ``[1m]`` on a Claude id →
    1,000,000). The rule is scoped to Claude ids so a custom/self-hosted model
    that merely happens to end in ``[1m]`` isn't force-sized to 1M.

    :param model: The model identifier (any namespacing).
    :returns: The context window in tokens, or ``None`` when the model isn't in
        our registry (caller falls back to litellm / the catalog / the default).
    """
    bare = model.rsplit("/", 1)[-1].split(":", 1)[0].strip().lower()
    if bare in _CONTEXT_WINDOW_REGISTRY:
        return _CONTEXT_WINDOW_REGISTRY[bare]
    # ``[1m]`` is an Anthropic-only beta convention, so gate on Claude ids
    # (covers ``claude-*`` and ``databricks-claude-*``); other providers fall
    # through to litellm/catalog rather than being forced to 1M.
    if "claude" in bare and bare.endswith(_ANTHROPIC_1M_BETA_SUFFIX):
        return _ANTHROPIC_1M_BETA_WINDOW
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

# Live OpenRouter pricing lookup cache.  The entire /api/v1/models list
# (~342 entries) is fetched once and cached under the singleton key
# _LIVE_PRICING_LIST_KEY as a dict[str, ModelPricing | None].  Per-model
# lookups are plain dict.get() against this cached map, so a single network
# call amortises across ALL models (the "one network call" property).
# Negative results (model not in list) are implicitly cached as missing keys.
# Transient failures (timeout/DNS/5xx) are cached separately at a short TTL
# so the model can recover quickly, while "not found" stays cached for the
# full list TTL.
_OPENROUTER_LIVE_TTL_SECONDS = 3600
_LIVE_PRICING_FAILURE_TTL_SECONDS = 60  # short TTL for transient failures
_live_pricing_cache: cachetools.TTLCache[str, dict[str, ModelPricing] | None] = (
    cachetools.TTLCache(maxsize=8, ttl=_OPENROUTER_LIVE_TTL_SECONDS)
)
_live_pricing_cache_lock = threading.Lock()
_LIVE_PRICING_LIST_KEY = "__openrouter_list__"
_LIVE_PRICING_MISS = object()  # sentinel distinguishing "absent" from cached None
# Per-key failure cache: maps lookup_key -> (reason, timestamp).
# Checked AFTER the main cache miss; entries expire after
# _LIVE_PRICING_FAILURE_TTL_SECONDS so transient failures recover quickly.
_live_pricing_failure_cache: dict[str, tuple[str, float]] = {}
_live_pricing_failure_lock = threading.Lock()


def _infer_provider(bare: str) -> str | None:
    """
    Infer the MLflow provider name from a bare model identifier.

    Checks ``_MODEL_PREFIX_TO_PROVIDER`` with longest-prefix-first
    matching.

    :param bare: Model name without provider prefix, e.g.
        ``"databricks-gpt-5-5"`` or ``"gpt-4o"``.
    :returns: Provider name (e.g. ``"databricks"``), or ``None``
        when the prefix is not recognised.
    """
    for prefix, provider in sorted(
        _MODEL_PREFIX_TO_PROVIDER.items(), key=lambda kv: len(kv[0]), reverse=True
    ):
        if bare.startswith(prefix):
            return provider
    return None


def _download_mlflow_provider_catalog(provider: str) -> dict[str, object] | None:
    """
    Download the MLflow GitHub Release catalog JSON for *provider*.

    Downloads ``_MLFLOW_CATALOG_URL.format(provider=provider)``,
    following the GitHub redirect to the release-assets CDN. Returns
    the parsed ``models`` dict (mapping model name to entry) on
    success, ``None`` on any network or parse error. This is the raw
    network call; callers should go through
    :func:`_fetch_mlflow_provider_catalog` for the cached path.

    :param provider: Provider name, e.g. ``"databricks"`` or
        ``"openai"``.
    :returns: Dict of model-name to catalog entry, or ``None`` on
        failure.
    """
    import json
    import urllib.request

    url = _MLFLOW_CATALOG_URL.format(provider=provider)
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data: dict[str, object] = json.loads(resp.read())
        models = data.get("models")
        return dict(models) if isinstance(models, dict) else None
    except Exception:
        return None


def _fetch_mlflow_provider_catalog(provider: str) -> dict[str, object] | None:
    """
    Return the MLflow catalog for *provider*, cached process-wide.

    Wraps :func:`_download_mlflow_provider_catalog` with a 1-hour TTL
    cache so the per-request GitHub fetch (~490ms) is paid at most once
    per provider per hour instead of on every ``GET /v1/sessions/{id}``
    snapshot. A ``None`` result (network error / missing asset) is also
    cached, so a transient outage doesn't make every subsequent request
    re-pay the timeout for an hour — acceptable since the caller falls
    back to the 128K default and the window is refreshed on TTL expiry.

    :param provider: Provider name, e.g. ``"databricks"`` or
        ``"openai"``.
    :returns: Dict of model-name to catalog entry, or ``None`` on
        failure.
    """
    with _catalog_cache_lock:
        cached = _catalog_cache.get(provider, _CATALOG_MISS)
        if cached is not _CATALOG_MISS:
            return cached
    # Network call outside the lock so a slow fetch for one provider
    # doesn't block lookups for another.
    result = _download_mlflow_provider_catalog(provider)
    with _catalog_cache_lock:
        _catalog_cache[provider] = result
    return result


def _fetch_context_window_from_mlflow(model: str) -> int | None:
    """
    Look up a model's context window via the MLflow GitHub Release
    catalog.

    Fetches the per-provider JSON file (one HTTP request per
    provider) and reads ``context_window.max_input``. Strategy:

    1. Infer the provider from the model name (explicit
       ``provider/`` prefix or ``_MODEL_PREFIX_TO_PROVIDER`` table).
    2. Fetch ``{provider}.json`` from the MLflow release asset CDN.
    3. Exact name match in the ``models`` dict.
    4. Family-prefix retry: strip the last hyphen component and
       search the same provider catalog. Accepted only when **all**
       prefix-matched entries share the same ``max_input``.

    Times out after 5 seconds; any network or parse error returns
    ``None``.

    :param model: Model identifier, e.g. ``"databricks-gpt-5-5"``
        or ``"openai/gpt-4o"``.
    :returns: ``max_input + max_output`` from the catalog entry
        in tokens, or ``None`` when the model cannot be resolved.
    """
    if os.environ.get("OMNIGENT_DISABLE_CATALOG_LOOKUP") == "1":
        return None

    if "/" in model:
        explicit_provider, bare = model.split("/", 1)
        provider = explicit_provider
    else:
        bare = model
        provider = _infer_provider(bare)

    if provider is None:
        return None

    models = _fetch_mlflow_provider_catalog(provider)
    if models is None:
        return None

    def _total(cw: object) -> int | None:
        """Sum max_input + max_output from a context_window dict."""
        if not isinstance(cw, dict):
            return None
        max_input = cw.get("max_input")
        if max_input is None:
            return None
        return int(max_input) + int(cw.get("max_output") or 0)

    entry = models.get(bare)
    if entry is not None and isinstance(entry, dict):
        val = _total(entry.get("context_window"))
        if val is not None:
            return val

    if "-" in bare:
        prefix = bare.rsplit("-", 1)[0]
        matched = {
            name: e
            for name, e in models.items()
            if name.startswith(prefix + "-") and isinstance(e, dict)
        }
        if matched:
            windows = {
                _total(e.get("context_window"))
                for e in matched.values()
                if _total(e.get("context_window")) is not None
            }
            if len(windows) == 1:
                return int(next(iter(windows)))  # type: ignore[arg-type]

    return None


def _fetch_live_openrouter_pricing(model: str) -> ModelPricing | None:
    """
    Fetch per-token pricing from the OpenRouter models LIST endpoint.

    Last-resort fallback for models absent from the MLflow openrouter
    catalog (e.g. ``z-ai/glm-5.2``).  The per-model endpoint
    (``/api/v1/models/{id}``) returns 404 for all models, so pricing is
    fetched from the list endpoint (``/api/v1/models``), which returns
    ~342 entries as a JSON array under ``data`` (NB: NOT keyed on id;
    not a dict).

    The entire list is fetched once per call (bounded by the 3 s timeout)
    and indexed into a dict keyed on each entry's ``id`` field.  The
    *entire* ``by_id`` map is then cached under a single singleton key
    (:data:`_LIVE_PRICING_LIST_KEY`) so a single network call amortises
    across ALL models -- the "one network call" property holds across
    different models.  Per-model lookups are plain ``dict.get()`` against
    this cached map.

    The ``model`` argument may carry an ``openrouter/`` prefix (bare
    ``vendor/model`` is the expected form, but the prefix is stripped for
    safety).

    Pricing fields in the response are per-token *strings* (e.g.
    ``"0.0000007938"``).  A value of ``"0"`` is an authoritative zero;
    absent or unparseable fields are treated as unknown (``None``).  The
    helper never raises into the caller.

    Transient failures (timeout/DNS/5xx) are cached for a short TTL
    (:data:`_LIVE_PRICING_FAILURE_TTL_SECONDS`) so the model can recover
    quickly.  "Not found" results are cached for the full list TTL
    (:data:`_OPENROUTER_LIVE_TTL_SECONDS`).

    :param model: The model id as reported by pi, e.g.
        ``"z-ai/glm-5.2"`` or ``"openrouter/z-ai/glm-5.2"``.
    :returns: :class:`ModelPricing` or ``None``.
    """
    import json as _json
    import urllib.error
    import urllib.request

    # Normalise: strip a leading "openrouter/" so the bare vendor/model
    # matches the list-endpoint's ``id`` field.
    lookup_key = model.removeprefix("openrouter/")

    # --- Check the main list cache first (singleton key) ---
    with _live_pricing_cache_lock:
        cached_list = _live_pricing_cache.get(_LIVE_PRICING_LIST_KEY, _LIVE_PRICING_MISS)
        if cached_list is not _LIVE_PRICING_MISS:
            if cached_list is None:
                return None
            return cached_list.get(lookup_key)

    # --- Check the short-TTL failure cache ---
    with _live_pricing_failure_lock:
        failure_entry = _live_pricing_failure_cache.get(lookup_key)
        if failure_entry is not None:
            reason, failure_ts = failure_entry
            elapsed = time.monotonic() - failure_ts
            if elapsed < _LIVE_PRICING_FAILURE_TTL_SECONDS:
                _logger.debug(
                    "openrouter pricing failure (cached %.0fs ago): %s: %s",
                    elapsed,
                    lookup_key,
                    reason,
                )
                return None

    # --- Fetch the full list (one network call) ---
    url = "https://openrouter.ai/api/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            raw = _json.loads(resp.read())
    except Exception as exc:
        _logger.debug("openrouter pricing fetch failed for %s: %s", lookup_key, exc)
        with _live_pricing_failure_lock:
            _live_pricing_failure_cache[lookup_key] = (str(exc), time.monotonic())
        return None

    # Parse the list into a by_id map.  On parse failure, cache a
    # short-TTL failure so the next call retries quickly.
    try:
        data = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(data, list):
            _logger.debug(
                "openrouter pricing: malformed response (data is %s)",
                type(data).__name__,
            )
            with _live_pricing_failure_lock:
                _live_pricing_failure_cache[lookup_key] = (
                    f"malformed response: data is {type(data).__name__}",
                    time.monotonic(),
                )
            return None

        def _to_float(val: object) -> float | None:
            """Parse a pricing string to float; ``None`` on failure."""
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        by_id: dict[str, ModelPricing] = {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("id")
            if not isinstance(eid, str):
                continue
            pricing_raw = entry.get("pricing")
            if not isinstance(pricing_raw, dict):
                continue
            input_pt = _to_float(pricing_raw.get("prompt"))
            output_pt = _to_float(pricing_raw.get("completion"))
            if input_pt is None or output_pt is None:
                continue
            by_id[eid] = ModelPricing(
                input_per_token=input_pt,
                output_per_token=output_pt,
                cache_read_per_token=_to_float(pricing_raw.get("input_cache_read")),
                cache_write_per_token=None,  # OpenRouter doesn't publish cache-write
            )

        # Cache the entire list under the singleton key.  If the target
        # model is absent, by_id.get(lookup_key) returns None (implicit
        # negative cache for the full list TTL).
        with _live_pricing_cache_lock:
            _live_pricing_cache[_LIVE_PRICING_LIST_KEY] = by_id
        return by_id.get(lookup_key)
    except Exception as exc:
        _logger.debug("openrouter pricing parse failed for %s: %s", lookup_key, exc)
        with _live_pricing_failure_lock:
            _live_pricing_failure_cache[lookup_key] = (str(exc), time.monotonic())
        return None


def get_model_context_window(model: str) -> int:
    """
    Look up the model's context window size in tokens.

    Resolution order:

    1. ``AP_CONTEXT_WINDOW_OVERRIDE`` env var — overrides everything.
       Supports custom/self-hosted models and e2e compaction tests.
    2. :func:`_registry_context_window` — Omnigent's own authoritative
       registry (Qwen models, Anthropic ``[1m]`` beta). Supersedes the
       upstream backends, which mis-size or omit these ids and collapse to
       the 128K default offline.
    3. ``litellm.get_model_info()`` — fast, local, no network. Also
       tried with the ``databricks/`` prefix for Databricks models.
    4. MLflow GitHub Release catalog — per-provider JSON fetched from
       ``github.com/mlflow/mlflow/releases``. Covers models not yet
       in litellm's bundled registry, with a family-prefix fallback
       for newly released variants.
    5. ``_DEFAULT_CONTEXT_WINDOW`` (128 K) — conservative fallback.

    :param model: The model identifier, e.g. ``"openai/gpt-4o"`` or
        ``"databricks-gpt-5-5"``.
    :returns: Context window size in tokens.
    """
    override = os.environ.get("AP_CONTEXT_WINDOW_OVERRIDE")
    if override is not None:
        return int(override)
    # Our registry supersedes the upstream backends: litellm and the MLflow
    # catalog mis-size or omit ids we serve (the Anthropic ``[1m]`` beta resolves
    # to 128K; Qwen models are absent), and offline both collapse to the 128K
    # default. Consult ours first.
    registered = _registry_context_window(model)
    if registered is not None:
        return registered
    try:
        import litellm
    except ImportError:
        return _fetch_context_window_from_mlflow(model) or _DEFAULT_CONTEXT_WINDOW
    try:
        info = litellm.get_model_info(model)
        if info:
            limit = info.get("max_input_tokens")
            if limit:
                return int(limit)
    except Exception:
        pass
    if model.startswith("databricks-"):
        try:
            info = litellm.get_model_info(f"databricks/{model}")
            if info:
                limit = info.get("max_input_tokens")
                if limit:
                    return int(limit)
        except Exception:
            pass
    return _fetch_context_window_from_mlflow(model) or _DEFAULT_CONTEXT_WINDOW


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
    provider-inference and catalog-fetch logic as
    :func:`_fetch_context_window_from_mlflow`, with the same
    family-prefix fallback for newly released model variants.

    :param model: Model identifier, e.g. ``"anthropic/claude-sonnet-4-6"``
        or ``"databricks-gpt-5-5"``.
    :returns: A :class:`ModelPricing`, or ``None`` when pricing is
        unavailable (network error, model not in catalog, or catalog
        entry lacks input/output pricing data).
    """
    if os.environ.get("OMNIGENT_DISABLE_CATALOG_LOOKUP") == "1":
        return None

    # Strip explicit openrouter/ prefix early so the lookup key is the
    # bare vendor/model form used by both the OpenRouter MLflow catalog
    # and the live API.  This MUST happen before the catalog retry and
    # family-prefix block so an ``openrouter/vendor/model`` id hits the
    # catalog retry (and doesn't fall through to the network path).
    or_model = model.removeprefix("openrouter/")

    if "/" in model:
        _explicit_provider, bare = model.split("/", 1)
        provider = _explicit_provider
    else:
        bare = model
        provider = _infer_provider(bare)

    if provider is None:
        return None

    models = _fetch_mlflow_provider_catalog(provider)

    def _extract(entry: object) -> ModelPricing | None:
        """Extract per-token pricing (incl. cache rates) from a catalog entry."""
        if not isinstance(entry, dict):
            return None
        pricing = entry.get("pricing")
        if not isinstance(pricing, dict):
            return None
        input_ppm = pricing.get("input_per_million_tokens")
        output_ppm = pricing.get("output_per_million_tokens")
        if input_ppm is None or output_ppm is None:
            return None
        cache_read_ppm = pricing.get("cache_read_per_million_tokens")
        cache_write_ppm = pricing.get("cache_write_per_million_tokens")
        return ModelPricing(
            input_per_token=float(input_ppm) / 1_000_000,
            output_per_token=float(output_ppm) / 1_000_000,
            cache_read_per_token=(
                float(cache_read_ppm) / 1_000_000 if cache_read_ppm is not None else None
            ),
            cache_write_per_token=(
                float(cache_write_ppm) / 1_000_000 if cache_write_ppm is not None else None
            ),
        )

    entry = models.get(bare) if models is not None else None
    if entry is not None:
        result = _extract(entry)
        if result is not None:
            return result

    # Family-prefix fallback: strip last hyphen segment and look for
    # entries that share the same pricing.
    # (#7) Use startswith(prefix + "-") to avoid over-matching
    # ("kimi" matching "kimi-k30").
    if "-" in bare and models is not None:
        prefix = bare.rsplit("-", 1)[0]
        matched = [e for name, e in models.items() if name.startswith(prefix + "-")]
        # (#1) Compute _extract once per element, dedupe on a tuple key
        # to avoid requiring ModelPricing to be hashable.
        prices: dict[tuple, ModelPricing] = {}
        for e in matched:
            p = _extract(e)
            if p is not None:
                k = (
                    p.input_per_token,
                    p.output_per_token,
                    p.cache_read_per_token,
                    p.cache_write_per_token,
                )
                prices[k] = p
        if len(prices) == 1:
            return next(iter(prices.values()))

    # Databricks-gateway alias fallback. A model served through the
    # Databricks gateway is reported as ``databricks-<base>`` (e.g.
    # ``databricks-claude-opus-4-8``), but the Databricks provider catalog
    # may not list every such alias even when the *underlying* provider
    # catalog prices the base model (anthropic's ``claude-opus-4-8`` is
    # priced; the databricks alias is not). Retry once with the de-prefixed
    # base so the underlying provider's pricing applies. Only the known
    # ``databricks-`` prefix is stripped, and the base never re-infers
    # ``databricks`` (it has no such prefix), so this can't recurse.
    if provider == "databricks" and bare.startswith("databricks-"):
        base = bare[len("databricks-") :]
        if base and base != bare:
            return fetch_model_pricing(base)

    # --- Precedence for vendor/model ids (e.g. "xiaomi/mimo-v2.5-pro") ---
    #
    # 1.  Own-provider catalog  (step 1 above: "xiaomi" -> xiaomi.json)
    # 2.  OpenRouter MLflow catalog  (step below: full key lookup + family-prefix)
    # 3.  Live OpenRouter API list  (step below: last-resort, cached)
    #
    # This means a model priced by its own provider catalog is never
    # overridden by the OpenRouter catalog, even when both carry the key.
    # Unresolved vendor/model ids fall through to OpenRouter rates by
    # design, because the bare form is what pi reports for all OpenRouter
    # dispatches.
    #
    # (#3) GATE: OpenRouter fallbacks apply only for an explicit
    # ``openrouter/`` prefix (or_model != model) or a provider outside
    # _KNOWN_DIRECT_PROVIDERS. Gating on the provider name rather than on
    # ``models is None`` matters because that signal is ambiguous: it's
    # ``None`` both for an unrecognized provider (safe to fall through) and
    # for a known provider's catalog fetch failing transiently (must NOT
    # fall through, or a one-off timeout mis-prices e.g. ``openai/gpt-4o``
    # at OpenRouter's rate for up to an hour).

    # OpenRouter vendor/model retry.  For bare ``vendor/model`` ids from
    # unrecognized providers (e.g. ``xiaomi/mimo-v2.5-pro``,
    # ``moonshotai/kimi-k3``) or explicitly ``openrouter/``-prefixed ids,
    # retry against the ``openrouter`` MLflow catalog.  The MLflow
    # openrouter.json stores these ids verbatim.
    # (#2) Uses the prefix-stripped ``or_model`` (not the raw ``model``)
    # so ``openrouter/vendor/model`` matches the catalog key
    # ``vendor/model``.
    #
    # The condition: explicit ``openrouter/`` prefix OR provider is not one
    # Omnigent routes directly.
    if or_model != model or provider not in _KNOWN_DIRECT_PROVIDERS:
        or_models = _fetch_mlflow_provider_catalog("openrouter")
        if or_models is not None:
            entry = or_models.get(or_model)
            if entry is not None:
                result = _extract(entry)
                if result is not None:
                    return result
            # Family-prefix fallback within the openrouter catalog too.
            # (#7) Use startswith(prefix + "-") to avoid over-matching.
            if "-" in or_model:
                prefix = or_model.rsplit("-", 1)[0]
                matched = [e for n, e in or_models.items() if n.startswith(prefix + "-")]
                # (#1) Dedupe on a tuple key; compute _extract once.
                prices = {}
                for e in matched:
                    p = _extract(e)
                    if p is not None:
                        k = (
                            p.input_per_token,
                            p.output_per_token,
                            p.cache_read_per_token,
                            p.cache_write_per_token,
                        )
                        prices[k] = p
                if len(prices) == 1:
                    return next(iter(prices.values()))

        # Live OpenRouter API fallback for models absent from MLflow entirely
        # (e.g. ``z-ai/glm-5.2``).  Fetches the models LIST endpoint once,
        # caches results for 1 h, never raises into the turn-completion path.
        return _fetch_live_openrouter_pricing(or_model)

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
