"""Tests for the runner busy check that gates managed-sandbox keepalive."""

from __future__ import annotations

import math

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


def test_keep_warm_lasts_the_grace_after_the_last_work(monkeypatch) -> None:
    from omnigent.runner import busy_check

    clock = [100.0]
    monkeypatch.setattr(busy_check.time, "monotonic", lambda: clock[0])
    active = False
    busy = RunnerBusyCheck(lambda: active)
    # No work yet: the grace counts from the runner's start, like its idle timeout.
    clock[0] = 159.0
    assert busy.keep_warm(60.0)
    clock[0] = 160.0
    assert not busy.keep_warm(60.0)
    active = True
    assert busy.keep_warm(60.0)
    active = False
    clock[0] = 219.0
    assert busy.keep_warm(60.0), "within the grace after the last work"
    clock[0] = 220.0
    assert not busy.keep_warm(60.0)
    assert not busy.keep_warm(0.0)
    assert busy.keep_warm(math.inf), "an unlimited grace keeps it warm while connected"


def test_each_consumer_tracks_progress_independently() -> None:
    activity_at = 1.0
    first = RunnerBusyCheck(lambda: False, lambda: activity_at)
    second = RunnerBusyCheck(lambda: False, lambda: activity_at)
    assert first.check()
    assert second.check(), "one consumer's check must not hide progress from another"
