"""Tests for the read-only LLMQ status reduction."""

from __future__ import annotations

import math

from omnigent.server.routes.quota_status import _burst_key, _reduce

_NOW = 2_000_000.0


def _snapshot(**overrides: object) -> dict[str, object]:
    """A snapshot shaped like the controller's real ``/v1/snapshot`` output."""
    base: dict[str, object] = {
        "generated_at": 1_999_990.0,
        "workstreams": [
            {
                "id": "agent-infra",
                "explicit_share_ppm": None,
                "weight": 2.0,
                "active": 1,
                "borrow_after_seconds": 300,
                "last_seen_at": 1_999_900.0,
            },
            {
                "id": "openwebui",
                "explicit_share_ppm": 150000,
                "weight": 1.0,
                "active": 0,
                "borrow_after_seconds": 300,
                "last_seen_at": 1_000_000.0,
            },
        ],
        "windows": [
            {
                "provider": "anthropic",
                "lane": "claude-max",
                "limit_id": "claude",
                "window_name": "five_hour",
                "model_scope": "*",
                "used_ppm": 160000,
                "window_seconds": 18000,
                "resets_at": 2_018_000.0,
                "hard_allowed": None,
                "observed_at": 1_999_980.0,
                "source": "claude-native-usage",
            },
            {
                "provider": "openrouter",
                "lane": "personal-api",
                "limit_id": "key-spend-limit",
                "window_name": "daily",
                "model_scope": "*",
                "used_ppm": 900000,
                "window_seconds": 86400,
                "resets_at": 2_080_000.0,
                "hard_allowed": 1,
                "observed_at": 1_999_900.0,
                "source": "openrouter-local-daily-budget",
            },
        ],
        "reservations": [
            {
                "workstream": "agent-infra",
                "status": "active",
                "estimated_ppm": 8528,
                "created_at": _NOW - 120.0,
            },
            {
                "workstream": "agent-infra",
                "status": "active",
                "estimated_ppm": 8528,
                "created_at": _NOW - 30.0,
            },
            # Settled and expired reservations are history, not current load.
            {
                "workstream": "agent-infra",
                "status": "settled",
                "estimated_ppm": 99999,
                "created_at": _NOW - 5.0,
            },
            {
                "workstream": "openwebui",
                "status": "expired",
                "estimated_ppm": 77777,
                "created_at": _NOW - 5.0,
            },
        ],
    }
    base.update(overrides)
    return base


_BURST = {
    "initial_burst_factor": 2.0,
    "max_burst_factor": None,
    "adaptive_enabled": True,
    "current_burst_factors": {"anthropic|claude-max|claude|five_hour|*|2018000": 1.58},
}


def test_reduce_projects_windows_and_joins_burst_factors() -> None:
    status = _reduce(_snapshot(), _BURST, _NOW)

    # Fullest window first, so the one about to throttle the fleet leads.
    assert [window.used_ppm for window in status.windows] == [900000, 160000]
    openrouter, anthropic = status.windows
    assert openrouter.hard_allowed == 1
    assert anthropic.burst_factor == 1.58
    # No published factor for the OpenRouter window: absent, not defaulted.
    assert openrouter.burst_factor is None
    assert status.burst.initial_burst_factor == 2.0
    assert status.burst.max_burst_factor is None
    assert status.burst.adaptive_enabled is True


def test_burst_key_matches_the_controller_spelling() -> None:
    window = {
        "provider": "anthropic",
        "lane": "claude-max",
        "limit_id": "claude",
        "window_name": "seven_day",
        "model_scope": "fable",
        "resets_at": 1789142400.0,
    }
    assert _burst_key(window) == "anthropic|claude-max|claude|seven_day|fable|1789142400"


def test_reduce_counts_only_active_reservations_per_workstream() -> None:
    status = _reduce(_snapshot(), _BURST, _NOW)

    busiest, idle = status.workstreams
    assert busiest.id == "agent-infra"
    assert busiest.active_reservations == 2
    assert busiest.active_estimated_ppm == 8528 * 2
    # Oldest still-open reservation, not the most recent one.
    assert busiest.oldest_active_age_seconds == 120.0
    assert idle.id == "openwebui"
    assert idle.active_reservations == 0
    assert idle.oldest_active_age_seconds is None
    assert status.active_reservations == 2


def test_reduce_ignores_reservations_for_unknown_workstreams() -> None:
    snapshot = _snapshot(
        reservations=[
            {"workstream": "retired-bucket", "status": "active", "estimated_ppm": 500},
            {"workstream": "", "status": "active", "estimated_ppm": 500},
        ]
    )
    status = _reduce(snapshot, _BURST, _NOW)

    assert status.active_reservations == 0
    assert all(row.active_reservations == 0 for row in status.workstreams)


def test_reduce_survives_a_controller_that_omits_or_malforms_fields() -> None:
    snapshot = {
        "windows": [
            {"provider": "anthropic"},
            "not-a-window",
        ],
        "workstreams": [{"id": "solo"}, {"no_id": True}, "not-a-workstream"],
        "reservations": ["not-a-reservation"],
    }
    status = _reduce(snapshot, {}, _NOW)

    # A snapshot with no generated_at is stamped with the reduction time rather
    # than rendering as 1970 in the panel.
    assert status.generated_at == _NOW
    assert len(status.windows) == 1
    assert status.windows[0].lane == "unknown"
    assert status.windows[0].window_seconds is None
    assert [row.id for row in status.workstreams] == ["solo"]
    assert status.workstreams[0].weight == 1.0
    assert status.burst.adaptive_enabled is None


def test_reduce_rejects_non_finite_and_boolean_numbers() -> None:
    snapshot = _snapshot(
        windows=[
            {
                "provider": "anthropic",
                "lane": "claude-max",
                "limit_id": "claude",
                "window_name": "five_hour",
                "model_scope": "*",
                "used_ppm": True,
                "window_seconds": math.inf,
                "resets_at": math.nan,
            }
        ]
    )
    status = _reduce(snapshot, _BURST, _NOW)

    window = status.windows[0]
    # ``True`` is not a usage number; it must not become ``1``.
    assert window.used_ppm == 0
    assert window.window_seconds is None
    assert window.resets_at is None
    assert window.burst_factor is None


def test_reduce_clamps_reservation_age_when_controller_clock_runs_ahead() -> None:
    snapshot = _snapshot(
        reservations=[
            {"workstream": "agent-infra", "status": "active", "created_at": _NOW + 60.0},
        ]
    )
    status = _reduce(snapshot, _BURST, _NOW)

    assert status.workstreams[0].oldest_active_age_seconds == 0.0
