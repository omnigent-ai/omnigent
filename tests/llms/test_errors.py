"""Unit tests for the byte-cap request-size overflow parser."""

from __future__ import annotations

import time

from omnigent.llms.errors import detect_request_size_overflow

# Enough repeated request fields that a backtracking parser takes many seconds;
# the linear scanner needs milliseconds. The budget is deliberately generous so
# shared-runner load cannot trip it, yet still fails fast if backtracking returns.
_HOSTILE_REPEATS = 20_000
_TIME_BUDGET_S = 5.0


def test_detect_request_size_overflow_matches_databricks_rejection() -> None:
    """The Databricks front-door rejection parses into request/limit bytes."""
    result = detect_request_size_overflow(
        "Server received a request which exceeds maximum allowed content "
        "length. RequestSize(bytes): 33967957, Limit(bytes): 33554432"
    )

    assert result is not None
    assert result.request_bytes == 33967957
    assert result.limit_bytes == 33554432


def test_detect_request_size_overflow_phrase_without_sizes_is_none() -> None:
    """The phrase alone, with no byte fields, is not a byte-cap rejection."""
    assert (
        detect_request_size_overflow(
            "Server received a request which exceeds maximum allowed content length."
        )
        is None
    )


def test_detect_request_size_overflow_repeated_requests_without_limit_is_bounded() -> None:
    """Repeated request fields with no limit return None without backtracking."""
    hostile = (
        "exceeds maximum allowed content length " + "RequestSize(bytes): 1 " * _HOSTILE_REPEATS
    )

    start = time.perf_counter()
    result = detect_request_size_overflow(hostile)
    elapsed = time.perf_counter() - start

    assert result is None
    assert elapsed < _TIME_BUDGET_S, f"parser took {elapsed:.3f}s on hostile input"


def test_detect_request_size_overflow_repeated_fields_ending_in_valid_pair() -> None:
    """A valid pair after repeated request noise is still detected, bounded."""
    message = (
        "exceeds maximum allowed content length "
        + "RequestSize(bytes): 1 " * _HOSTILE_REPEATS
        + "RequestSize(bytes): 33967957, Limit(bytes): 33554432"
    )

    start = time.perf_counter()
    result = detect_request_size_overflow(message)
    elapsed = time.perf_counter() - start

    assert result is not None
    # Only the adjacent request/limit pair matches; the standalone noise fields
    # are skipped, so the real cap's request and limit are returned intact.
    assert result.request_bytes == 33967957
    assert result.limit_bytes == 33554432
    assert elapsed < _TIME_BUDGET_S, f"parser took {elapsed:.3f}s on hostile input"


def test_detect_request_size_overflow_requires_adjacent_pair() -> None:
    """A stray request and a distant limit are not paired into an overflow."""
    assert (
        detect_request_size_overflow(
            "exceeds maximum allowed content length RequestSize(bytes): 1 "
            + "x " * 500
            + "Limit(bytes): 33554432"
        )
        is None
    )


# Python's int(str) conversion rejects more than this many digits by default.
_OVERLONG_DIGITS = "9" * 4301


def test_detect_request_size_overflow_overlong_request_field_is_none() -> None:
    """An overlong request field returns None instead of raising ValueError."""
    assert (
        detect_request_size_overflow(
            "exceeds maximum allowed content length. "
            f"RequestSize(bytes): {_OVERLONG_DIGITS}, Limit(bytes): 33554432"
        )
        is None
    )


def test_detect_request_size_overflow_overlong_limit_field_is_none() -> None:
    """An overlong limit field returns None instead of raising ValueError."""
    assert (
        detect_request_size_overflow(
            "exceeds maximum allowed content length. "
            f"RequestSize(bytes): 33967957, Limit(bytes): {_OVERLONG_DIGITS}"
        )
        is None
    )
