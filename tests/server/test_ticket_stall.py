"""When a board card reads as stalled, and who a running card shows at work."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.server.project_reliability import settings
from omnigent.server.ticket_stall import (
    CARD_STALL_MIN_S,
    WorkerState,
    card_stall_after,
    in_working_stage,
    ticket_stall,
    ticket_wait,
    ticket_working,
)

STAGES = [
    {"id": "open"},
    {"id": "in_progress"},
    {"id": "code_review"},
    {"id": "done"},
    {"id": "merged"},
]
DEFAULTS = settings({})


def _ticket(**fields):
    return {"labels": [], "waiting_on": None, "updated_at": 1_000, **fields}


def _worker(status: str | None = "idle", updated_at: int = 500, **fields) -> WorkerState:
    return WorkerState(status=status, updated_at=updated_at, name="Platform Engineer", **fields)


def test_working_stages_wait_at_least_the_card_minimum() -> None:
    assert card_stall_after(STAGES, "in_progress", DEFAULTS) == CARD_STALL_MIN_S
    longer = settings({"reliability": {"stage_minutes": {"code_review": 300}}})
    assert card_stall_after(STAGES, "code_review", longer) == 300 * 60


@pytest.mark.parametrize("stage_id", ["open", "merged"])
def test_unstarted_and_complete_stages_never_stall(stage_id: str) -> None:
    assert card_stall_after(STAGES, stage_id, DEFAULTS) is None


def test_a_stage_the_loop_skips_never_stalls() -> None:
    config = settings({"reliability": {"stage_minutes": {"in_progress": 0}}})
    assert card_stall_after(STAGES, "in_progress", config) is None


def test_finished_work_waiting_to_ship_uses_the_ship_wait() -> None:
    assert card_stall_after(STAGES, "done", DEFAULTS) == DEFAULTS.ship_wait_s


def test_idle_since_is_the_latest_of_the_ticket_and_its_workers() -> None:
    stall = ticket_stall(_ticket(), 7200, [_worker(updated_at=1_500)], workers_known=True)
    assert stall == {"idle_since": 1_500, "after_s": 7200}
    no_workers = ticket_stall(_ticket(), 7200, [], workers_known=True)
    assert no_workers == {"idle_since": 1_000, "after_s": 7200}


@pytest.mark.parametrize(
    ("ticket", "workers", "known"),
    [
        (_ticket(), [_worker("running")], True),
        (_ticket(), [_worker("waiting")], True),
        (_ticket(), [_worker(None)], True),
        (_ticket(), [], False),
        (_ticket(labels=[{"key": "needs-human", "value": "Pick A or B"}]), [], True),
        (_ticket(labels=[{"key": "waiting", "value": "2026-11-26T10:00Z release"}]), [], True),
        (_ticket(waiting_on={"reason": "ask", "responders": ["owner@example.com"]}), [], True),
    ],
    ids=[
        "running",
        "waiting-on-approval",
        "status-unknown",
        "workers-unread",
        "open-ask",
        "deliberate-wait",
        "waiting-on-person",
    ],
)
def test_no_stall_clock(ticket, workers, known) -> None:
    assert ticket_stall(ticket, 7200, workers, workers_known=known) is None


def test_a_failed_worker_still_leaves_the_card_stalling() -> None:
    assert ticket_stall(_ticket(), 7200, [_worker("failed")], workers_known=True) is not None


def test_working_names_the_running_worker_and_when_it_started() -> None:
    workers = [_worker("idle"), _worker("running", running_since=1_234.5)]
    assert ticket_working(workers) == {"name": "Platform Engineer", "since": 1_234}
    assert ticket_working([_worker("running")]) == {"name": "Platform Engineer", "since": None}
    assert ticket_working([_worker("idle")]) is None


OWNER = SimpleNamespace(user_id="owner@example.com", config={})


def _labels(**labels: str | None) -> list[dict[str, str | None]]:
    return [{"key": key.replace("_", "-"), "value": value} for key, value in labels.items()]


@pytest.mark.parametrize(
    ("ticket", "expected"),
    [
        (_ticket(labels=_labels(needs_human="Pick A or B")), {"kind": "person", "on": []}),
        (
            _ticket(waiting_on={"reason": "ask", "responders": ["owner@example.com"]}),
            {"kind": "person", "on": []},
        ),
        (
            _ticket(labels=_labels(waiting="2026-11-27T10:00Z release is out")),
            {"kind": "until", "on": ["2026-11-27T10:00Z release is out"]},
        ),
        (
            _ticket(labels=_labels(risk="migration", approved="abc")),
            {"kind": "sign_off", "on": ["risk-ok"]},
        ),
        (
            _ticket(
                labels=_labels(kind="feature", approved="abc"), requested_by="kasey@example.com"
            ),
            {"kind": "sign_off", "on": ["product-ok"]},
        ),
        (
            _ticket(
                labels=_labels(risk="migration", risk_ok=None, approved="abc", verified="abc")
            ),
            {"kind": "land", "on": []},
        ),
        (_ticket(labels=_labels(approved="abc", verified="old")), None),
        (_ticket(labels=_labels(risk="migration")), None),
        (_ticket(), None),
    ],
    ids=[
        "needs-human",
        "open-ask",
        "waiting-label",
        "risk-ok-missing",
        "product-ok-missing",
        "ready-to-land",
        "verified-elsewhere",
        "risk-but-not-approved-yet",
        "nothing",
    ],
)
def test_what_a_card_waits_on(ticket, expected) -> None:
    assert ticket_wait(ticket, OWNER) == expected


def test_only_started_unfinished_stages_wait_or_stall() -> None:
    assert [in_working_stage(STAGES, stage["id"]) for stage in STAGES] == [
        False,
        True,
        True,
        True,
        False,
    ]
