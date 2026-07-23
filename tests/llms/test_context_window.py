"""
Tests for model pricing and cache-aware LLM cost computation.

Covers :class:`ModelPricing`, :func:`compute_llm_cost` (the cache-aware
cost formula), and :func:`fetch_model_pricing`'s parsing of cache-read /
cache-write rates from a catalog entry.
"""

from __future__ import annotations

import json as _json_test
import urllib.request
from typing import Any

import pytest

from omnigent.llms import context_window
from omnigent.llms.context_window import (
    ModelPricing,
    _registry_context_window,
    compute_llm_cost,
    fetch_model_pricing,
    get_model_context_window,
    resolve_effective_context_window,
)


def test_resolve_effective_context_window_prefers_declared_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A spec-declared ``executor.context_window`` wins over the catalog lookup.

    Regression for the runner over-compaction bug: an agent that declares a
    1M window (e.g. Polly) must be budgeted against 1M, not the 128K catalog
    default. If the resolver fell back to the catalog here, the compaction
    budget would be ~8x too small and fire constantly.
    """

    def _boom(_model: str) -> int:
        raise AssertionError("catalog lookup must not run when a window is declared")

    monkeypatch.setattr(context_window, "get_model_context_window", _boom)
    assert resolve_effective_context_window(1_000_000, "claude-opus-4-8") == 1_000_000
    # Declared window applies even when the spec pins no model.
    assert resolve_effective_context_window(1_000_000, None) == 1_000_000


def test_resolve_effective_context_window_falls_back_to_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no declared window, resolve via the model catalog lookup."""
    monkeypatch.setattr(context_window, "get_model_context_window", lambda model: 200_000)
    assert resolve_effective_context_window(None, "claude-opus-4-8") == 200_000


def test_resolve_effective_context_window_none_when_no_window_and_no_model() -> None:
    """No declared window and no model → ``None`` (caller skips budgeting)."""
    assert resolve_effective_context_window(None, None) is None


def test_resolve_effective_context_window_override_bypasses_declared_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An active model override sizes against the override model's catalog window,
    NOT the spec-declared window.

    Matches the server ring: ``executor.context_window`` describes only the
    spec model, so overriding a 1M-window agent down to a 200K model must
    budget against 200K — otherwise the runner under-compacts past the real
    model's limit.
    """
    seen: list[str] = []

    def _catalog(model: str) -> int:
        seen.append(model)
        return 200_000

    monkeypatch.setattr(context_window, "get_model_context_window", _catalog)
    result = resolve_effective_context_window(
        1_000_000, "claude-opus-4-8", model_override="small-200k-model"
    )
    assert result == 200_000
    # The override model — not the spec model — drives the catalog lookup.
    assert seen == ["small-200k-model"]


def test_resolve_effective_context_window_declared_window_wins_without_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``model_override=None`` keeps the declared-window fast path."""

    def _boom(_model: str) -> int:
        raise AssertionError("catalog lookup must not run when no override is active")

    monkeypatch.setattr(context_window, "get_model_context_window", _boom)
    assert (
        resolve_effective_context_window(1_000_000, "claude-opus-4-8", model_override=None)
        == 1_000_000
    )


def test_compute_llm_cost_prices_cache_tokens_at_their_own_rates() -> None:
    """
    Cache reads/writes are billed at their own rates, not the input rate.

    Anthropic reports ``input_tokens`` as the non-cached portion and
    breaks out ``cache_read_input_tokens`` (cheap) / cache creation
    (pricey). A correct cost sums all four priced parts. If the formula
    reverted to ``input*price + output*price`` it would drop the 8000
    cache-read + 2000 cache-write tokens entirely (0.0136 -> 0.007).
    """
    pricing = ModelPricing(
        input_per_token=2e-6,
        output_per_token=1e-5,
        cache_read_per_token=2e-7,  # 0.1x input
        cache_write_per_token=2.5e-6,  # 1.25x input
    )
    usage: dict[str, Any] = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 2000,
    }
    # 1000*2e-6 + 500*1e-5 + 8000*2e-7 + 2000*2.5e-6
    # = 0.002 + 0.005 + 0.0016 + 0.005 = 0.0136
    assert compute_llm_cost(usage, pricing) == pytest.approx(0.0136)


