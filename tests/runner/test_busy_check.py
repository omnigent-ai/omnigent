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


def test_report_gives_seconds_since_the_last_work(monkeypatch) -> None:
    from omnigent.runner import busy_check

    clock = [100.0]
    monkeypatch.setattr(busy_check.time, "monotonic", lambda: clock[0])
    active = False
    busy = RunnerBusyCheck(lambda: active)
    clock[0] = 110.0
    # No work yet: idle since the check was created, like the runner idle timeout.
    assert busy.report() == (False, 10.0)
    active = True
    assert busy.report() == (True, 0.0)
    active = False
    clock[0] = 130.0
    assert busy.report() == (False, 20.0)


def test_each_consumer_tracks_progress_independently() -> None:
    activity_at = 1.0
    first = RunnerBusyCheck(lambda: False, lambda: activity_at)
    second = RunnerBusyCheck(lambda: False, lambda: activity_at)
    assert first.check()
    assert second.check(), "one consumer's check must not hide progress from another"
