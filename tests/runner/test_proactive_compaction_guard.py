"""Unit tests for the runner's futile-recompaction guard.

The runner triggers proactive compaction off the provider-reported context
fill (``provider_tokens``), but runner-side compaction can't shrink a harness's
*own* session context. ``_should_skip_futile_recompaction`` prevents re-firing
every turn when the fill hasn't dropped since the last compaction.
"""

from __future__ import annotations

from omnigent.runner.app import _should_skip_futile_recompaction


def test_skips_when_fill_did_not_drop() -> None:
    """Fill stayed flat since the last compaction → skip (futile re-fire)."""
    assert _should_skip_futile_recompaction(180_000, 180_000) is True


def test_skips_when_fill_grew() -> None:
    """Fill grew despite the last compaction → skip (the SDK loop)."""
    assert _should_skip_futile_recompaction(197_000, 180_000) is True


def test_allows_when_fill_dropped() -> None:
    """Fill fell below the last-compacted value → compaction helped, allow."""
    assert _should_skip_futile_recompaction(120_000, 180_000) is False


def test_never_skips_without_provider_tokens() -> None:
    """Tiktoken path (no provider tokens) never loops → never skip."""
    assert _should_skip_futile_recompaction(None, 180_000) is False


def test_never_skips_before_first_provider_compaction() -> None:
    """No prior provider-reported compaction recorded → allow the first one."""
    assert _should_skip_futile_recompaction(197_000, None) is False