def test_compute_llm_cost_derives_cache_rates_from_input_when_unpublished() -> None:
    """
    With no published cache rates, derive them from the input rate via the
    standard ratios: cache read at 0.10x input, cache write at 1.25x input.

    ``databricks-*`` catalog entries omit cache pricing, so this fallback is
    what every relay/native session on the gateway is billed by. Pricing cache
    reads at the full input rate (the old fallback) over-charged cache-heavy
    sessions ~10x — the bug this fixes.
    """
    pricing = ModelPricing(
        input_per_token=2e-6,
        output_per_token=1e-5,
        cache_read_per_token=None,
        cache_write_per_token=None,
    )
    usage: dict[str, Any] = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 2000,
    }
    # cache read at 0.10x input (2e-7), cache write at 1.25x input (2.5e-6):
    # 1000*2e-6 + 500*1e-5 + 8000*2e-7 + 2000*2.5e-6
    # = 0.002 + 0.005 + 0.0016 + 0.005 = 0.0136
    # The old full-input fallback would give 0.027 (cache read at 1.6e-2),
    # so a value of 0.027 here means the ratio fallback regressed.
    assert compute_llm_cost(usage, pricing) == pytest.approx(0.0136)


def test_compute_llm_cost_without_cache_tokens_is_the_flat_formula() -> None:
    """
    No cache-token keys -> reduces to ``input*price + output*price``.

    Regression guard for the common / OpenAI case (no cache breakdown):
    the cache-aware formula must not change the number when there are no
    cache tokens.
    """
    pricing = ModelPricing(
        input_per_token=2e-6,
        output_per_token=1e-5,
        cache_read_per_token=2e-7,
        cache_write_per_token=2.5e-6,
    )
    usage: dict[str, Any] = {"input_tokens": 1000, "output_tokens": 500}
    # 1000*2e-6 + 500*1e-5 = 0.002 + 0.005 = 0.007 (cache terms are 0)
    assert compute_llm_cost(usage, pricing) == pytest.approx(0.007)


