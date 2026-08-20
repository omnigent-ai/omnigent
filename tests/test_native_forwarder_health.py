"""Tests for the process-local native-forwarder POST-failure record.

``omnigent/_native_forwarder_health.py`` is the small shared sink that lets a
native forwarder report a connectivity failure to the harness idle-turn
watchdog (issue #1119). These cover its record / recency / clear contract; the
writer (forwarder retry loops) and reader (watchdog) integrations are tested in
``tests/test_native_post_delivery.py``, ``tests/test_codex_native_forwarder.py``,
and ``tests/runtime/harnesses/test_scaffold.py``.
"""

from __future__ import annotations

import httpx
import pytest

from omnigent import _native_forwarder_health as health


class _FakeClock:
    """Controllable monotonic clock so recency is tested over real intervals."""

    def __init__(self, start: float) -> None:
        """Start the clock at *start* seconds."""
        self.now = start

    def monotonic(self) -> float:
        """Return the current fake monotonic time."""
        return self.now


def test_recent_post_failure_round_trips_within_window() -> None:
    """A recorded failure is returned, with its event type and error repr."""
    health.clear()
    try:
        assert health.recent_post_failure(60.0) is None
        health.record_post_failure(
            "external_session_status", httpx.ConnectError("No route to host")
        )
        detail = health.recent_post_failure(60.0)
        assert detail is not None
        assert "external_session_status" in detail
        assert "No route to host" in detail
    finally:
        health.clear()


def test_recent_post_failure_respects_recency_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure older than the window is suppressed; one inside it is returned.

    Uses a controlled clock so the boundary is exercised over a realistic
    interval (record at t=1000, read 100s later) rather than a degenerate
    zero-length window — pinning the actual ``elapsed > within_s`` comparison.
    """
    clock = _FakeClock(start=1000.0)
    monkeypatch.setattr(health, "time", clock)
    health.clear()
    try:
        health.record_post_failure(
            "external_session_status", httpx.ConnectError("No route to host")
        )
        # 100s elapse before any read.
        clock.now = 1100.0
        # Window shorter than the elapsed gap → stale → suppressed.
        assert health.recent_post_failure(60.0) is None
        # Window longer than the gap → still surfaced, unchanged by the
        # earlier stale read.
        assert health.recent_post_failure(240.0) is not None
    finally:
        health.clear()


def test_clear_forgets_the_record() -> None:
    """``clear`` resets the slot so later reads see nothing."""
    health.record_post_failure("external_session_status", httpx.ConnectError("boom"))
    health.clear()
    assert health.recent_post_failure(60.0) is None


def test_note_post_success_clears_a_prior_failure() -> None:
    """A successful POST clears the slot so a recovered connection isn't blamed.

    Guards the misattribution fix: once connectivity returns and a POST gets a
    response, the recorded failure must not survive to be attached to a later,
    unrelated idle-watchdog stall.
    """
    health.clear()
    try:
        health.record_post_failure("external_session_status", httpx.ConnectError("boom"))
        assert health.recent_post_failure(60.0) is not None
        health.note_post_success()
        assert health.recent_post_failure(60.0) is None
    finally:
        health.clear()
