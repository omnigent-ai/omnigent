"""Request accounting stays correct when Codex totals span several requests."""

import pytest

from omnigent.llms.codex_usage import (
    compute_itemized_cost,
    request_usage_delta,
    token_counts,
    usage_from_counts,
)
from omnigent.llms.context_window import ModelPricing, compute_llm_cost


@pytest.mark.parametrize(
    "requests,expected",
    [
        ([(200_000, 0, 100), (200_000, 0, 100)], 0.802),
        ([(200_000, 0, 100), (300_000, 280_000, 100)], 0.5945),
        ([(300_000, 280_000, 100), (200_000, 0, 100)], 0.5945),
    ],
)
def test_request_cost_survives_aggregation(requests, expected):
    pricing = ModelPricing(
        2e-6, 10e-6, 0.2e-6, long_context=((272_000, ModelPricing(4e-6, 15e-6, 0.4e-6)),)
    )
    baseline = (0, 0, 0)
    itemized = []
    for request in requests:
        total = tuple(a + b for a, b in zip(baseline, request, strict=True))
        itemized.extend(request_usage_delta(total, request, baseline))
        assert request_usage_delta(total, request, total) == []
        baseline = total
    usage = usage_from_counts(baseline)
    assert compute_itemized_cost(usage, pricing, itemized) == pytest.approx(expected)
    # Historical totals cannot prove the size of any individual request.
    assert compute_llm_cost(usage, pricing) == pytest.approx(
        usage["input_tokens"] * 2e-6
        + usage["cache_read_input_tokens"] * 0.2e-6
        + usage["output_tokens"] * 10e-6
    )
    custom = ModelPricing(1e-6, 1e-6, 1e-6)
    assert compute_itemized_cost(usage, custom, itemized) == compute_llm_cost(usage, custom)


@pytest.mark.parametrize(
    "total,last,baseline,expected",
    [
        ((500_000, 0, 200), (300_000, 0, 100), (0, 0, 0), [None, 300_000]),
        ((500_000, 0, 200), None, (0, 0, 0), [None]),
        ((300_000, 0, 100), (300_000, 0, 100), (500_000, 0, 200), [300_000]),
        ((300_000, 0, 100), None, (500_000, 0, 200), []),
        ((0, 0, 0), (300_000, 0, 100), (0, 0, 0), []),
        ((200_000, 0, 100), (300_000, 0, 100), (0, 0, 0), [None]),
    ],
)
def test_missing_inherited_and_reset_boundaries(total, last, baseline, expected):
    parts = request_usage_delta(total, last, baseline)
    assert [part.get("request_input_tokens") for part in parts] == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"inputTokens": 300_000},
        {"inputTokens": 1, "cachedInputTokens": 2, "outputTokens": 0},
        {"inputTokens": True, "cachedInputTokens": 0, "outputTokens": 0},
    ],
)
def test_incomplete_counts_are_not_request_evidence(raw):
    assert token_counts(raw) is None


def test_malformed_itemization_keeps_flat_estimate():
    pricing = ModelPricing(1, 1, long_context=((10, ModelPricing(2, 2)),))
    usage = {"input_tokens": 20, "cache_read_input_tokens": 0, "output_tokens": 1}
    for item in (
        {**usage, "request_input_tokens": 21},
        {**usage, "request_input_tokens": 20, "input_tokens": 21},
        {**usage, "request_input_tokens": 20, "output_tokens": -1},
    ):
        assert compute_itemized_cost(usage, pricing, [item]) == 21


def test_itemization_cannot_inject_unsanitized_cache_creation():
    pricing = ModelPricing(1, 1, long_context=((10, ModelPricing(2, 2)),))
    usage = {"input_tokens": 20, "cache_read_input_tokens": 0, "output_tokens": 1}
    item = {**usage, "request_input_tokens": 20, "cache_creation_input_tokens": -1000}
    assert compute_itemized_cost(usage, pricing, [item]) == 42


def test_verified_child_reset_charges_equal_owned_totals_once():
    from omnigent.llms.codex_usage import native_cost_with_requests

    pricing = ModelPricing(
        2e-6, 10e-6, 0.2e-6, long_context=((272_000, ModelPricing(4e-6, 15e-6, 0.4e-6)),)
    )
    raw = {"inputTokens": 300_000, "cachedInputTokens": 280_000, "outputTokens": 100}
    usage = usage_from_counts((300_000, 280_000, 100))
    data = {
        "codex_pricing_source": "child",
        "codex_pricing_updates": [
            {"sequence": 1, "total": raw, "last": raw},
        ],
    }
    cost = native_cost_with_requests(data, usage, {}, 0, pricing)
    assert cost == pytest.approx(0.1935)
    data["codex_pricing_updates"] = [
        {"sequence": 2, "counter_reset": True, "total": raw, "last": raw},
    ]
    cost = native_cost_with_requests(data, usage, usage, cost, pricing)
    assert cost == pytest.approx(0.387)
    assert native_cost_with_requests(data, usage, usage, cost, pricing) == pytest.approx(cost)