def test_fetch_model_pricing_parses_cache_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``fetch_model_pricing`` surfaces catalog cache-read/write rates.

    The MLflow catalog publishes ``cache_read_per_million_tokens`` /
    ``cache_write_per_million_tokens`` for Anthropic models; this pins
    that they reach :class:`ModelPricing` (per-token), so cost can be
    cache-accurate. A failure means the cache rates were dropped and
    cost would fall back to the derived input-ratio default.
    """
    # Catalog lookup is disabled globally in tests (conftest); re-enable
    # for this one and stub the network fetch with a cache-priced entry.
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        lambda provider: {
            "claude-x": {
                "pricing": {
                    "input_per_million_tokens": 2.5,
                    "output_per_million_tokens": 10.0,
                    "cache_read_per_million_tokens": 0.25,
                    "cache_write_per_million_tokens": 3.125,
                }
            }
        },
    )
    pricing = fetch_model_pricing("anthropic/claude-x")
    assert pricing is not None
    assert pricing.input_per_token == pytest.approx(2.5e-6)
    assert pricing.output_per_token == pytest.approx(1e-5)
    assert pricing.cache_read_per_token == pytest.approx(0.25e-6)
    assert pricing.cache_write_per_token == pytest.approx(3.125e-6)


def test_fetch_model_pricing_omits_cache_rates_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A catalog entry with no cache fields yields ``None`` cache rates.

    OpenAI entries in the catalog carry only input/output rates;
    ``compute_llm_cost`` then derives cache rates from the input rate via
    the standard ratios. If these came back as ``0.0`` instead of ``None``,
    cache tokens would be billed free.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        lambda provider: {
            "gpt-x": {
                "pricing": {
                    "input_per_million_tokens": 1.25,
                    "output_per_million_tokens": 10.0,
                }
            }
        },
    )
    pricing = fetch_model_pricing("openai/gpt-x")
    assert pricing is not None
    assert pricing.input_per_token == pytest.approx(1.25e-6)
    assert pricing.cache_read_per_token is None
    assert pricing.cache_write_per_token is None


def test_fetch_model_pricing_databricks_alias_falls_back_to_base_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``databricks-<base>`` alias absent from the Databricks catalog is
    priced from the base model's underlying-provider catalog.

    Models served through the Databricks gateway are reported as
    ``databricks-claude-opus-4-8``, which the Databricks catalog may not
    list even though anthropic's ``claude-opus-4-8`` is priced. Without the
    de-prefix fallback, every unpinned claude-sdk agent on the Databricks
    gateway (which defaults to ``databricks-claude-opus-4-8``) would show
    "unpriced" — the exact gap reported for the debbie/debby supervisors.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)

    def _catalog(provider: str) -> dict[str, Any] | None:
        """Databricks catalog lacks opus; the base (anthropic) catalog prices it."""
        if provider == "databricks":
            # Has some databricks models, but NOT the opus alias under test.
            return {
                "databricks-claude-sonnet-4-6": {
                    "pricing": {
                        "input_per_million_tokens": 3.0,
                        "output_per_million_tokens": 15.0,
                    }
                }
            }
        # The underlying provider (anthropic) prices the de-prefixed base.
        return {
            "claude-opus-4-8": {
                "pricing": {
                    "input_per_million_tokens": 15.0,
                    "output_per_million_tokens": 75.0,
                }
            }
        }

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)

    pricing = fetch_model_pricing("databricks-claude-opus-4-8")
    assert pricing is not None, (
        "databricks-claude-opus-4-8 was not priced — the databricks→base "
        "fallback did not reach anthropic's claude-opus-4-8."
    )
    # Priced from the base model's rates (15 / 75 per million), not the
    # databricks sonnet entry (3 / 15).
    assert pricing.input_per_token == pytest.approx(15e-6)
    assert pricing.output_per_token == pytest.approx(75e-6)


def test_provider_catalog_is_cached_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The per-provider catalog is downloaded once, then served from cache.

    This pins the perf fix: the response builder calls
    ``get_model_context_window`` on every ``GET /v1/sessions/{id}``
    snapshot, and each call used to re-issue a ~490ms GitHub fetch.
    With the TTL cache, repeated lookups for the same provider must hit
    the network exactly once. A regression (cache removed) would show as
    a download count > 1. Asserting the resolved window also proves the
    cached payload still flows through the resolver unchanged.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    # Clear any residue from earlier tests so the count starts clean.
    context_window._catalog_cache.clear()
    calls: list[str] = []

    def _fake_download(provider: str) -> dict[str, Any]:
        """Record each network hit and return a one-model catalog."""
        calls.append(provider)
        return {"claude-z": {"context_window": {"max_input": 200_000, "max_output": 8_192}}}

    monkeypatch.setattr(context_window, "_download_mlflow_provider_catalog", _fake_download)

    # litellm resolves many real names; force the catalog path by using a
    # name it won't know, so the fetch is exercised deterministically.
    first = context_window.get_model_context_window("claude-z")
    second = context_window.get_model_context_window("claude-z")
    assert first == 208_192  # max_input + max_output from the stub
    assert second == 208_192
    # Exactly one network download despite two resolver calls.
    assert calls == ["anthropic"]


def test_provider_catalog_caches_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed download (``None``) is cached too, not retried every call.

    A transient GitHub outage returns ``None``; without caching that
    result, every subsequent snapshot would re-pay the 5s timeout for an
    hour. Pinning that ``None`` is cached keeps a single failure from
    amplifying into per-request latency. The caller still falls back to
    the 128K default, which this also checks.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._catalog_cache.clear()
    calls: list[str] = []

    def _fail(provider: str) -> None:
        """Record the hit and simulate a network/parse failure (returns None)."""
        calls.append(provider)

    monkeypatch.setattr(context_window, "_download_mlflow_provider_catalog", _fail)
    first = context_window.get_model_context_window("claude-z")
    second = context_window.get_model_context_window("claude-z")
    assert first == 128_000  # _DEFAULT_CONTEXT_WINDOW fallback
    assert second == 128_000
    assert calls == ["anthropic"]


# ---------------------------------------------------------------------------
# Omnigent's authoritative context-window registry (supersedes litellm/catalog)
# ---------------------------------------------------------------------------


def test_registry_context_window_normalizes_id() -> None:
    """The registry strips provider prefixes and ``:tag`` suffixes before matching."""
    assert _registry_context_window("qwen3-coder-plus") == 1_048_576
    assert _registry_context_window("qwen/qwen3-coder") == 262_144
    assert _registry_context_window("qwen3-coder:free") == 262_144
    assert _registry_context_window("openrouter/qwen/qwen3-coder:free") == 262_144
    assert _registry_context_window("QWEN3-CODER-PLUS") == 1_048_576  # case-insensitive
    # A model the registry doesn't own → None (caller falls back to litellm).
    assert _registry_context_window("qwen-nonexistent-xyz") is None
    assert _registry_context_window("gpt-5.4") is None


def test_registry_resolves_anthropic_1m_beta_suffix() -> None:
    """The Anthropic ``[1m]`` beta marker resolves to a 1M window via the registry.

    The suffix *is* the window — we read it, not strip it — so any
    ``<model>[1m]`` resolves to 1,000,000 while the bare base defers to the
    upstream backends (which may size it differently).
    """
    assert _registry_context_window("claude-opus-4-8[1m]") == 1_000_000
    assert _registry_context_window("anthropic/claude-opus-4-8[1m]") == 1_000_000
    assert _registry_context_window("claude-sonnet-4-6[1m]") == 1_000_000
    assert _registry_context_window("CLAUDE-OPUS-4-8[1M]") == 1_000_000  # case-insensitive
    # Databricks-hosted Claude (contains "claude") also resolves.
    assert _registry_context_window("databricks-claude-opus-4-8[1m]") == 1_000_000
    # Without the suffix the registry defers (None → caller uses litellm/catalog).
    assert _registry_context_window("claude-opus-4-8") is None
    # The rule is Claude-scoped: a non-Claude id ending in [1m] is NOT forced to
    # 1M (it defers to litellm/catalog), so custom/self-hosted ids are safe.
    assert _registry_context_window("my-local-model[1m]") is None
    assert _registry_context_window("gpt-5.4[1m]") is None


def test_get_model_context_window_uses_registry_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registry-curated ids resolve to their window with NO network.

    Catalog lookup is disabled to prove hermeticity: the registry is consulted
    before litellm and the catalog, so qwen models and the Anthropic ``[1m]``
    beta resolve correctly even offline (the meter / overflow-threshold
    bug was that these collapsed to the 128K default).
    """
    monkeypatch.setenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", "1")
    monkeypatch.delenv("AP_CONTEXT_WINDOW_OVERRIDE", raising=False)
    # Anthropic 1M beta: resolves via the registry, not the 128K default.
    assert get_model_context_window("claude-opus-4-8[1m]") == 1_000_000
    assert get_model_context_window("anthropic/claude-opus-4-8[1m]") == 1_000_000
    # Qwen: curated window, not the default.
    assert get_model_context_window("qwen3-coder-plus") == 1_048_576
    # A model the registry doesn't own still falls back to the conservative default.
    assert get_model_context_window("qwen-nonexistent-xyz") == 128_000


