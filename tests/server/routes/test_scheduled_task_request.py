"""Field bounds on the scheduled-task create request, checked before any route logic."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.server.routes.scheduled_tasks import CreateScheduledTaskRequest

_BODY = {
    "name": "reminder",
    "prompt": "ping",
    "rrule": "FREQ=DAILY;BYHOUR=9",
    "agent_id": "ag_1",
}


# 253402300800 is one second past 9999-12-31T23:59:59Z, the last one datetime formats.
@pytest.mark.parametrize("first_run_at", [0, 253402300800])
def test_out_of_range_first_run_at_is_rejected(first_run_at: int) -> None:
    # Rejected before any datetime formatting in the route can overflow.
    with pytest.raises(ValidationError, match="first_run_at"):
        CreateScheduledTaskRequest(**_BODY, first_run_at=first_run_at)


def test_last_formattable_first_run_at_is_accepted() -> None:
    assert CreateScheduledTaskRequest(**_BODY, first_run_at=253402300799).first_run_at == (
        253402300799
    )


_ONE_TIME = "FREQ=YEARLY;BYMONTH=11;BYMONTHDAY=14;BYHOUR=15;BYMINUTE=0;BYSECOND=0;COUNT=1"


def test_tasks_are_automations_unless_marked() -> None:
    assert CreateScheduledTaskRequest(**_BODY).kind == "automation"


def test_a_reminder_is_a_one_time_message_into_a_given_session() -> None:
    body = {**_BODY, "rrule": _ONE_TIME, "session_mode": "continue", "session_id": "conv_1"}
    assert CreateScheduledTaskRequest(**body, kind="reminder").kind == "reminder"


@pytest.mark.parametrize(
    "fields",
    [
        {"session_mode": "continue", "session_id": "conv_1"},
        {"rrule": _ONE_TIME, "session_mode": "continue"},
        {"rrule": _ONE_TIME},
    ],
    ids=["recurring", "no_session", "new_session_mode"],
)
def test_a_reminder_that_does_not_fit_is_rejected(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="kind 'reminder' needs"):
        CreateScheduledTaskRequest(**{**_BODY, **fields}, kind="reminder")


def test_an_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="kind"):
        CreateScheduledTaskRequest(**_BODY, kind="trigger")
