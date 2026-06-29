"""Unit tests for the pure usage aggregator."""

from __future__ import annotations

from omnigent.server.usage_summary import aggregate_usage


def test_empty_yields_zero_totals_and_no_cost_key() -> None:
    summary = aggregate_usage([])
    assert summary["by_model"] == {}
    assert summary["totals"]["input_tokens"] == 0
    assert summary["totals"]["total_tokens"] == 0
    assert "total_cost_usd" not in summary["totals"]


def test_sums_tokens_and_cost_across_blobs() -> None:
    summary = aggregate_usage(
        [
            {"input_tokens": 100, "output_tokens": 10, "total_cost_usd": 0.5},
            {"input_tokens": 200, "output_tokens": 20, "total_cost_usd": 1.25},
            None,  # skipped
            {},  # skipped
        ]
    )
    assert summary["totals"]["input_tokens"] == 300
    assert summary["totals"]["output_tokens"] == 30
    assert summary["totals"]["total_cost_usd"] == 1.75


def test_merges_by_model_across_blobs() -> None:
    summary = aggregate_usage(
        [
            {"by_model": {"claude-sonnet-4-6": {"input_tokens": 100, "total_cost_usd": 0.4}}},
            {"by_model": {"claude-sonnet-4-6": {"input_tokens": 50, "total_cost_usd": 0.2}}},
            {"by_model": {"gpt-5": {"input_tokens": 70}}},  # unpriced model
        ]
    )
    sonnet = summary["by_model"]["claude-sonnet-4-6"]
    assert sonnet["input_tokens"] == 150
    assert sonnet["total_cost_usd"] == 0.6
    gpt = summary["by_model"]["gpt-5"]
    assert gpt["input_tokens"] == 70
    assert "total_cost_usd" not in gpt  # never priced → no cost key


def test_unpriced_totals_omit_cost_and_malformed_counts_as_zero() -> None:
    summary = aggregate_usage(
        [
            {"input_tokens": 5},  # no cost
            {"input_tokens": "oops", "output_tokens": None},  # malformed → 0
        ]
    )
    assert summary["totals"]["input_tokens"] == 5
    assert summary["totals"]["output_tokens"] == 0
    assert "total_cost_usd" not in summary["totals"]