# ---------------------------------------------------------------------------
# OpenRouter vendor/model pricing retry
# ---------------------------------------------------------------------------


def test_fetch_model_pricing_bare_openrouter_id_via_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A bare ``vendor/model`` id (no ``openrouter/`` prefix) from an
    unrecognized provider is priced from the openrouter MLflow catalog
    on retry.  ``xiaomi`` has no own-provider catalog, so the fallback
    fires correctly.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "openrouter":
            return {
                "xiaomi/mimo-v2.5-pro": {
                    "pricing": {
                        "input_per_million_tokens": 1.0,
                        "output_per_million_tokens": 3.0,
                        "cache_read_per_million_tokens": 0.2,
                        "cache_write_per_million_tokens": 0.0,
                    }
                }
            }
        return None  # "xiaomi" provider doesn't exist

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)

    pricing = fetch_model_pricing("xiaomi/mimo-v2.5-pro")
    assert pricing is not None, "xiaomi/mimo-v2.5-pro was not priced via openrouter retry"
    assert pricing.input_per_token == pytest.approx(1.0e-6)
    assert pricing.output_per_token == pytest.approx(3.0e-6)
    assert pricing.cache_read_per_token == pytest.approx(0.2e-6)


def test_fetch_model_pricing_kimi_k3_via_openrouter_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``moonshotai/kimi-k3`` prices via the openrouter catalog retry.

    ``moonshotai`` has no own-provider catalog, so the fallback fires.
    The openrouter catalog contains ``moonshotai/kimi-k2.5`` so the
    family-prefix fallback should match ``kimi-k3``.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "openrouter":
            return {
                "moonshotai/kimi-k2.5": {
                    "pricing": {
                        "input_per_million_tokens": 0.6,
                        "output_per_million_tokens": 3.0,
                        "cache_read_per_million_tokens": 0.1,
                    }
                }
            }
        return None

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)

    pricing = fetch_model_pricing("moonshotai/kimi-k3")
    assert pricing is not None, "moonshotai/kimi-k3 was not priced via openrouter family-prefix"
    assert pricing.input_per_token == pytest.approx(0.6e-6)
    assert pricing.output_per_token == pytest.approx(3.0e-6)


def test_fetch_model_pricing_already_prefixed_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An ``openrouter/``-prefixed id still prices normally (no regression).

    ``openrouter/xiaomi/mimo-v2.5-pro`` splits as provider=``openrouter``,
    bare=``xiaomi/mimo-v2.5-pro``; the openrouter catalog lookup should
    find it directly.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "openrouter":
            return {
                "xiaomi/mimo-v2.5-pro": {
                    "pricing": {
                        "input_per_million_tokens": 1.0,
                        "output_per_million_tokens": 3.0,
                    }
                }
            }
        return None

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)

    pricing = fetch_model_pricing("openrouter/xiaomi/mimo-v2.5-pro")
    assert pricing is not None
    assert pricing.input_per_token == pytest.approx(1.0e-6)
    assert pricing.output_per_token == pytest.approx(3.0e-6)


