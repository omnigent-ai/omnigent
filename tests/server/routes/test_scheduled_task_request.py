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
    ],
    ids=["recurring", "no_session"],
)
def test_a_reminder_that_does_not_fit_is_rejected(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="kind 'reminder' needs"):
        CreateScheduledTaskRequest(**{**_BODY, **fields}, kind="reminder")


def test_an_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="kind"):
        CreateScheduledTaskRequest(**_BODY, kind="trigger")


_TRIGGER = {
    "provider": "slack",
    "event": "posted",
    "channels": ["eng-support"],
    "instructions": "Answer the question.",
}
_TRIGGER_BODY = {key: value for key, value in _BODY.items() if key not in ("prompt", "rrule")}


def test_a_trigger_takes_the_place_of_the_prompt_and_rule() -> None:
    request = CreateScheduledTaskRequest(**_TRIGGER_BODY, trigger_config=_TRIGGER)
    assert request.trigger_config is not None
    assert request.trigger_config.channels == ["eng-support"]


@pytest.mark.parametrize(
    "fields",
    [{"rrule": "FREQ=DAILY"}, {"prompt": "ping"}, {"session_mode": "continue"}],
    ids=["rrule", "prompt", "session_mode"],
)
def test_a_trigger_leaves_out_what_the_server_sets(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="the server sets them"):
        CreateScheduledTaskRequest(**_TRIGGER_BODY, **fields, trigger_config=_TRIGGER)


def test_a_task_without_a_trigger_needs_its_prompt() -> None:
    body = {key: value for key, value in _BODY.items() if key != "prompt"}
    with pytest.raises(ValidationError, match="prompt required without trigger_config"):
        CreateScheduledTaskRequest(**body)
