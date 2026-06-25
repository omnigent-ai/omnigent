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
    _qwen_context_window,
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


def test_claude_context_window_override_normalizes_id() -> None:
    """The curated Claude override matches across all the namings the model id
    reaches us in — gateway ``databricks-`` prefix, provider prefix, ``[1m]``
    bracket alias, ``:tag`` suffix, and case."""
    from omnigent.llms.context_window import _claude_context_window_override as ov

    for ident in (
        "claude-opus-4-8",
        "databricks-claude-opus-4-8",
        "anthropic/claude-opus-4-8",
        "databricks/databricks-claude-opus-4-8",
        "claude-opus-4-8[1m]",
        "Databricks-Claude-Opus-4-8",
        "claude-opus-4-8:beta",
    ):
        assert ov(ident) == 1_000_000, ident
    # Other current 1M Claude models are covered too.
    assert ov("databricks-claude-sonnet-4-6") == 1_000_000
    assert ov("databricks-claude-opus-4-6") == 1_000_000
    # Non-1M / unknown models are NOT overridden (caller continues the chain).
    assert ov("databricks-claude-sonnet-4-5") is None
    assert ov("databricks-gpt-5-5") is None
    assert ov("claude-3-opus-20240229") is None


def test_get_model_context_window_claude_1m_override_wins() -> None:
    """A 1M Claude model resolves to 1M — the override runs before litellm /
    the catalog (which report the 200K Anthropic base), so it wins regardless
    of those backends."""
    assert get_model_context_window("databricks-claude-opus-4-8") == 1_000_000
    assert get_model_context_window("claude-opus-4-8") == 1_000_000


def test_env_override_still_beats_claude_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AP_CONTEXT_WINDOW_OVERRIDE`` outranks the curated Claude override."""
    monkeypatch.setenv("AP_CONTEXT_WINDOW_OVERRIDE", "12345")
    assert get_model_context_window("databricks-claude-opus-4-8") == 12345


def test_resolve_effective_window_opus_4_8_defaults_to_1m() -> None:
    """With no declared window, opus-4-8 resolves to 1M via the override; an
    explicit spec window still wins over it."""
    assert resolve_effective_context_window(None, "databricks-claude-opus-4-8") == 1_000_000
    # Author-declared window is authoritative (e.g. intentionally smaller).
    assert resolve_effective_context_window(200_000, "databricks-claude-opus-4-8") == 200_000


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
# Qwen context-window fallback (models absent from litellm + MLflow catalog)
# ---------------------------------------------------------------------------


def test_qwen_context_window_normalizes_id() -> None:
    """The lookup strips provider prefixes and ``:tag`` suffixes before matching."""
    assert _qwen_context_window("qwen3-coder-plus") == 1_048_576
    assert _qwen_context_window("qwen/qwen3-coder") == 262_144
    assert _qwen_context_window("qwen3-coder:free") == 262_144
    assert _qwen_context_window("openrouter/qwen/qwen3-coder:free") == 262_144
    assert _qwen_context_window("QWEN3-CODER-PLUS") == 1_048_576  # case-insensitive
    # Unknown qwen variant → None (caller falls back to the default).
    assert _qwen_context_window("qwen-nonexistent-xyz") is None


def test_get_model_context_window_uses_qwen_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A known qwen model resolves to its curated window, not the 128K default.

    Catalog lookup is disabled so the resolution is hermetic (no network):
    litellm has no qwen entry, MLflow is skipped, so the qwen table answers.
    """
    monkeypatch.setenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", "1")
    monkeypatch.delenv("AP_CONTEXT_WINDOW_OVERRIDE", raising=False)
    assert get_model_context_window("qwen3-coder-plus") == 1_048_576
    # An unrecognized qwen model still falls back to the conservative default.
    assert get_model_context_window("qwen-nonexistent-xyz") == 128_000