class _ListResp:
    """Fake urllib response for the OpenRouter list-endpoint."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return _json_test.dumps(self._payload).encode()

    def __enter__(self) -> _ListResp:
        return self

    def __exit__(self, *args: object) -> None:
        pass


def _or_list_response(*entries: dict) -> dict:
    """Build a mock OpenRouter /api/v1/models response."""
    return {"data": list(entries)}


# --- A working fake catalog that returns entries for known providers ---
def _make_catalog(or_catalog: dict[str, Any] | None = None, other: dict[str, Any] | None = None):
    """Return a mock _fetch_mlflow_provider_catalog callable.

    ``or_catalog`` is the ``openrouter`` provider catalog;
    ``other`` is any other provider (e.g. "anthropic").
    """

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "openrouter":
            return or_catalog
        if provider == "anthropic":
            return other
        return None

    return _catalog


def test_fetch_model_pricing_unknown_bare_id_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A bare single-segment id with no catalog match returns ``None``.

    Exercises the genuine fall-through path (own-provider: None, OR catalog:
    None, live API: model not in list) without short-circuiting via
    ``OMNIGENT_DISABLE_CATALOG_LOOKUP``.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        _make_catalog(or_catalog=None),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout=3: _ListResp(
            _or_list_response({"id": "other/model", "pricing": {"prompt": "1", "completion": "2"}})
        ),
    )

    assert fetch_model_pricing("nonexistent-model") is None


def test_fetch_model_pricing_single_segment_unknown_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-segment id with no catalog entry returns ``None`` cleanly."""
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", lambda _: None)
    monkeypatch.setenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", "1")

    assert fetch_model_pricing("unknown-model") is None


def test_fetch_model_pricing_live_fallback_prices_glm52(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Models absent from MLflow (e.g. ``z-ai/glm-5.2``) are priced via the
    live OpenRouter list-endpoint fallback.

    The list endpoint returns ``data`` as a LIST, not a dict.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        _make_catalog(or_catalog=None),
    )

    list_payload = _or_list_response(
        {"id": "some/other-model", "pricing": {"prompt": "1", "completion": "2"}},
        {
            "id": "z-ai/glm-5.2",
            "pricing": {
                "prompt": "0.0000007938",
                "completion": "0.0000024948",
                "input_cache_read": "0.00000014742",
            },
        },
    )

    def _fake_urlopen(url: str, timeout: int = 3) -> _ListResp:
        assert "openrouter.ai/api/v1/models" in url
        return _ListResp(list_payload)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    pricing = fetch_model_pricing("z-ai/glm-5.2")
    assert pricing is not None, "z-ai/glm-5.2 was not priced via live list fallback"
    assert pricing.input_per_token == pytest.approx(7.938e-7)
    assert pricing.output_per_token == pytest.approx(2.4948e-6)
    assert pricing.cache_read_per_token == pytest.approx(1.4742e-7)


def test_fetch_model_pricing_live_fallback_strips_openrouter_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An ``openrouter/``-prefixed id is stripped before matching the live list.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        _make_catalog(or_catalog=None),
    )

    list_payload = _or_list_response(
        {"id": "vendor/model-x", "pricing": {"prompt": "2e-6", "completion": "8e-6"}},
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout=3: _ListResp(list_payload),
    )

    pricing = fetch_model_pricing("openrouter/vendor/model-x")
    assert pricing is not None
    assert pricing.input_per_token == pytest.approx(2e-6)
    assert pricing.output_per_token == pytest.approx(8e-6)


def test_fetch_model_pricing_live_fallback_404_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live fallback returns ``None`` on HTTP 404 without raising."""
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        _make_catalog(or_catalog=None),
    )

    def _fake_urlopen_404(url: str, timeout: int = 3) -> None:
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_404)

    assert fetch_model_pricing("z-ai/glm-99") is None


def test_fetch_model_pricing_live_fallback_timeout_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live fallback returns ``None`` on timeout without raising."""
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        _make_catalog(or_catalog=None),
    )

    def _fake_urlopen_timeout(url: str, timeout: int = 3) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_timeout)

    assert fetch_model_pricing("unknown/vendor-model") is None


def test_fetch_model_pricing_live_fallback_malformed_list_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 200 response where ``data`` is a non-list shape returns None
    without raising.  Covers: dict (old single-model shape), null, bare
    string, and nested-garbage.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        _make_catalog(or_catalog=None),
    )

    for malformed_payload in [
        {"data": {"id": "k", "pricing": {}}},  # dict, not list
        {"data": None},
        {"data": "garbage-string"},
        {"not_data_key": [1, 2, 3]},
        "bare-string-at-top-level",
    ]:
        # Clear the cache between iterations so each shape is freshly checked.
        context_window._live_pricing_cache.clear()
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda url, timeout=3, _p=malformed_payload: _ListResp(_p),
        )
        result = fetch_model_pricing("test/model-v1")
        assert result is None, (
            f"Malformed payload returned non-None: {result!r}  payload={malformed_payload!r}"
        )


