"""Tests for the scheduled-task stale-run backstop (lazy-on-read).

Exercises ``force_fail_stale_runs`` — the pure age-based orphan backstop the
scheduled-task read endpoints call. Completion of a normal run is event-driven
(``session_live_state.persist_scheduled_run_completion``, covered in
``tests/server/test_session_live_state.py``); there is no startup sweep and no
periodic reconcile, so this module only covers the stale force-fail policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omnigent.server.scheduled.run_reconciler import (
    STALE_RUN_ERROR_CODE,
    STALE_RUN_MAX_AGE_SECONDS,
    force_fail_stale_runs,
)


@dataclass
class _RunRow:
    """Mutable run row for the fake scheduled-task store."""

    id: str
    scheduled_task_id: str
    status: str
    scheduled_at: int
    conversation_id: str | None = None
    fired_at: int | None = None
    finished_at: int | None = None
    error: str | None = None
    error_code: str | None = None


class _FakeScheduledTaskStore:
    """Fake store exposing only ``update_run`` (what the backstop calls)."""

    def __init__(self, runs: list[_RunRow]) -> None:
        self._runs = {r.id: r for r in runs}
        self.update_calls: list[str] = []

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: int,
        error: str | None = None,
        error_code: str | None = None,
    ) -> _RunRow | None:
        self.update_calls.append(run_id)
        row = self._runs.get(run_id)
        if row is None or row.status != "running":
            return None  # conditional WHERE status = running
        row.status = status
        row.finished_at = finished_at
        row.error = error
        row.error_code = error_code
        return row


def _run(
    seed: str,
    *,
    status: str = "running",
    scheduled_at: int = 100,
    fired_at: int | None = None,
) -> _RunRow:
    return _RunRow(
        id=f"run_{seed}",
        scheduled_task_id=f"task_{seed}",
        status=status,
        scheduled_at=scheduled_at,
        conversation_id=f"conv_{seed}",
        fired_at=fired_at if fired_at is not None else scheduled_at + 1,
    )


def test_force_fails_stale_running_run() -> None:
    """A run past the max age flips to failed(incomplete) with finished_at."""
    now = 10_000_000
    run = _run("stale", scheduled_at=now - STALE_RUN_MAX_AGE_SECONDS - 1)
    store = _FakeScheduledTaskStore([run])
    out = force_fail_stale_runs(store, [run], now=now)
    assert run.status == "failed"
    assert run.error_code == STALE_RUN_ERROR_CODE
    assert run.finished_at == now
    # The returned list reflects the transition.
    assert out[0].status == "failed"


def test_leaves_young_running_run_untouched() -> None:
    """A recently-fired running run is not touched (no update attempted)."""
    now = 10_000_000
    run = _run("young", scheduled_at=now - 30)  # 30s old
    store = _FakeScheduledTaskStore([run])
    out = force_fail_stale_runs(store, [run], now=now)
    assert run.status == "running"
    assert run.finished_at is None
    assert store.update_calls == []  # never even attempted an update
    assert out[0].status == "running"


def test_age_measured_from_fired_at_force_fails() -> None:
    """Age is measured from fired_at: a run fired >6h ago is force-failed."""
    now = 10_000_000
    # Scheduled recently but fired_at is >6h ago (e.g. a clock/late-record edge):
    # fired_at is what counts, so this IS stale.
    run = _run("firedstale", scheduled_at=now - 60, fired_at=now - STALE_RUN_MAX_AGE_SECONDS - 1)
    store = _FakeScheduledTaskStore([run])
    force_fail_stale_runs(store, [run], now=now)
    assert run.status == "failed"
    assert run.error_code == STALE_RUN_ERROR_CODE
    assert run.finished_at == now


def test_scheduled_long_ago_but_fired_recently_left_alone() -> None:
    """A run scheduled >6h ago but fired recently is NOT force-failed.

    The behavior change: the 6h window measures from fired_at (when dispatch
    began), so a run that fired late still gets its full window and is not
    prematurely killed.
    """
    now = 10_000_000
    run = _run(
        "firedlate",
        scheduled_at=now - STALE_RUN_MAX_AGE_SECONDS - 3600,  # scheduled 7h+ ago
        fired_at=now - 60,  # but only fired 60s ago
    )
    store = _FakeScheduledTaskStore([run])
    force_fail_stale_runs(store, [run], now=now)
    assert run.status == "running"
    assert run.finished_at is None
    assert store.update_calls == []


def test_age_falls_back_to_scheduled_at_when_no_fired_at() -> None:
    """A run that never recorded fired_at ages from scheduled_at (fallback)."""
    now = 10_000_000
    run = _run("nofire", scheduled_at=now - STALE_RUN_MAX_AGE_SECONDS - 1, fired_at=None)
    # _run's default would set fired_at; force it None to exercise the fallback.
    run.fired_at = None
    store = _FakeScheduledTaskStore([run])
    force_fail_stale_runs(store, [run], now=now)
    assert run.status == "failed"
    assert run.error_code == STALE_RUN_ERROR_CODE


def test_leaves_already_terminal_run_untouched() -> None:
    """A terminal run (even if old) is not a candidate — status gate only."""
    now = 10_000_000
    run = _run("done", status="succeeded", scheduled_at=now - STALE_RUN_MAX_AGE_SECONDS - 100)
    store = _FakeScheduledTaskStore([run])
    force_fail_stale_runs(store, [run], now=now)
    assert run.status == "succeeded"
    assert store.update_calls == []


def test_mixed_batch_transitions_only_stale_running() -> None:
    """Across a batch, only stale running rows transition; others pass through."""
    now = 10_000_000
    stale = _run("stale", scheduled_at=now - STALE_RUN_MAX_AGE_SECONDS - 1)
    young = _run("young", scheduled_at=now - 10)
    done = _run("done", status="succeeded", scheduled_at=now - STALE_RUN_MAX_AGE_SECONDS - 5)
    store = _FakeScheduledTaskStore([stale, young, done])
    force_fail_stale_runs(store, [stale, young, done], now=now)
    assert stale.status == "failed" and stale.error_code == STALE_RUN_ERROR_CODE
    assert young.status == "running"
    assert done.status == "succeeded"
    assert store.update_calls == ["run_stale"]  # only the stale running row


def test_idempotent_when_run_already_transitioned() -> None:
    """A racing event-hook transition (update returns None) is not double-counted.

    Models the event hook winning between the read that listed the run and the
    backstop's update: the conditional ``WHERE status=running`` update returns
    None, and the original (stale) row is passed through unchanged in the list.
    """
    now = 10_000_000
    run = _run("race", scheduled_at=now - STALE_RUN_MAX_AGE_SECONDS - 1)

    class _RacingStore(_FakeScheduledTaskStore):
        def update_run(self, run_id: str, **kw: Any) -> _RunRow | None:
            self._runs[run_id].status = "succeeded"  # event hook won first
            return super().update_run(run_id, **kw)

    store = _RacingStore([run])
    out = force_fail_stale_runs(store, [run], now=now)
    # update_run returned None (not running anymore); the row we return is the
    # original object, whose status the racing store flipped to succeeded.
    assert out[0].status == "succeeded"
