"""Tests for the runner busy check that gates managed-sandbox keepalive."""

from __future__ import annotations

from omnigent.runner.busy_check import RunnerBusyCheck


def test_active_work_is_busy() -> None:
    active = True
    busy = RunnerBusyCheck(lambda: active)
    assert busy.check()
    active = False
    assert not busy.check()


def test_native_progress_is_busy_only_when_it_advances() -> None:
    activity_at: float | None = None
    busy = RunnerBusyCheck(lambda: False, lambda: activity_at)
    assert not busy.check()
    activity_at = 1.0
    assert busy.check()
    assert not busy.check(), "unchanged progress must not keep reporting busy"
    activity_at = 2.0
    assert busy.check()


def test_each_consumer_tracks_progress_independently() -> None:
    activity_at = 1.0
    first = RunnerBusyCheck(lambda: False, lambda: activity_at)
    second = RunnerBusyCheck(lambda: False, lambda: activity_at)
    assert first.check()
    assert second.check(), "one consumer's check must not hide progress from another"