def test_fetch_model_pricing_live_fallback_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live fallback results are cached; second call doesn't hit the network."""
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        _make_catalog(or_catalog=None),
    )

    call_count = 0

    list_payload = _or_list_response(
        {"id": "test/cached-model", "pricing": {"prompt": "1e-6", "completion": "5e-6"}},
    )

    def _counting_urlopen(url: str, timeout: int = 3) -> _ListResp:
        nonlocal call_count
        call_count += 1
        return _ListResp(list_payload)

    monkeypatch.setattr(urllib.request, "urlopen", _counting_urlopen)

    p1 = fetch_model_pricing("test/cached-model")
    p2 = fetch_model_pricing("test/cached-model")
    assert p1 is not None
    assert p2 is not None
    assert p1.input_per_token == p2.input_per_token
    assert call_count == 1, f"Network called {call_count} times; expected 1 (cached)"


def test_fetch_model_pricing_live_fallback_price_string_zero_is_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A pricing string of ``"0"`` is an authoritative zero, not "absent".
    Only absent/None fields are treated as unknown.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    monkeypatch.setattr(
        context_window,
        "_fetch_mlflow_provider_catalog",
        _make_catalog(or_catalog=None),
    )

    list_payload = _or_list_response(
        {"id": "free/model", "pricing": {"prompt": "0", "completion": "0"}},
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout=3: _ListResp(list_payload),
    )

    pricing = fetch_model_pricing("free/model")
    assert pricing is not None, "Price string '0' should be parsed as 0.0"
    assert pricing.input_per_token == 0.0
    assert pricing.output_per_token == 0.0


def test_fetch_model_pricing_own_catalog_precedence_over_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A vendor/model id priced by its OWN provider catalog returns the
    own-catalog price, even when the openrouter catalog holds the same
    key at a DIFFERENT price.  This locks in precedence.

    Example: "anthropic/claude-sonnet-4-6" -- if "anthropic" catalog has
    it at $3/M input and openrouter catalog has it at $99/M input, the
    own-catalog price wins.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()

    OWN_PRICE = 3.0  # $/M tokens -- own catalog
    OR_PRICE = 99.0  # $/M tokens -- openrouter catalog (different!)

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "anthropic":
            return {
                "claude-sonnet-4-6": {
                    "pricing": {
                        "input_per_million_tokens": OWN_PRICE,
                        "output_per_million_tokens": 15.0,
                    }
                }
            }
        if provider == "openrouter":
            return {
                "anthropic/claude-sonnet-4-6": {
                    "pricing": {
                        "input_per_million_tokens": OR_PRICE,
                        "output_per_million_tokens": 450.0,
                    }
                }
            }
        return None

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)

    pricing = fetch_model_pricing("anthropic/claude-sonnet-4-6")
    assert pricing is not None
    assert pricing.input_per_token == pytest.approx(OWN_PRICE / 1_000_000), (
        "Own-provider catalog price must take precedence over openrouter"
    )


# ---------------------------------------------------------------------------
# New tests for review items
# ---------------------------------------------------------------------------


def test_fetch_model_pricing_multi_match_distinct_prices_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    (#1) Family-prefix fallback with >1 distinct price returns None.

    When multiple entries share a family prefix but have DIFFERENT pricing,
    the fallback cannot disambiguate. The old code built a set of
    ModelPricing objects which would TypeError on a non-frozen dataclass;
    the fix dedupes on a tuple key. This test exercises that path.

    Uses entries like "vendor/model-v1" and "vendor/model-v2" so
    rsplit("-", 1)[0] yields "vendor/model" and startswith("vendor/model-")
    matches both entries.  A query for "vendor/model-v99" (absent) should
    match the family prefix but return None because prices differ.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "openrouter":
            return {
                "vendor/model-v1": {
                    "pricing": {
                        "input_per_million_tokens": 1.0,
                        "output_per_million_tokens": 3.0,
                    }
                },
                "vendor/model-v2": {
                    "pricing": {
                        # DIFFERENT price from model-v1
                        "input_per_million_tokens": 2.0,
                        "output_per_million_tokens": 6.0,
                    }
                },
            }
        return None

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)

    # vendor/model-v99 shares the family prefix "vendor/model" with both
    # model-v1 and model-v2 (via startswith("vendor/model-")), but they
    # have different prices.
    pricing = fetch_model_pricing("vendor/model-v99")
    assert pricing is None, (
        f"Multi-match with >1 distinct price should return None, got {pricing!r}"
    )


