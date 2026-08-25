"""
Tests for model pricing and cache-aware LLM cost computation.

Covers :class:`ModelPricing`, :func:`compute_llm_cost` (the cache-aware
cost formula), and :func:`fetch_model_pricing`'s parsing of cache-read /
cache-write rates from a catalog entry.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.llms import context_window
from omnigent.llms.context_window import (
    ModelPricing,
    _catalog_context_window,
    _encoded_context_window,
    compute_llm_cost,
    fetch_model_pricing,
    get_model_context_window,
    resolve_effective_context_window,
)
from omnigent.onboarding.providers import ModelInfo


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
    monkeypatch.setattr(
        context_window,
        "find_catalog_models",
        lambda _model: [
            ModelInfo(
                name="catalog-model",
                provider="provider",
                input_price=2.5,
                output_price=10.0,
                cache_read_price=0.25,
                cache_write_price=3.125,
            )
        ],
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
    monkeypatch.setattr(
        context_window,
        "find_catalog_models",
        lambda _model: [
            ModelInfo(
                name="catalog-model",
                provider="provider",
                input_price=1.25,
                output_price=10.0,
            )
        ],
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
    monkeypatch.setattr(
        context_window,
        "find_catalog_models",
        lambda _model: [
            ModelInfo(
                name="catalog-model",
                provider="anthropic",
                input_price=15.0,
                output_price=75.0,
            )
        ],
    )

    pricing = fetch_model_pricing("databricks-claude-opus-4-8")
    assert pricing is not None, (
        "databricks-claude-opus-4-8 was not priced — the databricks→base "
        "fallback did not reach anthropic's claude-opus-4-8."
    )
    # Priced from the base model's rates (15 / 75 per million), not the
    # databricks sonnet entry (3 / 15).
    assert pricing.input_per_token == pytest.approx(15e-6)
    assert pricing.output_per_token == pytest.approx(75e-6)


def test_catalog_context_window_uses_max_input_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context sizing uses the catalog's normalized max-input field."""
    monkeypatch.setattr(
        context_window,
        "find_catalog_models",
        lambda _model: [
            ModelInfo(
                name="catalog-model",
                provider="provider",
                max_input_tokens=997_952,
                max_output_tokens=65_536,
            )
        ],
    )

    assert _catalog_context_window("catalog-model") == 997_952


def test_catalog_context_window_rejects_ambiguous_family_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A family fallback is used only when every catalog entry agrees."""
    monkeypatch.setattr(
        context_window,
        "find_catalog_models",
        lambda _model: [
            ModelInfo(name="family-a", provider="provider", max_input_tokens=100_000),
            ModelInfo(name="family-b", provider="provider", max_input_tokens=200_000),
        ],
    )

    assert _catalog_context_window("family-new") is None


def test_encoded_context_window_resolves_anthropic_1m_beta_suffix() -> None:
    """The Anthropic ``[1m]`` beta marker resolves to a 1M window.

    The suffix *is* the window — we read it, not strip it — so any
    ``<model>[1m]`` resolves to 1,000,000 while the bare base defers to the
    upstream backends (which may size it differently).
    """
    assert _encoded_context_window("claude-opus-4-8[1m]") == 1_000_000
    assert _encoded_context_window("anthropic/claude-opus-4-8[1m]") == 1_000_000
    assert _encoded_context_window("claude-sonnet-4-6[1m]") == 1_000_000
    assert _encoded_context_window("CLAUDE-OPUS-4-8[1M]") == 1_000_000
    # Databricks-hosted Claude (contains "claude") also resolves.
    assert _encoded_context_window("databricks-claude-opus-4-8[1m]") == 1_000_000
    # Without the suffix the encoded path defers to catalog metadata.
    assert _encoded_context_window("claude-opus-4-8") is None
    # The rule is Claude-scoped: a non-Claude id ending in [1m] is NOT forced to
    # 1M (it defers to litellm/catalog), so custom/self-hosted ids are safe.
    assert _encoded_context_window("my-local-model[1m]") is None
    assert _encoded_context_window("gpt-5.4[1m]") is None


def test_get_model_context_window_prefers_catalog_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dynamic catalog metadata replaces the removed exact-id registry."""
    monkeypatch.delenv("AP_CONTEXT_WINDOW_OVERRIDE", raising=False)
    monkeypatch.setattr(
        context_window,
        "find_catalog_models",
        lambda _model: [
            ModelInfo(name="catalog-model", provider="provider", max_input_tokens=997_952)
        ],
    )

    assert get_model_context_window("catalog-model") == 997_952


def test_get_model_context_window_encoded_and_offline_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Encoded metadata remains available offline; unknown ids stay conservative."""
    monkeypatch.delenv("AP_CONTEXT_WINDOW_OVERRIDE", raising=False)
    monkeypatch.setattr(context_window, "find_catalog_models", lambda _model: [])
    assert get_model_context_window("claude-opus-4-8[1m]") == 1_000_000
    assert get_model_context_window("anthropic/claude-opus-4-8[1m]") == 1_000_000
    assert get_model_context_window("uncatalogued-model") == 128_000
