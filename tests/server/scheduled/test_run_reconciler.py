"""Tests for the scheduled-task run reconciler.

Exercises the terminal-state classification matrix and the sweep's
transition/idempotency/stale-fallback behavior against fakes, with no live
server or database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from omnigent.server.routes.sessions import _LAST_TASK_ERROR_CODE_LABEL_KEY
from omnigent.server.scheduled.run_reconciler import (
    STALE_RUN_ERROR_CODE,
    STALE_RUN_MAX_AGE_SECONDS,
    ScheduledRunReconciler,
    classify_conversation_terminal_state,
)

# ── fakes ────────────────────────────────────────────────────────────────────


@dataclass
class _FakeItem:
    """Minimal stand-in for a ConversationItem (only ``status`` is read)."""

    status: str


@dataclass
class _FakePage:
    """Minimal stand-in for a PagedList (only ``data`` is read)."""

    data: list[_FakeItem] = field(default_factory=list)


@dataclass
class _FakeConversation:
    """Minimal stand-in for a Conversation."""

    live_status: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


class _FakeConversationStore:
    """Fake conversation store returning canned conversations + latest items."""

    def __init__(
        self,
        conversations: dict[str, _FakeConversation | None],
        latest_items: dict[str, list[_FakeItem]] | None = None,
    ) -> None:
        self._conversations = conversations
        self._latest_items = latest_items or {}

    def get_conversation(self, conversation_id: str) -> _FakeConversation | None:
        return self._conversations.get(conversation_id)

    def list_items(
        self,
        conversation_id: str,
        limit: int = 100,
        after: str | None = None,
        before: str | None = None,
        order: str = "asc",
        type: str | None = None,
    ) -> _FakePage:
        return _FakePage(data=list(self._latest_items.get(conversation_id, [])))


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
    workspace_id: int = 0


class _FakeScheduledTaskStore:
    """Fake store exposing the two methods the reconciler uses."""

    def __init__(self, runs: list[_RunRow]) -> None:
        self._runs = {r.id: r for r in runs}
        self.update_calls: list[tuple[str, str, str | None]] = []

    def list_runs_by_status_all_workspaces(self, status: str) -> list[_RunRow]:
        return [r for r in self._runs.values() if r.status == status]

    def update_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: int,
        error: str | None = None,
        error_code: str | None = None,
    ) -> _RunRow | None:
        self.update_calls.append((run_id, status, error_code))
        row = self._runs.get(run_id)
        if row is None or row.status != "running":
            return None  # conditional WHERE status = running
        row.status = status
        row.finished_at = finished_at
        row.error = error
        row.error_code = error_code
        return row


def _running_run(
    seed: str, *, scheduled_at: int = 100, conversation_id: str | None = None
) -> _RunRow:
    return _RunRow(
        id=f"run_{seed}",
        scheduled_task_id=f"task_{seed}",
        status="running",
        scheduled_at=scheduled_at,
        conversation_id=conversation_id or f"conv_{seed}",
        fired_at=scheduled_at + 1,
    )


# ── classify_conversation_terminal_state ─────────────────────────────────────


def test_classify_completed_transcript_is_succeeded() -> None:
    """A quiescent conversation whose latest message is completed → succeeded."""
    conv = _FakeConversation(live_status="idle")
    store = _FakeConversationStore({"c": conv}, {"c": [_FakeItem(status="completed")]})
    decision = classify_conversation_terminal_state(conv, store, "c")
    assert decision.status == "succeeded"
    assert decision.error_code is None


def test_classify_error_label_is_failed_with_code() -> None:
    """A failure label wins over anything else → failed carrying that code."""
    conv = _FakeConversation(
        live_status="failed",
        labels={_LAST_TASK_ERROR_CODE_LABEL_KEY: "runner_error"},
    )
    store = _FakeConversationStore({"c": conv}, {"c": [_FakeItem(status="completed")]})
    decision = classify_conversation_terminal_state(conv, store, "c")
    assert decision.status == "failed"
    assert decision.error_code == "runner_error"


def test_classify_in_flight_live_status_left_running() -> None:
    """live_status running/waiting short-circuits to not-terminal (leave running)."""
    for ls in ("running", "waiting"):
        conv = _FakeConversation(live_status=ls)
        # Even with a completed item present, the in-flight pre-filter wins:
        store = _FakeConversationStore({"c": conv}, {"c": [_FakeItem(status="completed")]})
        decision = classify_conversation_terminal_state(conv, store, "c")
        assert decision.status is None


def test_classify_quiescent_but_no_completed_item_left_running() -> None:
    """Idle with no completed message yet → not terminal (leave running)."""
    conv = _FakeConversation(live_status="idle")
    store = _FakeConversationStore({"c": conv}, {"c": [_FakeItem(status="in_progress")]})
    decision = classify_conversation_terminal_state(conv, store, "c")
    assert decision.status is None


def test_classify_missing_conversation_is_failed() -> None:
    """A deleted conversation → failed(conversation_missing), nothing to await."""
    store = _FakeConversationStore({})
    decision = classify_conversation_terminal_state(None, store, "c")
    assert decision.status == "failed"
    assert decision.error_code == "conversation_missing"


# ── reconcile_once (sweep) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_transitions_completed_run_to_succeeded() -> None:
    """A running run whose conversation completed flips to succeeded + finished_at."""
    run = _running_run("ok")
    sched = _FakeScheduledTaskStore([run])
    conv = _FakeConversation(live_status="idle")
    convs = _FakeConversationStore({"conv_ok": conv}, {"conv_ok": [_FakeItem(status="completed")]})
    rec = ScheduledRunReconciler(sched, convs, now=lambda: 1000)
    n = await rec.reconcile_once()
    assert n == 1
    assert run.status == "succeeded"
    assert run.finished_at == 1000


@pytest.mark.asyncio
async def test_reconcile_transitions_errored_run_to_failed() -> None:
    """A running run whose conversation errored flips to failed with the code."""
    run = _running_run("bad")
    sched = _FakeScheduledTaskStore([run])
    conv = _FakeConversation(
        live_status="failed", labels={_LAST_TASK_ERROR_CODE_LABEL_KEY: "runner_disconnected"}
    )
    convs = _FakeConversationStore({"conv_bad": conv})
    rec = ScheduledRunReconciler(sched, convs, now=lambda: 1000)
    n = await rec.reconcile_once()
    assert n == 1
    assert run.status == "failed"
    assert run.error_code == "runner_disconnected"
    assert run.finished_at == 1000


@pytest.mark.asyncio
async def test_reconcile_leaves_young_running_run_alone() -> None:
    """A young, still-in-flight running run is not touched."""
    run = _running_run("young", scheduled_at=990)
    sched = _FakeScheduledTaskStore([run])
    conv = _FakeConversation(live_status="running")
    convs = _FakeConversationStore({"conv_young": conv})
    rec = ScheduledRunReconciler(sched, convs, now=lambda: 1000)  # age 10s
    n = await rec.reconcile_once()
    assert n == 0
    assert run.status == "running"
    assert run.finished_at is None
    assert sched.update_calls == []  # never even attempted an update


@pytest.mark.asyncio
async def test_reconcile_force_fails_stale_running_run() -> None:
    """A running run past the max-age with a non-terminal conv → failed(incomplete)."""
    now = 10_000_000
    scheduled_at = now - STALE_RUN_MAX_AGE_SECONDS - 1  # just past the threshold
    run = _running_run("stale", scheduled_at=scheduled_at)
    sched = _FakeScheduledTaskStore([run])
    conv = _FakeConversation(live_status="running")  # still "in flight" but stale
    convs = _FakeConversationStore({"conv_stale": conv})
    rec = ScheduledRunReconciler(sched, convs, now=lambda: now)
    n = await rec.reconcile_once()
    assert n == 1
    assert run.status == "failed"
    assert run.error_code == STALE_RUN_ERROR_CODE
    assert run.finished_at == now


@pytest.mark.asyncio
async def test_reconcile_stale_run_still_prefers_real_terminal_state() -> None:
    """A stale run whose conv actually completed is succeeded, not force-failed."""
    now = 10_000_000
    scheduled_at = now - STALE_RUN_MAX_AGE_SECONDS - 1
    run = _running_run("staleok", scheduled_at=scheduled_at)
    sched = _FakeScheduledTaskStore([run])
    conv = _FakeConversation(live_status="idle")
    convs = _FakeConversationStore(
        {"conv_staleok": conv}, {"conv_staleok": [_FakeItem(status="completed")]}
    )
    rec = ScheduledRunReconciler(sched, convs, now=lambda: now)
    n = await rec.reconcile_once()
    assert n == 1
    assert run.status == "succeeded"
    assert run.error_code is None


@pytest.mark.asyncio
async def test_reconcile_idempotent_when_run_already_terminal() -> None:
    """If the conditional update finds the run already terminal, it's a no-op."""
    # Seed a run that the fake reports as 'running' to the sweep but update_run
    # refuses (simulating a race where another writer won first).
    run = _running_run("race")

    # Flip the row terminal AFTER the sweep lists it but BEFORE update — model
    # the race by having update_run see a non-running status.
    class _RacingStore(_FakeScheduledTaskStore):
        def update_run(self, run_id: str, **kw: Any) -> _RunRow | None:
            self._runs[run_id].status = "succeeded"  # someone else won
            return super().update_run(run_id, **kw)

    racing = _RacingStore([run])
    conv = _FakeConversation(live_status="idle")
    convs = _FakeConversationStore(
        {"conv_race": conv}, {"conv_race": [_FakeItem(status="completed")]}
    )
    rec = ScheduledRunReconciler(racing, convs, now=lambda: 1000)
    n = await rec.reconcile_once()
    assert n == 0  # update returned None; not counted as a transition