def test_fetch_model_pricing_openrouter_prefixed_id_via_catalog_no_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    (#2) An ``openrouter/vendor/model`` id resolves via catalog without
    hitting the network.

    The ``openrouter/`` prefix is stripped before the catalog retry lookup,
    so ``openrouter/vendor/model`` matches the openrouter catalog key
    ``vendor/model``. No network (live API) call should be made.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()

    network_called = False

    def _boom(url: str, timeout: int = 3):
        nonlocal network_called
        network_called = True
        raise AssertionError("Network should not be called for catalog-resolved id")

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "openrouter":
            return {
                "vendor/model-x": {
                    "pricing": {
                        "input_per_million_tokens": 1.0,
                        "output_per_million_tokens": 3.0,
                    }
                }
            }
        return None

    # vendor has no own-provider catalog (returns None), so the fallback fires

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    pricing = fetch_model_pricing("openrouter/vendor/model-x")
    assert pricing is not None, "openrouter/vendor/model-x was not priced via catalog"
    assert pricing.input_per_token == pytest.approx(1.0e-6)
    assert not network_called, "Network was called despite catalog match"


def test_fetch_model_pricing_directly_routed_id_no_openrouter_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    (#3) A directly-routed ``openai/gpt-4o``-style id does NOT get OpenRouter
    pricing via the fallback path.

    When the own-provider catalog has no entry for a directly-routed id,
    the function returns None -- it must NOT fall through to OpenRouter's
    public rates (which may differ from the provider's contract rates).
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()

    or_api_called = False

    def _boom(url: str, timeout: int = 3):
        nonlocal or_api_called
        or_api_called = True
        raise AssertionError("OpenRouter API should not be called for directly-routed provider id")

    def _catalog(provider: str) -> dict[str, Any] | None:
        # openai catalog EXISTS and is recognized, but lacks gpt-4o
        if provider == "openai":
            return {
                "gpt-5": {
                    "pricing": {
                        "input_per_million_tokens": 5.0,
                        "output_per_million_tokens": 15.0,
                    }
                }
            }
        # openrouter catalog has gpt-4o at a different price (should NOT be reached)
        if provider == "openrouter":
            return {
                "openai/gpt-4o": {
                    "pricing": {
                        "input_per_million_tokens": 99.0,
                        "output_per_million_tokens": 99.0,
                    }
                }
            }
        return None

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    pricing = fetch_model_pricing("openai/gpt-4o")
    # The own-provider catalog has gpt-5 which matches gpt-4o's family prefix
    # "gpt-" (via startswith("gpt-")).  This is correct own-provider pricing.
    # The key assertion: it must NOT get OpenRouter's $99/M rate.
    assert pricing is not None, (
        "openai/gpt-4o should be priced via own-provider family-prefix "
        "(gpt-5 matches gpt-), but got None"
    )
    assert pricing.input_per_token == pytest.approx(5e-6), (
        f"openai/gpt-4o should get own-provider rate (5e-6), "
        f"not OpenRouter rate (99e-6). Got {pricing.input_per_token}"
    )
    assert not or_api_called, "OpenRouter API was called for a directly-routed provider id"


def test_fetch_model_pricing_two_cold_models_single_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    (#4) Two different cold models trigger only one network download.

    The live OpenRouter list endpoint (~342 entries) is fetched once and
    cached under a singleton key. Per-model lookups are plain dict.get()
    against this cached map, so the "one network call" property holds
    across different models.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    context_window._live_pricing_failure_cache.clear()

    download_count = 0

    list_payload = _or_list_response(
        {"id": "vendor/model-a", "pricing": {"prompt": "1e-6", "completion": "5e-6"}},
        {"id": "vendor/model-b", "pricing": {"prompt": "2e-6", "completion": "8e-6"}},
    )

    def _counting_urlopen(url: str, timeout: int = 3) -> _ListResp:
        nonlocal download_count
        download_count += 1
        return _ListResp(list_payload)

    monkeypatch.setattr(
        context_window, "_fetch_mlflow_provider_catalog", _make_catalog(or_catalog=None)
    )
    monkeypatch.setattr(urllib.request, "urlopen", _counting_urlopen)

    p1 = fetch_model_pricing("vendor/model-a")
    p2 = fetch_model_pricing("vendor/model-b")

    assert p1 is not None, "vendor/model-a was not priced"
    assert p2 is not None, "vendor/model-b was not priced"
    assert p1.input_per_token == pytest.approx(1e-6)
    assert p2.input_per_token == pytest.approx(2e-6)
    assert download_count == 1, (
        f"Expected 1 network download, got {download_count}. "
        "The full list should be cached under a singleton key."
    )


def test_fetch_model_pricing_transient_failure_short_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    (#5) Transient failures (timeout/DNS/5xx) are cached for a short TTL,
    not the full 1-hour "not found" TTL.

    After the short TTL expires, the next call should retry the network.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    context_window._live_pricing_failure_cache.clear()

    attempt_count = 0

    def _timeout_then_succeed(url: str, timeout: int = 3):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise TimeoutError("simulated timeout")
        return _ListResp(
            _or_list_response(
                {"id": "vendor/model", "pricing": {"prompt": "1e-6", "completion": "5e-6"}},
            )
        )

    monkeypatch.setattr(
        context_window, "_fetch_mlflow_provider_catalog", _make_catalog(or_catalog=None)
    )
    monkeypatch.setattr(urllib.request, "urlopen", _timeout_then_succeed)

    # First call: timeout -> cached as failure
    p1 = fetch_model_pricing("vendor/model")
    assert p1 is None
    assert attempt_count == 1

    # Simulate TTL expiry by clearing the failure cache
    context_window._live_pricing_failure_cache.clear()

    # Second call: should retry and succeed
    p2 = fetch_model_pricing("vendor/model")
    assert p2 is not None, "Should succeed after failure cache expires"
    assert p2.input_per_token == pytest.approx(1e-6)
    assert attempt_count == 2, f"Expected 2 attempts (failure + retry), got {attempt_count}"


def test_fetch_model_pricing_pinned_bare_id_xiaomi_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The pinned bare id ``xiaomi/mimo-v2.5-pro`` (no ``openrouter/`` prefix)
    resolves to OpenRouter pricing via the catalog retry.

    ``xiaomi`` is not a recognized MLflow provider (catalog is None), so the
    OpenRouter fallback fires and the openrouter catalog stores the id
    verbatim.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "openrouter":
            return {
                "xiaomi/mimo-v2.5-pro": {
                    "pricing": {
                        "input_per_million_tokens": 1.0,
                        "output_per_million_tokens": 3.0,
                        "cache_read_per_million_tokens": 0.2,
                        "cache_write_per_million_tokens": 0.0,
                    }
                }
            }
        return None

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)

    pricing = fetch_model_pricing("xiaomi/mimo-v2.5-pro")
    assert pricing is not None, (
        "Pinned bare id xiaomi/mimo-v2.5-pro must be priced (via openrouter catalog retry)"
    )
    assert pricing.input_per_token == pytest.approx(1.0e-6)
    assert pricing.output_per_token == pytest.approx(3.0e-6)
    assert pricing.cache_read_per_token == pytest.approx(0.2e-6)


def test_fetch_model_pricing_pinned_bare_id_kimi_k3_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The pinned bare id ``moonshotai/kimi-k3`` (no ``openrouter/`` prefix)
    resolves to OpenRouter pricing via catalog family-prefix fallback.

    ``moonshotai`` is not a recognized MLflow provider. The openrouter
    catalog contains ``moonshotai/kimi-k2.5`` so the family-prefix fallback
    matches.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()

    def _catalog(provider: str) -> dict[str, Any] | None:
        if provider == "openrouter":
            return {
                "moonshotai/kimi-k2.5": {
                    "pricing": {
                        "input_per_million_tokens": 0.6,
                        "output_per_million_tokens": 3.0,
                        "cache_read_per_million_tokens": 0.1,
                    }
                }
            }
        return None

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)

    pricing = fetch_model_pricing("moonshotai/kimi-k3")
    assert pricing is not None, (
        "Pinned bare id moonshotai/kimi-k3 must be priced (via openrouter family-prefix)"
    )
    assert pricing.input_per_token == pytest.approx(0.6e-6)
    assert pricing.output_per_token == pytest.approx(3.0e-6)


def test_fetch_model_pricing_pinned_bare_id_glm52_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The pinned bare id ``z-ai/glm-5.2`` (no ``openrouter/`` prefix)
    resolves to OpenRouter pricing via the live API fallback.

    ``z-ai`` is not a recognized MLflow provider and is absent from the
    openrouter MLflow catalog, so the live list-endpoint fallback fires.
    """
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    context_window._live_pricing_cache.clear()
    context_window._live_pricing_failure_cache.clear()

    def _catalog(provider: str) -> dict[str, Any] | None:
        return None

    list_payload = _or_list_response(
        {"id": "some/other", "pricing": {"prompt": "1", "completion": "2"}},
        {
            "id": "z-ai/glm-5.2",
            "pricing": {
                "prompt": "0.0000007938",
                "completion": "0.0000024948",
                "input_cache_read": "0.00000014742",
            },
        },
    )

    def _fake_urlopen(url: str, timeout: int = 3) -> _ListResp:
        assert "openrouter.ai/api/v1/models" in url
        return _ListResp(list_payload)

    monkeypatch.setattr(context_window, "_fetch_mlflow_provider_catalog", _catalog)
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    pricing = fetch_model_pricing("z-ai/glm-5.2")
    assert pricing is not None, (
        "Pinned bare id z-ai/glm-5.2 must be priced (via live OpenRouter API)"
    )
    assert pricing.input_per_token == pytest.approx(7.938e-7)
    assert pricing.output_per_token == pytest.approx(2.4948e-6)
    assert pricing.cache_read_per_token == pytest.approx(1.4742e-7)
